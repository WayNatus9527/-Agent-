from typing import Annotated, Literal
from pathlib import Path
from uuid import uuid4
from fastapi import Request
from pydantic import Field, SecretStr, model_validator
from sqlalchemy import select, or_, func
from .main import Strict, Text, Qty
from .phase2 import Key, Positive, Reason, PurchaseLine, Receive, Close, Review
from . import db, operations as op, accounts as ac, workflows as w
from .domain import require_role, available, unallocated

class Transfer(Strict):
    code:Text
    warehouse_id:Text
    destination_id:Text
    reason:Reason
    lines:Annotated[list[PurchaseLine],Field(min_length=1,max_length=100)]
    @model_validator(mode='after')
    def unique(self):
        if len({x.sku_id for x in self.lines})!=len(self.lines):raise ValueError('商品行不可重复')
        return self
class StockVersion(Strict):
    sku_id:Text
    expected_version:Qty
class TransferAction(Strict):
    expected_version:Qty
    reason:Reason
    lines:Annotated[list[StockVersion],Field(max_length=100)]=[]
    @model_validator(mode='after')
    def unique(self):
        if len({x.sku_id for x in self.lines})!=len(self.lines):raise ValueError('商品行不可重复')
        return self
class CustomerReturn(Strict):
    kind:Literal['customer']
    original_id:Text
    reference:Text
    quantity:Positive
    expected_version:Qty
    reason:Reason
class SupplierReturn(Strict):
    kind:Literal['supplier']
    original_id:Text
    reference:Text
    g:Qty=0
    q:Qty=0
    d:Qty=0
    expected_version:Qty
    reason:Reason
    @model_validator(mode='after')
    def total(self):
        if not self.g+self.q+self.d:raise ValueError('退货数量必须大于零')
        return self
class Inspection(Strict):
    reference:Text
    return_id:Text|None=None
    return_version:Qty|None=None
    warehouse_id:Text|None=None
    sku_id:Text|None=None
    good:Qty=0
    bad:Qty=0
    expected_version:Qty
    reason:Reason
    @model_validator(mode='after')
    def scope(self):
        if self.good+self.bad==0:raise ValueError('质检数量必须大于零')
        if self.return_id:
            if self.return_version is None or self.warehouse_id or self.sku_id:raise ValueError('退货质检需要退货版本，不能指定其他仓库或商品')
        elif not self.warehouse_id or not self.sku_id or self.return_version is not None:raise ValueError('普通质检需要仓库与商品')
        return self
class AccountBase(Strict):
    name:Text
    role:Literal['admin','warehouse','cashier']
    warehouse_ids:Annotated[list[Text],Field(max_length=100)]=[]
    @model_validator(mode='after')
    def unique(self):
        if len(set(self.warehouse_ids))!=len(self.warehouse_ids):raise ValueError('仓库授权不可重复')
        return self
class NewAccount(AccountBase):
    username:Annotated[str,Field(pattern=r'^[a-zA-Z0-9_-]{3,40}$')]
    password:Annotated[SecretStr,Field(min_length=12,max_length=256)]
class EditAccount(AccountBase):
    active:bool
    expected_version:Qty

