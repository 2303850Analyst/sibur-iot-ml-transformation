def make_case(env,name,mask):
    wh=env['stock.warehouse'].search([],limit=1)
    buffer=env['stock.location'].search([('name','=','V3-Buffer'),('location_id','=',wh.lot_stock_id.id)],limit=1)
    product=env['product.product'].search([('default_code','=','V3-SKU')],limit=1)
    cases=env['wms.v3.case']
    pallets=env['wms.research.pallet']
    quality=env['wms.v3.quality'].create({'name':'Q-'+name})
    members=[]
    lot=env['stock.lot'].create({'name':'LOT-'+name,'product_id':product.id,'company_id':env.company.id})
    for i in range(2):
        unit=f'{name}-{i}'
        package=env['stock.package'].create({'name':unit})
        receipt=pallets._new_picking(env,product,1,env.ref('stock.stock_location_suppliers'),buffer,wh.in_type_id,unit)
        members.append(pallets.create(dict(run_id='V3',pallet_id=unit,product_id=product.id,lot_id=lot.id,package_id=package.id,
                  quantity=1,storage_group=0,quality='hold',receipt_id=receipt.id)).id)
    return cases.create({'name':name,'mask':mask,'scope':name,'quality_id':quality.id,'members':[(6,0,members)]})
