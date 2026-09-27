"""Tenant-scoped workflows; each command commits its complete audit trail atomically."""
import csv
import hashlib
import io
import json
from sqlalchemy import Table, Column as C, String as S, Integer as I, JSON, UniqueConstraint as U, CheckConstraint as CK, select, insert, update
from . import db
from .domain import BusinessError, uid, now, FIELDS, require_role, get_warehouse, get_sku, available, unallocated

imports = Table('import_batches',db.metadata,C('id',S,primary_key=True),C('tenant_id',S,nullable=False),C('actor_id',S),C('kind',S),C('digest',S),C('rows',JSON),C('errors',JSON),C('status',S),C('created_at',S),C('applied_at',S),U('tenant_id','kind','digest'))
orders = Table('purchase_orders',db.metadata,C('id',S,primary_key=True),C('tenant_id',S,nullable=False),C('warehouse_id',S),C('code',S),C('supplier',S),C('note',S),C('status',S),C('version',I),C('actor_id',S),C('created_at',S),C('close_reason',S),U('tenant_id','code'))
lines = Table('purchase_lines',db.metadata,C('id',S,primary_key=True),C('order_id',S,nullable=False),C('sku_id',S),C('ordered',I,nullable=False),C('received',I,nullable=False),U('order_id','sku_id'),CK('ordered > 0'),CK('received >= 0 AND received <= ordered'))
receipts = Table('purchase_receipts',db.metadata,C('id',S,primary_key=True),C('tenant_id',S,nullable=False),C('order_id',S),C('reference',S),C('document_ids',JSON),C('created_at',S),U('tenant_id','order_id','reference'))
approvals = Table('approval_requests',db.metadata,C('id',S,primary_key=True),C('tenant_id',S,nullable=False),C('warehouse_id',S),C('sku_id',S),C('kind',S),C('original_id',S),C('delta',JSON),C('expected_version',I),C('reason',S),C('requester_id',S),C('reviewer_id',S),C('review_note',S),C('status',S),C('document_id',S),C('created_at',S),C('reviewed_at',S))
reversals = Table('reversal_links',db.metadata,C('original_id',S,primary_key=True),C('tenant_id',S,nullable=False),C('reversal_id',S,unique=True),C('approval_id',S))
TABLES = {'skus':db.skus,'warehouses':db.warehouses}
CSV_FIELDS = {'skus':['code','name','spec','unit','barcode'], 'warehouses':['code','name','country','timezone','authority'], 'opening':['warehouse_code','sku_code',*FIELDS]}

def fail(code,message,status=409):raise BusinessError(code,message,status)

