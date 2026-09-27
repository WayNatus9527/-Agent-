"""Physical transfers, returns and quality inspection with transactional audit."""
from sqlalchemy import Table, Column as C, String as S, Integer as I, JSON, UniqueConstraint as U, CheckConstraint as CK, select, insert, update, func, or_
from . import db
from . import workflows as w
from .domain import require_role, get_warehouse, get_sku, uid, now

transfers=Table('transfers',db.metadata,C('id',S,primary_key=True),C('tenant_id',S,nullable=False),C('code',S),C('warehouse_id',S),C('destination_id',S),C('status',S),C('version',I),C('reason',S),C('actor_id',S),C('created_at',S),U('tenant_id','code'))
transfer_lines=Table('transfer_lines',db.metadata,C('id',S,primary_key=True),C('transfer_id',S,nullable=False),C('sku_id',S),C('quantity',I),C('sent',I),C('received',I),C('lost',I),U('transfer_id','sku_id'),CK('quantity > 0 AND sent >= 0 AND received >= 0 AND lost >= 0 AND sent <= quantity AND received + lost <= sent'))
transfer_events=Table('transfer_events',db.metadata,C('id',S,primary_key=True),C('transfer_id',S,nullable=False),C('kind',S),C('reference',S),C('items',JSON),C('document_ids',JSON),C('actor_id',S),C('created_at',S),U('transfer_id','kind','reference'))
claims=Table('transfer_claims',db.metadata,C('id',S,primary_key=True),C('tenant_id',S),C('transfer_id',S),C('expected_version',I),C('reason',S),C('requester_id',S),C('reviewer_id',S),C('note',S),C('status',S),C('created_at',S),C('reviewed_at',S))
returns=Table('return_orders',db.metadata,C('id',S,primary_key=True),C('tenant_id',S,nullable=False),C('warehouse_id',S),C('sku_id',S),C('kind',S),C('reference',S),C('original_id',S),C('quantity',I),C('good',I),C('bad',I),C('status',S),C('version',I),C('reason',S),C('actor_id',S),C('document_id',S),C('created_at',S),U('tenant_id','kind','reference'),CK('quantity > 0 AND good >= 0 AND bad >= 0 AND good + bad <= quantity'))
quality=Table('quality_events',db.metadata,C('id',S,primary_key=True),C('tenant_id',S),C('warehouse_id',S),C('sku_id',S),C('return_id',S),C('reference',S),C('good',I),C('bad',I),C('reason',S),C('document_id',S),C('actor_id',S),C('created_at',S),U('tenant_id','reference'))

def quality_hold(c,tenant,warehouse,sku):
    return c.scalar(select(func.coalesce(func.sum(returns.c.quantity-returns.c.good-returns.c.bad),0)).where(returns.c.tenant_id==tenant,returns.c.warehouse_id==warehouse,returns.c.sku_id==sku,returns.c.kind=='customer'))

def protect_pending_quality(c,actor,row):
    if row['q']<quality_hold(c,actor['tenant_id'],row['warehouse_id'],row['sku_id']):w.fail('QUALITY_RESERVED','待验库存含未完成退货质检，请先在对应退货单处理')

