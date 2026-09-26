from typing import Annotated, Literal
from fastapi import Request, Header
from fastapi.responses import Response
from pydantic import Field, model_validator
from sqlalchemy import select
from .main import Strict, Text, Qty
from . import workflows as w
from .domain import require_role

Key=Annotated[str,Header(min_length=8,max_length=120)]
Positive=Annotated[int,Field(strict=True,ge=1,le=1_000_000)]
Reason=Annotated[str,Field(min_length=2,max_length=500)]
class ImportPreview(Strict):
    kind:Literal['skus','warehouses','opening']
    csv_text:Annotated[str,Field(min_length=1,max_length=1_000_000)]
class PurchaseLine(Strict):
    sku_id:Text
    quantity:Positive
class Purchase(Strict):
    warehouse_id:Text
    code:Text
    supplier:Text
    note:Annotated[str,Field(max_length=500)]=''
    lines:Annotated[list[PurchaseLine],Field(min_length=1,max_length=100)]
    @model_validator(mode='after')
    def unique(self):
        if len({l.sku_id for l in self.lines})!=len(self.lines):raise ValueError('每个商品只能有一行')
        return self
class ReceiveLine(Strict):
    sku_id:Text
    expected_version:Qty
    g:Qty
    q:Qty=0
    d:Qty=0
    @model_validator(mode='after')
    def total(self):
        if not self.g+self.q+self.d:raise ValueError('收货数量必须大于零')
        return self
class Receive(Strict):
    reference:Text
    expected_version:Qty
    lines:Annotated[list[ReceiveLine],Field(min_length=1,max_length=100)]
    @model_validator(mode='after')
    def unique(self):
        if len({l.sku_id for l in self.lines})!=len(self.lines):raise ValueError('每个商品只能有一行')
        return self
class Close(Strict):
    expected_version:Qty
    reason:Reason
Signed=Annotated[int,Field(strict=True,ge=-1_000_000,le=1_000_000)]
class Delta(Strict):
    g:Signed=0
    q:Signed=0
    d:Signed=0
    @model_validator(mode='after')
    def nonzero(self):
        if not any((self.g,self.q,self.d)):raise ValueError('调整数量不能全部为零')
        return self
class Adjustment(Strict):
    kind:Literal['adjustment']
    warehouse_id:Text
    sku_id:Text
    expected_version:Qty
    delta:Delta
    reason:Reason
class Reversal(Strict):
    kind:Literal['reversal']
    original_id:Text
    expected_version:Qty
    reason:Reason
class Review(Strict):
    decision:Literal['approve','reject']
    note:Reason

def install(app,engine,actor_for,scope):
    @app.get('/api/v1/imports/template/{kind}')
    def template(kind:str,request:Request):
        actor,_=actor_for(request);require_role(actor,'admin')
        if kind not in w.CSV_FIELDS:w.fail('NOT_FOUND','模板不存在',404)
        return Response('\ufeff'+','.join(w.CSV_FIELDS[kind])+'\n',media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="{kind}.csv"'})
    @app.get('/api/v1/imports')
    def batches(request:Request):
        actor,_=actor_for(request);require_role(actor,'admin')
        with engine.connect() as c:return [dict(r) for r in c.execute(select(w.imports).where(w.imports.c.tenant_id==actor['tenant_id']).order_by(w.imports.c.created_at.desc()).limit(100)).mappings()]
    @app.post('/api/v1/imports/preview')
    def preview(body:ImportPreview,request:Request):
        actor,_=actor_for(request,True)
        return w.import_preview(engine,actor,body.model_dump())
    @app.post('/api/v1/imports/{batch_id}/apply')
    def apply_import(batch_id:str,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True)
        return w.import_apply(engine,actor,batch_id,idempotency_key)
    @app.get('/api/v1/purchases')
    def purchases(request:Request):
        actor,_=actor_for(request);require_role(actor,'admin','warehouse')
        with engine.connect() as c:return [w.order_view(c,dict(r)) for r in c.execute(scope(actor,w.orders).order_by(w.orders.c.created_at.desc()).limit(100)).mappings()]
    @app.post('/api/v1/purchases')
    def new_purchase(body:Purchase,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True)
        return w.purchase_create(engine,actor,body.model_dump(),idempotency_key)
    @app.post('/api/v1/purchases/{order_id}/receive')
    def receive(order_id:str,body:Receive,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True)
        return w.purchase_receive(engine,actor,order_id,body.model_dump(),idempotency_key)
    @app.post('/api/v1/purchases/{order_id}/close')
    def close(order_id:str,body:Close,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True)
        return w.purchase_close(engine,actor,order_id,body.model_dump(),idempotency_key)
    @app.get('/api/v1/approvals')
    def approvals(request:Request):
        actor,_=actor_for(request);require_role(actor,'admin','warehouse')
        with engine.connect() as c:
            names={r['id']:r['name'] for r in c.execute(select(w.db.users).where(w.db.users.c.tenant_id==actor['tenant_id'])).mappings()}
            return [{**dict(r),'requester_name':names.get(r['requester_id'],r['requester_id']),'reviewer_name':names.get(r['reviewer_id']),'is_own':r['requester_id']==actor['id']} for r in c.execute(scope(actor,w.approvals).order_by(w.approvals.c.created_at.desc()).limit(100)).mappings()]
    @app.post('/api/v1/approvals')
    def request_approval(body:Annotated[Adjustment|Reversal,Field(discriminator='kind')],request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True)
        return w.approval_request(engine,actor,body.model_dump(),idempotency_key)
    @app.post('/api/v1/approvals/{approval_id}/review')
    def review(approval_id:str,body:Review,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True)
        return w.approval_review(engine,actor,approval_id,body.model_dump(),idempotency_key)
