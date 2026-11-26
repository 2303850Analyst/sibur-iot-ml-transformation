import hashlib
import json
from odoo import fields, models
from odoo.exceptions import UserError
from .policy import Address, choose_address, enabled

def require(condition, message):
    if not condition:
        raise UserError(message)

class Quality(models.Model):
    _name = 'wms.v3.quality'
    _description = 'Shared authorized quality decision'
    name = fields.Char(required=True, index=True)
    result = fields.Selection([('hold','Hold'), ('released','Released'), ('failed','Failed')],default='hold',required=True)
    authority = fields.Many2one('res.users')
    version = fields.Integer(default=0)
    _unique = models.Constraint('unique(name)', 'Duplicate quality object')

class Case(models.Model):
    _name = 'wms.v3.case'
    _description = 'Inbound manifest with a fixed member set'
    name = fields.Char(required=True,index=True)
    mask = fields.Integer(required=True)
    quality_id = fields.Many2one('wms.v3.quality',required=True)
    members = fields.Many2many('wms.research.pallet')
    checks = fields.Json(default=dict)
    revision = fields.Integer(default=1)
    serial = fields.Integer(default=0)
    closed = fields.Boolean()
    _unique = models.Constraint('unique(name)', 'Duplicate case')

    def _apply(self, number, role, actor, event_id, version, data):
        self.ensure_one()
        require(type(number) is int and 1 <= number <= 25,'operation')
        require(type(data) is dict and type(event_id) is str and 0 < len(event_id) <= 128,'envelope')
        # Serialize changes to this case. Concurrent snapshot conflicts retry in the HTTP transaction.
        self.env.cr.execute('UPDATE wms_v3_case SET serial=serial+1 WHERE id=%s',[self.id])
        log = self.env['wms.v3.event']
        digest = hashlib.sha256(json.dumps([self.id,number,role,version,data],sort_keys=True).encode()).hexdigest()
        old = log.search([('name','=',event_id)],limit=1)
        if old:
            require(old.digest == digest,'duplicate_conflict')
            return old.result
        require(version == self.revision,'stale_manifest')
        checks = dict(self.checks or {})
        members = self.members
        require(members and not self.closed,'closed_or_empty')
        pallet_ops = {2,10,16,17,18,19,20,21}
        pallet = members.filtered(lambda p:p.pallet_id==data.get('pallet')) if number in pallet_ops else None
        if number in pallet_ops:
            require(len(pallet)==1,'foreign_pallet')
        def key(n,p=None):
            return str(n) if p is None else f'{n}/{p.pallet_id}'
        def done(n,p=None):
            return key(n,p) in checks
        scope = key(number,pallet)
        require(scope not in checks,'step_already_applied')
        roles = {1:'production',2:'operator',3:'operator',4:'operator',5:'operator',6:'quality',7:'quality',
                 8:'quality',9:'quality',10:'driver',11:'quality',12:'operator',13:'operator',14:'operator',
                 15:'operator',16:'operator',17:'dispatch',18:'driver',19:'driver',20:'operator',
                 21:'operator',22:'closing',23:'closing',24:'exchange',25:'closing'}
        extra = {1:('production_adapter',1),9:('quality_adapter',2),2:('sensor',5)}
        require(role==roles[number] or (number in extra and role==extra[number][0] and enabled(self.mask,extra[number][1])), 'role_or_module')
        result = {'status':'applied','operation':number}
        if number==2: result['group']=pallet.storage_group
        if number==1:
            require(sorted(data.get('members',[]))==sorted(members.mapped('pallet_id')),'manifest_mismatch')
        elif number==2:
            require(done(1) and data.get('barcode')==pallet.pallet_id,'identity')
        elif number==3:
            require(done(1) and data.get('document')==self.name,'incomplete_documents')
        elif number in (4,5,7,8,9,13,14,15,23,24,25):
            require(done(number-1),'missing_predecessor')
        elif number==6:
            require(done(1) and data.get('quality_case')==self.quality_id.name,'quality_scope')
        elif number==10:
            require(done(16,pallet),'no_accounting_basis')
            if pallet.quality=='released':
                result['status']='NOT_APPLICABLE:released'
            else:
                require(not pallet.cell_id,'already_moved')
                self._reserve(pallet)
                pallet._transition('place',{'address':pallet.reserved_id.name})
                result['picking']=pallet.transfer_id.id
        elif number==11:
            require(done(9),'no_decision')
            result['status']='open_nonconformity' if self.quality_id.result=='failed' else 'NOT_APPLICABLE:no_nonconformity'
        elif number==12:
            require(done(5),'no_preliminary_acceptance')
        elif number==16:
            require(done(15) and done(2,pallet),'missing_validation')
            pallet._transition('receive',pallet._expected())
            result['picking']=pallet.receipt_id.id
        elif number==17:
            require(done(10,pallet) and done(9) and pallet.quality=='released','quality_or_stock')
            self._reserve(pallet)
            result['address']=pallet.reserved_id.name
        elif number in (18,19):
            require(done(number-1,pallet) and pallet.reserved_id and data.get('address')==pallet.reserved_id.name,'task_address')
            if number==19:
                pallet._transition('final_place' if pallet.cell_id else 'place',data)
                result['picking']=pallet.transfer_id.id
        elif number==20:
            require(done(19,pallet) and pallet.cell_id and pallet.quality=='released' and pallet.transfer_id.state=='done','placement')
            result['address']=pallet.cell_id.name
        elif number==21:
            require(done(20,pallet),'no_verification')
            result['status']='NOT_APPLICABLE:placement_verified'
        elif number==22:
            require(done(11) and all(done(21,p) for p in members),'incomplete_manifest')
        if number==7 and self.quality_id.version:
            result['status']='REUSED:authorized_decision'
        if number==8:
            q=self.quality_id
            if not q.version:
                require(data.get('decision') in ('released','failed','hold'),'invalid_decision')
                q.write({'result':data['decision'],'authority':actor.id,'version':1})
            result['quality_version']=q.version
        if number==9:
            q=self.quality_id
            require(q.authority and data.get('quality_case')==q.name and data.get('quality_version')==q.version,'authority_version')
            if q.result=='released':
                members.filtered(lambda p:p.quality!='released').write({'quality':'released'})
        if number==14:
            require(all(p.product_id and p.lot_id.product_id==p.product_id and p.quantity>0 and p.package_id for p in members),'invalid_members')
        if number==15:
            result['status']='NOT_APPLICABLE:validated' if not data.get('correction') else 'corrected'
        if number==24:
            require(data.get('ack')==self.name+'/'+str(version),'unconfirmed_exchange')
        if number==25:
            require(all(p.quality=='released' and p.transfer_id.state=='done' for p in members),'unresolved_member')
            self.closed=True
        checks[scope]=result
        self.checks=checks
        log.create(dict(name=event_id,digest=digest,case_id=self.id,operation=number,actor_id=actor.id,role=role,
                        version=version,member=pallet.pallet_id if pallet else False,result=result))
        if enabled(self.mask,3):
            automatic = {5:(12,13,14,15), 22:(23,), 24:(25,)}.get(number,())
            for nxt in automatic:
                auto_role=roles[nxt]
                self._apply(nxt,auto_role,actor,event_id+'/workflow/'+str(nxt),version,{})
        return result

    def _reserve(self,pallet):
        # A shared write prevents concurrent assignments in different receipts.
        self.env.cr.execute('UPDATE wms_research_mutex SET version=version+1 WHERE id=1')
        require(self.env.cr.rowcount==1,'missing_reservation_mutex')
        busy=self.env['wms.research.pallet'].search(['|',('reserved_id','!=',False),('cell_id','!=',False)])
        unavailable=(busy.reserved_id | busy.cell_id).ids
        cells=self.env['wms.research.cell'].search([('run_id','=','V3'),('id','not in',unavailable)])
        options=[Address(c.name,c.storage_group,c.zone=='quarantine',c.distance) for c in cells]
        winner=choose_address(options,pallet.storage_group,pallet.quality=='released',self.mask)
        require(winner,'no_free_address')
        pallet.reserved_id=cells.filtered(lambda c:c.name==winner.name).ensure_one()

class Event(models.Model):
    _name = 'wms.v3.event'
    _description = 'Applied inbound event'
    name=fields.Char(required=True)
    digest=fields.Char(required=True)
    case_id=fields.Many2one('wms.v3.case',required=True)
    operation=fields.Integer(required=True)
    actor_id=fields.Many2one('res.users',required=True)
    role=fields.Char(required=True)
    version=fields.Integer(required=True)
    member=fields.Char()
    result=fields.Json(required=True)
    _unique=models.Constraint('unique(name)','Duplicate event')

