import hashlib
import json
import uuid
from datetime import datetime, timezone
from sqlalchemy import select, insert, update
from . import db

FIELDS = ('g','q','d','r','t','h','b','offline','online','pending','withdrawing')
def uid(): return str(uuid.uuid4())
def now(): return datetime.now(timezone.utc).isoformat()

class BusinessError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)

def require_role(actor, *roles):
    if actor['role'] not in roles:
        raise BusinessError('FORBIDDEN', '当前岗位没有此操作权限', 403)

def get_warehouse(conn, actor, warehouse_id, writable=False):
    row = conn.execute(select(db.warehouses).where(db.warehouses.c.id == warehouse_id,
                       db.warehouses.c.tenant_id == actor['tenant_id'])).mappings().first()
    if not row or (actor['role'] != 'admin' and warehouse_id not in actor['warehouse_ids']):
        raise BusinessError('NOT_FOUND', '仓库不存在或未授权', 404)
    if writable and row['authority'] != 'local':
        raise BusinessError('READ_ONLY_WAREHOUSE', '该仓为人工参考数据，尚未接入收发回执，禁止自动改账')
    return row

def get_sku(conn, actor, sku_id):
    row = conn.execute(select(db.skus).where(db.skus.c.id == sku_id, db.skus.c.tenant_id == actor['tenant_id'])).mappings().first()
    if not row: raise BusinessError('NOT_FOUND', '商品不存在', 404)
    return row

def available(row):
    return row['g']-row['r']-row['t']-row['h']-row['b']

def unallocated(row):
    return max(0, available(row))-row['offline']-row['online']-row['pending']-row['withdrawing']

def stock_action(engine, actor, kind, payload, key):
    require_role(actor, *({'receipt': ('admin','warehouse'), 'sale': ('admin','cashier')}[kind]))
    fingerprint = hashlib.sha256(json.dumps({'kind':kind,'body':payload},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    tenant = actor['tenant_id']
    with db.write(engine, tenant) as conn:
        from .accounts import assert_current
        assert_current(conn,actor)
        previous = conn.execute(select(db.requests).where(db.requests.c.tenant_id==tenant,
                    db.requests.c.actor_id==actor['id'], db.requests.c.key==key)).mappings().first()
        if previous:
            if previous['fingerprint'] != fingerprint:
                raise BusinessError('IDEMPOTENCY_CONFLICT', '此请求编号已用于不同内容，请勿修改原请求后重试')
            return {**previous['response'], 'replayed': True}
        get_warehouse(conn, actor, payload['warehouse_id'], writable=True)
        get_sku(conn, actor, payload['sku_id'])
        condition = (db.balances.c.tenant_id==tenant, db.balances.c.owner_id==tenant,
                     db.balances.c.warehouse_id==payload['warehouse_id'], db.balances.c.sku_id==payload['sku_id'])
        row = conn.execute(select(db.balances).where(*condition)).mappings().first()
        if row is None:
            row = dict(id=uid(), tenant_id=tenant, owner_id=tenant, warehouse_id=payload['warehouse_id'],
                       sku_id=payload['sku_id'], version=0, updated_at=now(), **dict.fromkeys(FIELDS,0))
            conn.execute(insert(db.balances).values(**row))
        row = dict(row)
        if row['version'] != payload['expected_version']:
            raise BusinessError('VERSION_CONFLICT', '库存已变化，请刷新库存并重新确认')
        before = {f:row[f] for f in FIELDS}
        delta = {'g':0,'q':0,'d':0}
        if kind == 'receipt':
            delta = {f:payload[f] for f in ('g','q','d')}
            for f in delta: row[f] += delta[f]
            quantity = sum(delta.values())
        else:
            quantity = payload['quantity']
            if quantity > available(row) or quantity > row['offline']+max(0,unallocated(row)):
                raise BusinessError('INSUFFICIENT_STOCK', '本仓可用或线下可用额度不足，不可消耗线上已承诺额度')
            row['offline'] -= min(row['offline'],quantity)
            row['g'] -= quantity
            delta['g'] = -quantity
        timestamp = now()
        row['version'] += 1
        row['updated_at'] = timestamp
        conn.execute(update(db.balances).where(db.balances.c.id==row['id']).values(**{f:row[f] for f in (*FIELDS,'version','updated_at')}))
        document_id = ('RCV' if kind=='receipt' else 'SAL')+'-'+uuid.uuid4().hex[:16].upper()
        conn.execute(insert(db.documents).values(id=document_id,tenant_id=tenant,kind=kind,
                     warehouse_id=row['warehouse_id'],sku_id=row['sku_id'],quantity=quantity,
                     source=payload['source'],note=payload.get('note',''),actor_id=actor['id'],created_at=timestamp,status='completed'))
        conn.execute(insert(db.ledger).values(id=uid(),tenant_id=tenant,owner_id=tenant,
                     warehouse_id=row['warehouse_id'],sku_id=row['sku_id'],document_id=document_id,
                     kind=kind,delta=delta,before=before,after={f:row[f] for f in FIELDS},actor_id=actor['id'],created_at=timestamp))
        # Transactional event record only. No connector delivery is claimed.
        conn.execute(insert(db.outbox).values(id=uid(),tenant_id=tenant,document_id=document_id,
                     event_type='inventory.changed',payload={'warehouse_id':row['warehouse_id'],'sku_id':row['sku_id'],'version':row['version']},
                     status='not_connected',created_at=timestamp))
        result = {'document_id':document_id,'kind':kind,'quantity':quantity,'version':row['version'],
                  'available':available(row),'balance':{f:row[f] for f in FIELDS},'replayed':False,'sync_status':'not_connected'}
        conn.execute(insert(db.requests).values(id=uid(),tenant_id=tenant,actor_id=actor['id'],key=key,fingerprint=fingerprint,response=result))
        return result
