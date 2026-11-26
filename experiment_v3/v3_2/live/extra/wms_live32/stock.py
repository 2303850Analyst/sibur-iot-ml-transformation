from odoo import models
from odoo.exceptions import UserError
from odoo.addons.wms_research_v3.policy import choose_address,Address

class Case(models.Model):
    _inherit='wms.v3.case'
    def _apply(self,number,role,actor,event_id,version,data):
        self.ensure_one()
        if number==16:
            self.env.cr.execute('UPDATE wms_research_mutex SET version=version+1 WHERE id=1')
            p=self.members.filtered(lambda u:u.pallet_id==data.get('pallet')).ensure_one()
            if p.receipt_id.state!='done':
                occupancy=self.env['stock.quant'].search_count([('location_id','=',p.receipt_id.location_dest_id.id),('quantity','>',0)])
                if occupancy>=self.capacity:raise UserError('buffer_full')
        return super()._apply(number,role,actor,event_id,version,data)
    def _reserve(self,pallet):
        self.env.cr.execute('UPDATE wms_research_mutex SET version=version+1 WHERE id=1')
        if self.env.cr.rowcount!=1:raise UserError('reservation_mutex')
        reservations=self.env['wms.research.pallet'].search([('reserved_id','!=',False)]).reserved_id
        pool=self.env['wms.research.cell'].search([('run_id','=','V3'),('id','not in',reservations.ids)])
        occupied=set(self.env['stock.quant'].search([('quantity','>',0),('location_id','in',pool.location_id.ids)]).location_id.ids)
        available=[Address(c.name,c.storage_group,c.zone=='quarantine',c.distance) for c in pool if c.location_id.id not in occupied]
        win=choose_address(available,pallet.storage_group,pallet.quality=='released',self.mask)
        if not win:raise UserError('no_free_address')
        pallet.reserved_id=pool.filtered(lambda c:c.name==win.name).ensure_one()