def install(app,engine,actor_for,scope):
    @app.get('/api/v1/transfer-targets')
    def targets(request:Request):
        actor,_=actor_for(request);require_role(actor,'admin','warehouse')
        with engine.connect() as c:return [dict(x) for x in c.execute(select(db.warehouses.c.id,db.warehouses.c.name,db.warehouses.c.code).where(db.warehouses.c.tenant_id==actor['tenant_id'],db.warehouses.c.authority=='local')).mappings()]
    @app.get('/api/v1/transfers')
    def transfers(request:Request):
        actor,_=actor_for(request);require_role(actor,'admin','warehouse')
        query=select(op.transfers).where(op.transfers.c.tenant_id==actor['tenant_id'])
        if actor['role']!='admin':query=query.where(or_(op.transfers.c.warehouse_id.in_(actor['warehouse_ids']),op.transfers.c.destination_id.in_(actor['warehouse_ids'])))
        with engine.connect() as c:return [op.transfer_view(c,dict(x)) for x in c.execute(query.order_by(op.transfers.c.created_at.desc()).limit(100)).mappings()]
    @app.post('/api/v1/transfers')
    def create_transfer(body:Transfer,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return op.transfer_create(engine,actor,body.model_dump(),idempotency_key)
    @app.post('/api/v1/transfers/{tid}/receive')
    def receive(tid:str,body:Receive,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return op.transfer_action(engine,actor,tid,'receive',body.model_dump(),idempotency_key)
    @app.post('/api/v1/transfers/{tid}/discrepancy')
    def discrepancy(tid:str,body:Close,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return op.discrepancy_request(engine,actor,tid,body.model_dump(),idempotency_key)
    @app.post('/api/v1/transfer-claims/{cid}/review')
    def claim_review(cid:str,body:Review,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return op.discrepancy_review(engine,actor,cid,body.model_dump(),idempotency_key)
    @app.post('/api/v1/transfers/{tid}/{action}')
    def transfer_action(tid:str,action:Literal['reserve','ship','cancel'],body:TransferAction,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return op.transfer_action(engine,actor,tid,action,body.model_dump(),idempotency_key)
    @app.get('/api/v1/returns')
    def returns(request:Request):
        actor,_=actor_for(request);require_role(actor,'admin','warehouse')
        with engine.connect() as c:return [dict(x) for x in c.execute(scope(actor,op.returns).order_by(op.returns.c.created_at.desc()).limit(100)).mappings()]
    @app.post('/api/v1/returns')
    def return_create(body:Annotated[CustomerReturn|SupplierReturn,Field(discriminator='kind')],request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return op.return_create(engine,actor,body.model_dump(),idempotency_key)
    @app.get('/api/v1/quality')
    def quality(request:Request):
        actor,_=actor_for(request);require_role(actor,'admin','warehouse')
        with engine.connect() as c:return [dict(x) for x in c.execute(scope(actor,op.quality).order_by(op.quality.c.created_at.desc()).limit(100)).mappings()]
    @app.post('/api/v1/quality')
    def inspect(body:Inspection,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return op.inspect_quality(engine,actor,body.model_dump(),idempotency_key)
    @app.get('/api/v1/users')
    def users(request:Request):
        actor,_=actor_for(request);require_role(actor,'admin')
        with engine.connect() as c:return [ac.safe_user(c,x) for x in c.execute(select(db.users).where(db.users.c.tenant_id==actor['tenant_id']).order_by(db.users.c.username)).mappings()]
    @app.post('/api/v1/users')
    def new_user(body:NewAccount,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);payload=body.model_dump();payload['password']=body.password.get_secret_value()
        return ac.create_user(engine,actor,payload,idempotency_key)
    @app.post('/api/v1/users/{user_id}')
    def edit_user(user_id:str,body:EditAccount,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);return ac.edit_user(engine,actor,user_id,body.model_dump(),idempotency_key)
    @app.get('/api/v1/admin-audit')
    def admin_audit(request:Request):
        actor,_=actor_for(request);require_role(actor,'admin')
        with engine.connect() as c:return [dict(x) for x in c.execute(select(ac.audit).where(ac.audit.c.tenant_id==actor['tenant_id']).order_by(ac.audit.c.created_at.desc()).limit(100)).mappings()]
    @app.get('/api/v1/maintenance')
    def maintenance(request:Request):
        actor,_=actor_for(request);require_role(actor,'admin')
        from .migrations import versions
        with engine.connect() as c:
            rows=list(c.execute(scope(actor,db.balances)).mappings())
            invalid=[{'warehouse_id':x['warehouse_id'],'sku_id':x['sku_id']} for x in rows if available(x)<0 or unallocated(x)<0 or x['q']<op.quality_hold(c,actor['tenant_id'],x['warehouse_id'],x['sku_id'])]
            return {'database':engine.dialect.name,'schema_versions':list(c.execute(select(versions.c.version).order_by(versions.c.version)).scalars()),'inventory_issues':invalid,'balance_count':len(rows),'backup_supported':engine.dialect.name=='sqlite','external_connectors':'not_connected'}
    @app.post('/api/v1/maintenance/backup')
    def backup(request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);require_role(actor,'admin')
        if engine.dialect.name!='sqlite':w.fail('USE_PG_BACKUP','PostgreSQL请使用文档中的pg_dump/恢复演练脚本')
        from .backup import snapshot,restore
        def apply(c):
            if c.scalar(select(func.count()).select_from(db.tenants))!=1:w.fail('SINGLE_TENANT_ONLY','此本地维护工具仅适用于单商户数据库')
            # SQLite online backup reads the committed snapshot; this command has not changed business data.
            report=snapshot(engine.url.database,Path('.local/backups'))
            drill=restore(Path('.local/backups')/report['file'],Path('.local/recovery')/(uuid4().hex+'.db'))
            c.execute(insert_audit(actor,report,drill))
            return {'backup':{'file':report['file'],'sha256':report['sha256']},'restore':drill}
        return w.command(engine,actor,'maintenance.backup',{},idempotency_key,apply)

def insert_audit(actor,report,drill):
    from sqlalchemy import insert
    from .domain import uid,now
    return insert(ac.audit).values(id=uid(),tenant_id=actor['tenant_id'],actor_id=actor['id'],action='backup.restore_drill',target_id=report['file'],before={},after={'sha256':report['sha256'],'verified':drill['verified']},created_at=now())