def command(engine,actor,kind,body,key,action):
    digest=hashlib.sha256(json.dumps({'kind':kind,'body':body},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with db.write(engine,actor['tenant_id']) as c:
        from .accounts import assert_current
        assert_current(c,actor)
        old=c.execute(select(db.requests).where(db.requests.c.tenant_id==actor['tenant_id'],db.requests.c.actor_id==actor['id'],db.requests.c.key==key)).mappings().first()
        if old:
            if old['fingerprint']!=digest:fail('IDEMPOTENCY_CONFLICT','请求编号已用于其他内容')
            return {**old['response'],'replayed':True}
        result=action(c)
        result['replayed']=False
        c.execute(insert(db.requests).values(id=uid(),tenant_id=actor['tenant_id'],actor_id=actor['id'],key=key,fingerprint=digest,response=result))
        return result

def catalog_create(engine,actor,kind,body,key):
    require_role(actor,'admin')
    def apply(c):
        row={'id':uid(),'tenant_id':actor['tenant_id'],**body}
        c.execute(insert(TABLES[kind]).values(**row))
        return row
    return command(engine,actor,'catalog:'+kind,body,key,apply)

def balance(c,actor,warehouse_id,sku_id):
    get_warehouse(c,actor,warehouse_id,True);get_sku(c,actor,sku_id)
    row=c.execute(select(db.balances).where(db.balances.c.tenant_id==actor['tenant_id'],db.balances.c.owner_id==actor['tenant_id'],db.balances.c.warehouse_id==warehouse_id,db.balances.c.sku_id==sku_id)).mappings().first()
    return dict(row) if row else dict(id=uid(),tenant_id=actor['tenant_id'],owner_id=actor['tenant_id'],warehouse_id=warehouse_id,sku_id=sku_id,version=0,updated_at=now(),**dict.fromkeys(FIELDS,0))

def valid_balance(row):
    if any(row[f]<0 for f in FIELDS):fail('INSUFFICIENT_STOCK','操作会使库存或占用数量小于零')
    if available(row)<0 or unallocated(row)<0:fail('COMMITMENT_CONFLICT','良品不足以覆盖订单、调拨、冻结、安全量和已分配额度')

def movement(c,actor,row,delta,kind,source,note,expected):
    if row['version']!=expected:fail('VERSION_CONFLICT','库存已变化，请刷新后重新申请或确认')
    before={f:row[f] for f in FIELDS}
    for f in FIELDS:row[f]+=delta.get(f,0)
    valid_balance(row)
    from .operations import protect_pending_quality
    protect_pending_quality(c,actor,row)
    timestamp=now();row['version']+=1;row['updated_at']=timestamp
    if c.execute(select(db.balances.c.id).where(db.balances.c.id==row['id'])).first():
        c.execute(update(db.balances).where(db.balances.c.id==row['id']).values(**row))
    else:c.execute(insert(db.balances).values(**row))
    doc_id=kind.upper()[:4]+'-'+uid()[:18]
    c.execute(insert(db.documents).values(id=doc_id,tenant_id=actor['tenant_id'],kind=kind,warehouse_id=row['warehouse_id'],sku_id=row['sku_id'],quantity=sum(delta.get(f,0) for f in ('g','q','d')),source=source,note=note,actor_id=actor['id'],created_at=timestamp,status='completed'))
    c.execute(insert(db.ledger).values(id=uid(),tenant_id=actor['tenant_id'],owner_id=actor['tenant_id'],warehouse_id=row['warehouse_id'],sku_id=row['sku_id'],document_id=doc_id,kind=kind,delta={f:row[f]-before[f] for f in FIELDS},before=before,after={f:row[f] for f in FIELDS},actor_id=actor['id'],created_at=timestamp))
    c.execute(insert(db.outbox).values(id=uid(),tenant_id=actor['tenant_id'],document_id=doc_id,event_type='inventory.changed',payload={'warehouse_id':row['warehouse_id'],'sku_id':row['sku_id'],'version':row['version']},status='not_connected',created_at=timestamp))
    return doc_id

def opening_row(c,actor,row):
    wh=c.execute(select(db.warehouses).where(db.warehouses.c.tenant_id==actor['tenant_id'],db.warehouses.c.code==row['warehouse_code'])).mappings().first()
    sku=c.execute(select(db.skus).where(db.skus.c.tenant_id==actor['tenant_id'],db.skus.c.code==row['sku_code'])).mappings().first()
    if not wh or not sku:fail('MISSING_MAPPING','仓库编码或商品编码不存在')
    stock=balance(c,actor,wh['id'],sku['id'])
    if stock['version']!=0:fail('OPENING_EXISTS','该仓库商品已有库存历史，禁止再次导入期初')
    valid_balance({**stock,**{f:row[f] for f in FIELDS}})
    return stock

def import_preview(engine,actor,body):
    require_role(actor,'admin')
    # Local model imports avoid circular module registration.
    from .main import SKU, Warehouse
    kind=body['kind'];reader=csv.DictReader(io.StringIO(body['csv_text'].lstrip('\ufeff')),strict=True)
    required={'skus':{'code','name','spec'},'warehouses':{'code','name'},'opening':{'warehouse_code','sku_code','g'}}[kind]
    headers=reader.fieldnames or []
    if len(headers)!=len(set(headers)) or not required.issubset(headers) or set(headers)-set(CSV_FIELDS[kind]):
        fail('INVALID_HEADERS','表头缺失、重复或包含未知列，请使用模板',422)
    rows=[];errors=[];seen=set()
    with db.write(engine,actor['tenant_id']) as c:
        try:
            for n,raw in enumerate(reader,2):
                if n>501:fail('IMPORT_TOO_LARGE','每次最多导入500行',422)
                try:
                    if None in raw or any(v is None for v in raw.values()):raise ValueError('列数与表头不一致')
                    raw={k:v.strip() for k,v in raw.items()}
                    if kind=='opening':
                        row={'warehouse_code':raw['warehouse_code'],'sku_code':raw['sku_code']}
                        if not row['warehouse_code'] or not row['sku_code']:raise ValueError('编码不能为空')
                        for f in FIELDS:
                            v=raw.get(f,'0') or '0'
                            if not v.isascii() or not v.isdecimal() or int(v)>1_000_000:raise ValueError(f'{f}必须为0至1000000的整数')
                            row[f]=int(v)
                        identity=(row['warehouse_code'],row['sku_code'])
                        opening_row(c,actor,row)
                    else:
                        row=(SKU if kind=='skus' else Warehouse).model_validate({k:v for k,v in raw.items() if v!=''}).model_dump()
                        identity=row['code'];table=TABLES[kind]
                        if c.execute(select(table.c.id).where(table.c.tenant_id==actor['tenant_id'],table.c.code==identity)).first():raise ValueError('编码已存在')
                    if identity in seen:raise ValueError('文件内编码重复')
                    seen.add(identity);rows.append(row)
                except (ValueError,BusinessError) as e:
                    errors.append({'line':n,'message':str(e)})
        except csv.Error:fail('INVALID_CSV','CSV格式错误，请重新导出为UTF-8 CSV',422)
        if not rows and not errors:fail('EMPTY_IMPORT','文件没有数据行',422)
        # Replaying the same normalized file identifies its original batch even after applying.
        content_digest=hashlib.sha256(body['csv_text'].lstrip('\ufeff').replace('\r\n','\n').strip().encode()).hexdigest()
        old=c.execute(select(imports).where(imports.c.tenant_id==actor['tenant_id'],imports.c.kind==kind,imports.c.digest==content_digest)).mappings().first()
        if old and old['status']=='applied':return dict(old)
        record=dict(id=old['id'] if old else uid(),tenant_id=actor['tenant_id'],actor_id=actor['id'],kind=kind,digest=content_digest,rows=rows,errors=errors,status='invalid' if errors else 'preview',created_at=now(),applied_at=None)
        if old:c.execute(update(imports).where(imports.c.id==old['id']).values(**record))
        else:c.execute(insert(imports).values(**record))
        return record

def import_apply(engine,actor,batch_id,key):
    require_role(actor,'admin')
    def apply(c):
        batch=c.execute(select(imports).where(imports.c.id==batch_id,imports.c.tenant_id==actor['tenant_id'])).mappings().first()
        if not batch:fail('NOT_FOUND','导入批次不存在',404)
        if batch['status']=='applied':return {'batch_id':batch_id,'already_applied':True,'count':len(batch['rows'])}
        if batch['status']!='preview':fail('INVALID_IMPORT','请先修正预览中的错误')
        for row in batch['rows']:
            if batch['kind']=='opening':
                stock=opening_row(c,actor,row)
                movement(c,actor,stock,{f:row[f] for f in FIELDS},'opening',batch_id,'正式期初导入（本地开发环境）',0)
            else:c.execute(insert(TABLES[batch['kind']]).values(id=uid(),tenant_id=actor['tenant_id'],**row))
        c.execute(update(imports).where(imports.c.id==batch_id).values(status='applied',applied_at=now()))
        return {'batch_id':batch_id,'count':len(batch['rows']),'already_applied':False}
    return command(engine,actor,'import:'+batch_id,{},key,apply)

def get_order(c,actor,order_id):
    row=c.execute(select(orders).where(orders.c.id==order_id,orders.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not row:fail('NOT_FOUND','采购单不存在',404)
    get_warehouse(c,actor,row['warehouse_id'],True)
    return dict(row)

def order_view(c,row):
    history=[dict(r) for r in c.execute(select(receipts).where(receipts.c.order_id==row['id'],receipts.c.tenant_id==row['tenant_id']).order_by(receipts.c.created_at)).mappings()]
    for receipt in history:
        receipt['reversed_document_ids']=list(c.execute(select(reversals.c.original_id).where(reversals.c.tenant_id==row['tenant_id'],reversals.c.original_id.in_(receipt['document_ids']))).scalars())
    return {**row,'lines':[dict(r) for r in c.execute(select(lines).where(lines.c.order_id==row['id'])).mappings()], 'receipt_history':history}

def purchase_create(engine,actor,body,key):
    require_role(actor,'admin','warehouse')
    def apply(c):
        get_warehouse(c,actor,body['warehouse_id'],True)
        row={k:v for k,v in body.items() if k!='lines'}
        row.update(id=uid(),tenant_id=actor['tenant_id'],status='open',version=1,actor_id=actor['id'],created_at=now(),close_reason='')
        c.execute(insert(orders).values(**row))
        for item in body['lines']:
            get_sku(c,actor,item['sku_id'])
            c.execute(insert(lines).values(id=uid(),order_id=row['id'],sku_id=item['sku_id'],ordered=item['quantity'],received=0))
        return order_view(c,row)
    return command(engine,actor,'purchase.create',body,key,apply)

def purchase_receive(engine,actor,order_id,body,key):
    require_role(actor,'admin','warehouse')
    def apply(c):
        order=get_order(c,actor,order_id)
        if c.execute(select(receipts.c.id).where(receipts.c.tenant_id==actor['tenant_id'],receipts.c.order_id==order_id,receipts.c.reference==body['reference'])).first():fail('RECEIPT_EXISTS','该到货凭证已登记，请查看原收货记录，不能重复入账')
        if order['status'] not in ('open','partial'):fail('ORDER_CLOSED','采购单已完成或关闭')
        if order['version']!=body['expected_version']:fail('VERSION_CONFLICT','采购单已变化，请刷新后确认')
        ids=[]
        for item in body['lines']:
            line=c.execute(select(lines).where(lines.c.order_id==order_id,lines.c.sku_id==item['sku_id'])).mappings().first()
            if not line:fail('NOT_FOUND','商品不在此采购单中',404)
            quantity=sum(item[f] for f in ('g','q','d'))
            if quantity>line['ordered']-line['received']:fail('OVER_RECEIPT','实收数量超过采购单剩余未收数量')
            stock=balance(c,actor,order['warehouse_id'],line['sku_id'])
            ids.append(movement(c,actor,stock,{f:item[f] for f in ('g','q','d')},'purchase_receipt',order_id,body['reference'],item['expected_version']))
            c.execute(update(lines).where(lines.c.id==line['id']).values(received=line['received']+quantity))
        status='completed' if all(r['received']==r['ordered'] for r in c.execute(select(lines).where(lines.c.order_id==order_id)).mappings()) else 'partial'
        c.execute(update(orders).where(orders.c.id==order_id).values(status=status,version=order['version']+1))
        rid=uid();c.execute(insert(receipts).values(id=rid,tenant_id=actor['tenant_id'],order_id=order_id,reference=body['reference'],document_ids=ids,created_at=now()))
        return {'receipt_id':rid,'document_ids':ids,'order_id':order_id,'status':status,'version':order['version']+1}
    return command(engine,actor,'purchase.receive:'+order_id,body,key,apply)

def purchase_close(engine,actor,order_id,body,key):
    require_role(actor,'admin','warehouse')
    def apply(c):
        row=get_order(c,actor,order_id)
        if row['version']!=body['expected_version']:fail('VERSION_CONFLICT','采购单已变化')
        if row['status'] not in ('open','partial'):fail('ORDER_CLOSED','采购单已完成或关闭')
        c.execute(update(orders).where(orders.c.id==order_id).values(status='closed',close_reason=body['reason'],version=row['version']+1))
        return {'order_id':order_id,'status':'closed','version':row['version']+1}
    return command(engine,actor,'purchase.close:'+order_id,body,key,apply)

def original(c,actor,document_id):
    row=c.execute(select(db.documents).where(db.documents.c.id==document_id,db.documents.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not row:fail('NOT_FOUND','原单据不存在',404)
    get_warehouse(c,actor,row['warehouse_id'],True)
    if row['kind'] not in ('receipt','sale','purchase_receipt','adjustment'):fail('UNSUPPORTED_REVERSAL','此类单据不可冲正；期初数据须通过审批调整，冲正单不可再次冲正')
    if c.execute(select(reversals).where(reversals.c.original_id==document_id)).first():fail('ALREADY_REVERSED','该单据已冲正')
    from .operations import returns
    if c.execute(select(returns.c.id).where(returns.c.original_id==document_id,returns.c.tenant_id==actor['tenant_id'])).first():fail('HAS_RETURNS','原单已关联退货，不能再按原单全额冲正')
    entry=c.execute(select(db.ledger).where(db.ledger.c.document_id==document_id,db.ledger.c.tenant_id==actor['tenant_id'])).mappings().one()
    return row,{f:entry['before'][f]-entry['after'][f] for f in FIELDS}

def approval_request(engine,actor,body,key):
    require_role(actor,'admin','warehouse')
    def apply(c):
        if body['kind']=='reversal':
            doc,delta=original(c,actor,body['original_id']);warehouse_id=doc['warehouse_id'];sku_id=doc['sku_id']
        else:delta=body['delta'];warehouse_id=body['warehouse_id'];sku_id=body['sku_id']
        stock=balance(c,actor,warehouse_id,sku_id)
        if stock['version']!=body['expected_version']:fail('VERSION_CONFLICT','库存已变化，请刷新后重新申请')
        valid_balance({**stock,**{f:stock[f]+delta.get(f,0) for f in FIELDS}})
        row=dict(id=uid(),tenant_id=actor['tenant_id'],warehouse_id=warehouse_id,sku_id=sku_id,kind=body['kind'],original_id=body.get('original_id'),delta=delta,expected_version=body['expected_version'],reason=body['reason'],requester_id=actor['id'],reviewer_id=None,review_note=None,status='pending',document_id=None,created_at=now(),reviewed_at=None)
        c.execute(insert(approvals).values(**row))
        return row
    return command(engine,actor,'approval.request',body,key,apply)

def approval_review(engine,actor,approval_id,body,key):
    require_role(actor,'admin')
    def apply(c):
        row=c.execute(select(approvals).where(approvals.c.id==approval_id,approvals.c.tenant_id==actor['tenant_id'])).mappings().first()
        if not row:fail('NOT_FOUND','审批单不存在',404)
        get_warehouse(c,actor,row['warehouse_id'],True)
        if row['requester_id']==actor['id']:fail('SELF_APPROVAL','申请人不能审批自己的申请',403)
        if row['status']!='pending':fail('ALREADY_REVIEWED','该申请已处理')
        doc_id=None
        if body['decision']=='approve':
            delta=row['delta'];doc=None
            if row['kind']=='reversal':doc,delta=original(c,actor,row['original_id'])
            stock=balance(c,actor,row['warehouse_id'],row['sku_id'])
            doc_id=movement(c,actor,stock,delta,'reversal' if doc else 'adjustment',row['original_id'] or approval_id,row['reason'],row['expected_version'])
            if doc:
                c.execute(insert(reversals).values(original_id=doc['id'],tenant_id=actor['tenant_id'],reversal_id=doc_id,approval_id=approval_id))
                if doc['kind']=='purchase_receipt':
                    order=get_order(c,actor,doc['source'])
                    line=c.execute(select(lines).where(lines.c.order_id==order['id'],lines.c.sku_id==doc['sku_id'])).mappings().one()
                    c.execute(update(lines).where(lines.c.id==line['id']).values(received=line['received']-doc['quantity']))
                    all_lines=list(c.execute(select(lines).where(lines.c.order_id==order['id'])).mappings())
                    status='closed' if order['status']=='closed' else ('partial' if any(x['received'] for x in all_lines) else 'open')
                    c.execute(update(orders).where(orders.c.id==order['id']).values(status=status,version=order['version']+1))
        status='approved' if body['decision']=='approve' else 'rejected'
        c.execute(update(approvals).where(approvals.c.id==approval_id).values(status=status,reviewer_id=actor['id'],review_note=body['note'],reviewed_at=now(),document_id=doc_id))
        return {'approval_id':approval_id,'status':status,'document_id':doc_id}
    return command(engine,actor,'approval.review:'+approval_id,body,key,apply)
