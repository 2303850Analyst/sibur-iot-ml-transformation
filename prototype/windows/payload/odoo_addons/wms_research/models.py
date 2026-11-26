import hashlib
import json

from odoo import fields, models
from odoo.exceptions import UserError

from .rules import eligible, identity_matches


class Mutex(models.Model):
    _name = 'wms.research.mutex'
    _description = 'Research transaction scope'

    version = fields.Integer(default=0)


class Cell(models.Model):
    _name = 'wms.research.cell'
    _description = 'Research storage address'
    _order = 'distance, name, id'

    name = fields.Char(required=True)
    run_id = fields.Char(required=True, default='DEMO', index=True)
    zone = fields.Selection([('storage', 'Storage'), ('quarantine', 'Quarantine')], required=True)
    storage_group = fields.Integer()
    distance = fields.Float(required=True)
    location_id = fields.Many2one('stock.location', required=True)
    _name_key = models.Constraint('UNIQUE(run_id,name)', 'The address must be unique within a run.')


class Event(models.Model):
    _name = 'wms.research.event'
    _description = 'Research device event and operation audit'

    run_id = fields.Char(required=True)
    pallet_id = fields.Char(required=True)
    event_id = fields.Char(required=True)
    operation = fields.Char(required=True)
    operation_key = fields.Char()
    occurred_at = fields.Datetime(required=True)
    received_at = fields.Datetime(required=True)
    processed_at = fields.Datetime(required=True, default=fields.Datetime.now)
    device_id = fields.Char(required=True)
    source = fields.Selection([('TSD', 'TSD'), ('IOT', 'IoT'), ('QUALITY', 'Quality')], required=True)
    schema_version = fields.Integer(required=True)
    payload_hash = fields.Char(required=True)
    payload = fields.Json()
    status = fields.Char(required=True)
    reason = fields.Char()
    picking_id = fields.Many2one('stock.picking')
    result = fields.Json()
    _event_key = models.Constraint('UNIQUE(event_id)', 'Event ID already recorded.')
    _business_key = models.Constraint('UNIQUE(run_id,pallet_id,operation_key)', 'Operation already applied.')


