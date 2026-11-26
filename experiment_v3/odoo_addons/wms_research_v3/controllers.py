import json
from odoo import http
from odoo.http import request
from odoo.exceptions import UserError
from pathlib import Path

ROLES=('production','operator','driver','quality','dispatch','closing','supervisor','production_adapter','quality_adapter','sensor','exchange')
class Inbound(http.Controller):
    @http.route('/v3/tsd',type='http',auth='user',methods=['GET'])
    def mobile(self):
        markup=(Path(__file__).parent/'static/tsd.html').read_text()
        return request.make_response(markup.replace('TOKEN_VALUE',request.csrf_token()),headers=[('Content-Type','text/html; charset=utf-8')])

    @http.route('/v3/step',type='http',auth='user',methods=['POST'],csrf=True)
    def step(self,message=''):
        try:
            if len(message)>8192:
                raise ValueError()
            msg=json.loads(message)
            role=msg['role']
            if role not in ROLES or not request.env.user.has_group('wms_research_v3.role_'+role):
                return request.make_json_response({'error':'role'},status=403)
            # Users have no generic ORM write access to research objects. Elevation is scoped
            # to the validated business command; caller context is never forwarded.
            cases=request.env['wms.v3.case'].with_context({}).sudo()
            case=cases.search([('name','=',msg['case'])],limit=1)
            if not case:
                return request.make_json_response({'error':'case'},status=404)
            with request.env.cr.savepoint():
                if msg['operation']=='reading' and role=='sensor':
                    result=case._reading(**msg['data'])
                elif msg['operation']=='next' and role=='dispatch':
                    result=case._next()
                else:
                    result=case._apply(msg['operation'],role,request.env.user,msg['event_id'],msg['version'],msg['data'])
            return request.make_json_response(result)
        except (TypeError,KeyError,ValueError):
            return request.make_json_response({'error':'invalid_message'},status=400)
        except UserError as e:
            return request.make_json_response({'error':str(e)},status=422)