def get_transfer(c,actor,tid,side=None):
    row=c.execute(select(transfers).where(transfers.c.id==tid,transfers.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not row:w.fail('NOT_FOUND','调拨单不存在',404)
    if side:get_warehouse(c,actor,row['warehouse_id'] if side=='source' else row['destination_id'],True)
    elif actor['role']!='admin' and not set((row['warehouse_id'],row['destination_id'])) & set(actor['warehouse_ids']):w.fail('NOT_FOUND','调拨单不存在或未授权',404)
    return dict(row)

def transfer_view(c,row):
    result={**row,'lines':[dict(x) for x in c.execute(select(transfer_lines).where(transfer_lines.c.transfer_id==row['id'])).mappings()],
            'events':[dict(x) for x in c.execute(select(transfer_events).where(transfer_events.c.transfer_id==row['id']).order_by(transfer_events.c.created_at)).mappings()],
            'claims':[dict(x) for x in c.execute(select(claims).where(claims.c.transfer_id==row['id']).order_by(claims.c.created_at)).mappings()]}
    for line in result['lines']:line['in_transit']=line['sent']-line['received']-line['lost']
    return result

def event(c,actor,tid,kind,reference,items,docs):
    c.execute(insert(transfer_events).values(id=uid(),transfer_id=tid,kind=kind,reference=reference,items=items,document_ids=docs,actor_id=actor['id'],created_at=now()))

def transfer_create(engine,actor,body,key):
    require_role(actor,'admin','warehouse')
    def apply(c):
        get_warehouse(c,actor,body['warehouse_id'],True)
        get_warehouse(c,{**actor,'role':'admin'},body['destination_id'],True)
        if body['warehouse_id']==body['destination_id']:w.fail('SAME_WAREHOUSE','调出仓和调入仓必须不同',422)
        row={k:v for k,v in body.items() if k!='lines'}
        row.update(id=uid(),tenant_id=actor['tenant_id'],status='draft',version=1,actor_id=actor['id'],created_at=now())
        c.execute(insert(transfers).values(**row))
        for item in body['lines']:
            get_sku(c,actor,item['sku_id'])
            c.execute(insert(transfer_lines).values(id=uid(),transfer_id=row['id'],sku_id=item['sku_id'],quantity=item['quantity'],sent=0,received=0,lost=0))
        event(c,actor,row['id'],'create',row['code'],body['lines'],[])
        return transfer_view(c,row)
    return w.command(engine,actor,'transfer.create',body,key,apply)

def transfer_action(engine,actor,tid,action,body,key):
    require_role(actor,'admin','warehouse')
    def apply(c):
        row=get_transfer(c,actor,tid,'destination' if action=='receive' else 'source')
        if row['version']!=body['expected_version']:w.fail('VERSION_CONFLICT','调拨单已变化，请刷新后重试')
        items=list(c.execute(select(transfer_lines).where(transfer_lines.c.transfer_id==tid)).mappings());docs=[]
        if action in ('reserve','ship','cancel'):
            allowed={'reserve':['draft'],'ship':['reserved'],'cancel':['draft','reserved']}[action]
            if row['status'] not in allowed:w.fail('INVALID_STATE','当前状态不能执行此操作')
            versions={l['sku_id']:l['expected_version'] for l in body['lines']}
            if action!='cancel' or row['status']=='reserved':
                if set(versions)!={l['sku_id'] for l in items}:w.fail('INVALID_LINES','必须包含所有商品的库存版本',422)
                for line in items:
                    stock=w.balance(c,actor,row['warehouse_id'],line['sku_id']);n=line['quantity']
                    delta={'t':n} if action=='reserve' else ({'t':-n,'g':-n} if action=='ship' else {'t':-n})
                    docs.append(w.movement(c,actor,stock,delta,'transfer_'+action,tid,body['reason'],versions[line['sku_id']]))
                    if action=='ship':c.execute(update(transfer_lines).where(transfer_lines.c.id==line['id']).values(sent=n))
            status={'reserve':'reserved','ship':'in_transit','cancel':'cancelled'}[action]
            event(c,actor,tid,action,action,[dict(x) for x in items],docs)
        else:
            if row['status'] not in ('in_transit','partial'):w.fail('INVALID_STATE','调拨单不在待签收状态')
            if c.execute(select(transfer_events.c.id).where(transfer_events.c.transfer_id==tid,transfer_events.c.kind=='receive',transfer_events.c.reference==body['reference'])).first():w.fail('RECEIPT_EXISTS','该签收凭证已登记')
            by_sku={x['sku_id']:x for x in items}
            for item in body['lines']:
                line=by_sku.get(item['sku_id']);n=item['g']+item['q']+item['d']
                if not line:w.fail('NOT_FOUND','商品不在调拨单中',404)
                if n>line['sent']-line['received']-line['lost']:w.fail('OVER_RECEIPT','签收数量超过剩余在途数量')
                stock=w.balance(c,actor,row['destination_id'],item['sku_id'])
                docs.append(w.movement(c,actor,stock,{f:item[f] for f in ('g','q','d')},'transfer_receive',tid,body['reference'],item['expected_version']))
                c.execute(update(transfer_lines).where(transfer_lines.c.id==line['id']).values(received=line['received']+n))
            fresh=list(c.execute(select(transfer_lines).where(transfer_lines.c.transfer_id==tid)).mappings())
            status='completed' if all(l['received']==l['sent'] for l in fresh) else 'partial'
            event(c,actor,tid,'receive',body['reference'],body['lines'],docs)
        c.execute(update(transfers).where(transfers.c.id==tid).values(status=status,version=row['version']+1))
        return {'id':tid,'status':status,'version':row['version']+1,'document_ids':docs}
    return w.command(engine,actor,'transfer.'+action+':'+tid,body,key,apply)

def discrepancy_request(engine,actor,tid,body,key):
    require_role(actor,'admin','warehouse')
    def apply(c):
        row=get_transfer(c,actor,tid)
        if row['version']!=body['expected_version']:w.fail('VERSION_CONFLICT','调拨单已变化')
        if row['status'] not in ('in_transit','partial'):w.fail('INVALID_STATE','无待处理在途差异')
        if c.execute(select(claims.c.id).where(claims.c.transfer_id==tid,claims.c.status=='pending')).first():w.fail('PENDING_CLAIM','已有待审核差异申请')
        claim=dict(id=uid(),tenant_id=actor['tenant_id'],transfer_id=tid,expected_version=row['version'],reason=body['reason'],requester_id=actor['id'],reviewer_id=None,note=None,status='pending',created_at=now(),reviewed_at=None)
        c.execute(insert(claims).values(**claim));return claim
    return w.command(engine,actor,'transfer.discrepancy:'+tid,body,key,apply)

def discrepancy_review(engine,actor,cid,body,key):
    require_role(actor,'admin')
    def apply(c):
        claim=c.execute(select(claims).where(claims.c.id==cid,claims.c.tenant_id==actor['tenant_id'])).mappings().first()
        if not claim:w.fail('NOT_FOUND','差异申请不存在',404)
        if claim['requester_id']==actor['id']:w.fail('SELF_APPROVAL','申请人不能审核自己的差异',403)
        if claim['status']!='pending':w.fail('ALREADY_REVIEWED','差异申请已处理')
        row=get_transfer(c,actor,claim['transfer_id'])
        if body['decision']=='approve':
            if row['version']!=claim['expected_version']:w.fail('VERSION_CONFLICT','调拨已有新签收，请驳回旧申请后重新核对')
            if row['status'] not in ('in_transit','partial'):w.fail('INVALID_STATE','调拨单已完成')
            changes=[]
            for line in c.execute(select(transfer_lines).where(transfer_lines.c.transfer_id==row['id'])).mappings():
                loss=line['sent']-line['received'];changes.append({'sku_id':line['sku_id'],'lost':loss})
                c.execute(update(transfer_lines).where(transfer_lines.c.id==line['id']).values(lost=loss))
            event(c,actor,row['id'],'loss',cid,changes,[])
            c.execute(update(transfers).where(transfers.c.id==row['id']).values(status='closed_difference',version=row['version']+1))
        status='approved' if body['decision']=='approve' else 'rejected'
        c.execute(update(claims).where(claims.c.id==cid).values(status=status,reviewer_id=actor['id'],note=body['note'],reviewed_at=now()))
        return {'id':cid,'status':status}
    return w.command(engine,actor,'claim.review:'+cid,body,key,apply)

def return_create(engine,actor,body,key):
    require_role(actor,'admin','warehouse')
    def apply(c):
        original=c.execute(select(db.documents).where(db.documents.c.id==body['original_id'],db.documents.c.tenant_id==actor['tenant_id'])).mappings().first()
        if not original:w.fail('NOT_FOUND','原业务单据不存在',404)
        if original['kind'] not in (('sale',) if body['kind']=='customer' else ('receipt','purchase_receipt')):w.fail('INVALID_ORIGINAL','客户退货须关联交货单；退供应商须关联收货单')
        if c.execute(select(w.reversals).where(w.reversals.c.original_id==original['id'])).first():w.fail('ALREADY_REVERSED','原单已冲正，不能据此退货')
        n=body['quantity'] if body['kind']=='customer' else sum(body[f] for f in ('g','q','d'))
        returned=c.scalar(select(func.coalesce(func.sum(returns.c.quantity),0)).where(returns.c.original_id==original['id'],returns.c.tenant_id==actor['tenant_id']))
        if returned+n>original['quantity']:w.fail('OVER_RETURN','累计退货超过原单数量')
        stock=w.balance(c,actor,original['warehouse_id'],original['sku_id'])
        row=dict(id=uid(),tenant_id=actor['tenant_id'],warehouse_id=original['warehouse_id'],sku_id=original['sku_id'],kind=body['kind'],reference=body['reference'],original_id=original['id'],quantity=n,good=0,bad=0,status='awaiting_quality' if body['kind']=='customer' else 'completed',version=1,reason=body['reason'],actor_id=actor['id'],document_id=None,created_at=now())
        c.execute(insert(returns).values(**row))
        delta={'q':n} if body['kind']=='customer' else {f:-body[f] for f in ('g','q','d')}
        doc=w.movement(c,actor,stock,delta,'customer_return' if body['kind']=='customer' else 'supplier_return',row['id'],body['reason'],body['expected_version'])
        c.execute(update(returns).where(returns.c.id==row['id']).values(document_id=doc))
        return {**row,'document_id':doc}
    return w.command(engine,actor,'return.create',body,key,apply)

def inspect_quality(engine,actor,body,key):
    require_role(actor,'admin','warehouse')
    def apply(c):
        n=body['good']+body['bad'];rid=body.get('return_id')
        if rid:
            ret=c.execute(select(returns).where(returns.c.id==rid,returns.c.tenant_id==actor['tenant_id'])).mappings().first()
            if not ret:w.fail('NOT_FOUND','退货单不存在',404)
            if ret['kind']!='customer' or n>ret['quantity']-ret['good']-ret['bad']:w.fail('OVER_INSPECTION','质检数量超过该退货单剩余待验量')
            warehouse_id,sku_id=ret['warehouse_id'],ret['sku_id']
            if ret['version']!=body['return_version']:w.fail('VERSION_CONFLICT','退货单已变化')
            c.execute(update(returns).where(returns.c.id==rid).values(good=ret['good']+body['good'],bad=ret['bad']+body['bad'],version=ret['version']+1,status='completed' if n==ret['quantity']-ret['good']-ret['bad'] else 'awaiting_quality'))
        else:warehouse_id,sku_id=body['warehouse_id'],body['sku_id']
        stock=w.balance(c,actor,warehouse_id,sku_id)
        doc=w.movement(c,actor,stock,{'q':-n,'g':body['good'],'d':body['bad']},'quality',rid or body['reference'],body['reason'],body['expected_version'])
        row=dict(id=uid(),tenant_id=actor['tenant_id'],warehouse_id=warehouse_id,sku_id=sku_id,return_id=rid,reference=body['reference'],good=body['good'],bad=body['bad'],reason=body['reason'],document_id=doc,actor_id=actor['id'],created_at=now())
        c.execute(insert(quality).values(**row));return row
    return w.command(engine,actor,'quality.inspect',body,key,apply)