class Pallet(models.Model):
    _name = 'wms.research.pallet'
    _description = 'Expected pallet and its stock transactions'

    run_id = fields.Char(required=True)
    pallet_id = fields.Char(required=True)
    product_id = fields.Many2one('product.product', required=True)
    lot_id = fields.Many2one('stock.lot', required=True)
    package_id = fields.Many2one('stock.package', required=True)
    quantity = fields.Float(required=True)
    storage_group = fields.Integer(required=True)
    quality = fields.Selection([(value, value) for value in ('unknown', 'hold', 'released')], required=True)
    receipt_id = fields.Many2one('stock.picking', required=True)
    transfer_id = fields.Many2one('stock.picking')
    cell_id = fields.Many2one('wms.research.cell')
    reserved_id = fields.Many2one('wms.research.cell')
    _pallet_key = models.Constraint('UNIQUE(run_id,pallet_id)', 'The pallet must be unique within its run.')

    def _expected(self):
        return dict(sku=self.product_id.default_code, lot=self.lot_id.name, quantity=self.quantity)

    def _snapshot(self):
        quants = self.env['stock.quant'].search([('package_id', '=', self.package_id.id)])
        return {**self._expected(), 'quality': self.quality, 'pallet': self.pallet_id,
                'receipt_id': self.receipt_id.id, 'receipt_state': self.receipt_id.state,
                'transfer_id': self.transfer_id.id or None, 'reserved': self.reserved_id.name or None,
                'cell': self.cell_id.name or None, 'quants': [
                    [quant.location_id.name, quant.lot_id.name, quant.quantity]
                    for quant in quants if quant.quantity],
                'odoo_url': f'/odoo/action-stock.action_picking_tree_all/{self.receipt_id.id}'}

    @staticmethod
    def _new_picking(env, product, quantity, source, destination, picking_type, origin):
        picking = env['stock.picking'].create(dict(
            picking_type_id=picking_type.id, location_id=source.id,
            location_dest_id=destination.id, origin=origin,
        ))
        env['stock.move'].create(dict(
            product_id=product.id, product_uom=product.uom_id.id, product_uom_qty=quantity,
            picking_id=picking.id, location_id=source.id, location_dest_id=destination.id,
        ))
        picking.action_confirm()
        return picking

    def _finish(self, picking, packaged):
        move = picking.move_ids.ensure_one()
        move.move_line_ids.unlink()
        self.env['stock.move.line'].create(dict(
            move_id=move.id, picking_id=picking.id, product_id=self.product_id.id,
            product_uom_id=move.product_uom.id, quantity=self.quantity, picked=True,
            company_id=picking.company_id.id, lot_id=self.lot_id.id,
            location_id=picking.location_id.id, location_dest_id=picking.location_dest_id.id,
            package_id=self.package_id.id if packaged else False, result_package_id=self.package_id.id,
        ))
        result = picking.with_context(skip_backorder=True).button_validate()
        if picking.state != 'done':
            raise UserError(f'Transfer not completed: {result}')

    def _transition(self, operation, payload):
        picking = self.env['stock.picking']
        if operation == 'observe':
            if payload.get('barcode') != self.pallet_id:
                raise UserError('barcode_mismatch')
            return picking
        if operation == 'receive':
            if not identity_matches(payload, self._expected()):
                raise UserError('identity_mismatch')
            if self.receipt_id.state in ('done', 'cancel'):
                raise UserError('receipt_not_open')
            buffer = self.receipt_id.location_dest_id
            if self.env.context.get('reference_serial_replay'):
                occupancy = self.env['stock.quant'].search_count([('location_id', '=', buffer.id), ('quantity', '>', 0)])
                capacity = self.env.context['reference_buffer_capacity']
                if not occupancy < self.env.context['reference_physical_buffer'] <= capacity:
                    raise UserError('buffer_projection_mismatch')
            elif self.search_count([('receipt_id.state', '=', 'done'), ('cell_id', '=', False),
                                    ('receipt_id.location_dest_id', '=', buffer.id)]) >= 40:
                raise UserError('buffer_full')
            self._finish(self.receipt_id, False)
            return self.receipt_id
        if operation == 'release':
            if self.quality not in ('hold', 'unknown'):
                raise UserError('not_on_hold')
            self.quality = 'released'
            return picking
        if self.receipt_id.state != 'done':
            raise UserError('receive_first')
        if self.cell_id.zone == 'storage':
            raise UserError('already_stored')
        if operation.startswith('final_') != bool(self.cell_id):
            raise UserError('wrong_stage')
        if operation.endswith('reserve'):
            occupied = self.search([('run_id', '=', self.run_id), ('id', '!=', self.id), '|', ('cell_id', '!=', False),
                                    ('reserved_id', '!=', False)])
            unavailable = (occupied.cell_id | occupied.reserved_id | self.cell_id).ids
            pool = self.env.context.get('reference_cell_pool', self.run_id)
            domain = [('run_id', '=', pool), ('id', 'not in', unavailable)]
            if 'reference_available' in self.env.context:
                domain.append(('id', 'in', self.env.context['reference_available']))
            if self.quality != 'released':
                domain.append(('zone', '=', 'quarantine'))
            else:
                domain += [('zone', '=', 'storage'), ('storage_group', '=', self.storage_group)]
            for cell in self.env['wms.research.cell'].search(domain, limit=1):
                if eligible(self.quality, self.storage_group, cell.zone, cell.storage_group):
                    self.reserved_id = cell
                    return picking
            raise UserError('no_available_address')
        cell = self.reserved_id
        if not cell or payload.get('address') != cell.name:
            raise UserError('wrong_address')
        if cell.zone != 'quarantine' and not eligible(self.quality, self.storage_group, cell.zone, cell.storage_group):
            raise UserError('incompatible_address')
        source = self.cell_id.location_id or self.receipt_id.location_dest_id
        if self.env.context.get('reference_serial_replay') and self.env.context.get('reference_transit_id'):
            source = self.env['stock.location'].browse(self.env.context['reference_transit_id'])
        quants = self.env['stock.quant'].search([('package_id', '=', self.package_id.id),
                                                ('location_id', '=', source.id), ('lot_id', '=', self.lot_id.id)])
        if sum(quants.mapped('quantity')) != self.quantity:
            raise UserError('unexpected_source_balance')
        picking = self._new_picking(self.env, self.product_id, self.quantity, source,
                                    cell.location_id, self.receipt_id.picking_type_id.warehouse_id.int_type_id,
                                    self.pallet_id)
        self._finish(picking, True)
        self.write({'cell_id': cell.id, 'reserved_id': False, 'transfer_id': picking.id})
        return picking

    def _apply(self, message, source):
        if not self.env.context.get('reference_serial_replay'):
            self.env.cr.execute('UPDATE wms_research_mutex SET version=version+1 WHERE id=1')
        payload = message['payload']
        operation = message['operation']
        event_id = message['event_id']
        pallet_id = message['pallet_id']
        run_id = message['run_id']
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        journal = self.env['wms.research.event']
        old = journal.search([('event_id', '=', event_id)], limit=1)
        if old:
            if (old.payload_hash, old.run_id, old.pallet_id, old.operation) != (digest, run_id, pallet_id, operation):
                return dict(status='conflict', reason='event_id_reused', audit_id=old.id)
            return old.result
        operation_key = f'{operation}:{event_id}' if operation in ('observe', 'presence') else operation
        old = journal.search([('run_id', '=', run_id), ('pallet_id', '=', pallet_id),
                              ('operation_key', '=', operation_key)], limit=1)
        if old and old.payload_hash == digest:
            return old.result
        pallet = self.search([('run_id', '=', run_id), ('pallet_id', '=', pallet_id)], limit=1)
        picking = self.env['stock.picking']
        status, reason = 'applied', None
        if old:
            status, reason = 'conflict', 'business_key_payload_conflict'
        elif operation != 'presence':
            try:
                with self.env.cr.savepoint():
                    if not pallet:
                        raise UserError('unknown_pallet')
                    picking = pallet._transition(operation, payload)
            except UserError as error:
                status, reason = 'rejected', str(error)
        result = dict(status=status, reason=reason, picking_id=picking.id or None,
                      snapshot=pallet._snapshot() if pallet else None)
        entry = journal.create(dict(run_id=run_id, pallet_id=pallet_id, operation=operation,
                                    operation_key=operation_key if status == 'applied' else False,
                                    event_id=event_id, occurred_at=message['occurred_at'],
                                    received_at=fields.Datetime.now(), device_id=message['device_id'],
                                    source=source, schema_version=1, payload_hash=digest, payload=payload,
                                    status=status, reason=reason, picking_id=picking.id))
        result['audit_id'] = entry.id
        entry.result = result
        return result
