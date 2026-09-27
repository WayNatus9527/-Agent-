import csv
import io
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal
from fastapi import FastAPI, Request, Response, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, insert, delete
from sqlalchemy.exc import IntegrityError, OperationalError
from . import db
from .domain import BusinessError, require_role, get_warehouse, stock_action, available, unallocated, uid
from .security import verify_password, token_hash

Text = Annotated[str, Field(min_length=1, max_length=120)]
Qty = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid',str_strip_whitespace=True)
class Login(Strict):
    username: Text
    password: Annotated[str,Field(min_length=1,max_length=256)]
class StockBase(Strict):
    warehouse_id: Text
    sku_id: Text
    expected_version: Qty
    source: Text
    note: Annotated[str,Field(max_length=500)] = ''
class Receipt(StockBase):
    g: Qty
    q: Qty = 0
    d: Qty = 0
    @model_validator(mode='after')
    def total(self):
        if self.g+self.q+self.d == 0:raise ValueError('实收数量必须大于0')
        return self
class Sale(StockBase):
    quantity: Annotated[int,Field(strict=True,ge=1,le=1_000_000)]
class SKU(Strict):
    code: Text
    name: Text
    spec: Text
    unit: Text = '件'
    barcode: Annotated[str,Field(max_length=120)] = ''
class Warehouse(Strict):
    code: Text
    name: Text
    country: Annotated[str,Field(pattern=r'^[A-Z]{2}$')] = 'CN'
    timezone: Text = 'Asia/Shanghai'
    authority: Literal['local','manual'] = 'local'
    @model_validator(mode='after')
    def valid_timezone(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:raise ValueError('时区须为有效IANA时区')
        return self

def create_app(engine=None):
    engine = engine or db.make_engine()
    @asynccontextmanager
    async def lifespan(app):
        from .migrations import migrate
        migrate(engine)
        yield
    app = FastAPI(title='义乌商贸库存管家',version='0.3.0',lifespan=lifespan)
    app.state.engine = engine
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['127.0.0.1','localhost','testserver'])
    attempts = defaultdict(deque)
    attempt_lock = Lock()

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):
        # Validation input can contain passwords; return only field diagnostics.
        detail=[{k:e[k] for k in ('loc','msg','type')} for e in exc.errors()]
        return JSONResponse({'detail':detail},status_code=422)

    @app.exception_handler(BusinessError)
    async def business_error(request,exc):
        return JSONResponse({'error':{'code':exc.code,'message':exc.message}},status_code=exc.status)
    @app.exception_handler(IntegrityError)
    async def integrity_error(request,exc):
        return JSONResponse({'error':{'code':'DUPLICATE','message':'编码已存在，请检查后重试'}},status_code=409)
    @app.exception_handler(OperationalError)
    async def busy_error(request,exc):
        return JSONResponse({'error':{'code':'DATABASE_UNAVAILABLE','message':'数据库暂不可用，请保留原请求编号重试'}},status_code=503)
    @app.middleware('http')
    async def security_headers(request,call_next):
        if request.method in ('POST','PUT','PATCH','DELETE'):
            origin = request.headers.get('origin')
            expected = str(request.base_url).rstrip('/')
            if origin and origin != expected:
                return JSONResponse({'error':{'code':'ORIGIN_REJECTED','message':'请求来源不允许'}},status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith('/api'):response.headers['Cache-Control']='no-store'
        return response

    def actor_for(request, write=False):
        token = request.cookies.get('inventory_session','')
        with engine.connect() as c:
            sess = c.execute(select(db.sessions).where(db.sessions.c.token_hash==token_hash(token),db.sessions.c.expires>int(time.time()))).mappings().first()
            if not sess:raise BusinessError('UNAUTHENTICATED','请先登录',401)
            actor = c.execute(select(db.users).where(db.users.c.id==sess['user_id'])).mappings().first()
        if not actor:raise BusinessError('UNAUTHENTICATED','账号不存在',401)
        from .accounts import active
        with engine.connect() as c:
            if not active(c,actor['id']):raise BusinessError('UNAUTHENTICATED','账号已停用',401)
        if write and not secrets.compare_digest(request.headers.get('x-csrf-token',''),sess['csrf']):
            raise BusinessError('CSRF_REJECTED','操作凭证失效，请刷新页面',403)
        return dict(actor),sess['csrf']

    @app.post('/api/v1/session')
    def login(body:Login,request:Request,response:Response):
        address=request.client.host if request.client else 'unknown'
        with attempt_lock:
            queue=attempts[address]
            while queue and queue[0]<time.time()-60:queue.popleft()
            if len(queue)>=10:raise BusinessError('RATE_LIMITED','尝试过于频繁，请一分钟后重试',429)
            queue.append(time.time())
        with engine.connect() as c:
            actor=c.execute(select(db.users).where(db.users.c.username==body.username)).mappings().first()
        from .accounts import active
        with engine.connect() as c:enabled=bool(actor) and active(c,actor['id'])
        if not enabled or not verify_password(body.password,actor['password_hash']):
            raise BusinessError('LOGIN_FAILED','账号或密码错误',401)
        token,csrf=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        with db.write(engine) as c:
            c.execute(delete(db.sessions).where(db.sessions.c.expires<int(time.time())))
            c.execute(delete(db.sessions).where(db.sessions.c.token_hash==token_hash(request.cookies.get('inventory_session',''))))
            c.execute(insert(db.sessions).values(token_hash=token_hash(token),user_id=actor['id'],csrf=csrf,expires=int(time.time())+8*3600))
        response.set_cookie('inventory_session',token,httponly=True,samesite='strict',secure=os.getenv('COOKIE_SECURE')=='1',max_age=8*3600)
        return {'id':actor['id'],'name':actor['name'],'role':actor['role'],'csrf':csrf}

    @app.get('/api/v1/session')
    def session(request:Request):
        actor,csrf=actor_for(request)
        return {'id':actor['id'],'name':actor['name'],'role':actor['role'],'csrf':csrf}
    @app.delete('/api/v1/session')
    def logout(request:Request,response:Response):
        actor_for(request,True)
        with db.write(engine) as c:c.execute(delete(db.sessions).where(db.sessions.c.token_hash==token_hash(request.cookies.get('inventory_session',''))))
        response.delete_cookie('inventory_session')
        return {'ok':True}

    @app.get('/api/v1/bootstrap')
    def bootstrap(request:Request):
        actor,_=actor_for(request)
        with engine.connect() as c:
            ws=select(db.warehouses).where(db.warehouses.c.tenant_id==actor['tenant_id'])
            if actor['role']!='admin':ws=ws.where(db.warehouses.c.id.in_(actor['warehouse_ids']))
            return {'tenant':c.execute(select(db.tenants.c.name).where(db.tenants.c.id==actor['tenant_id'])).scalar_one(),
                    'warehouses':[dict(x) for x in c.execute(ws).mappings()],
                    'skus':[dict(x) for x in c.execute(select(db.skus).where(db.skus.c.tenant_id==actor['tenant_id']).order_by(db.skus.c.code)).mappings()],
                    'connectors':[{'name':'跨境销售渠道','status':'not_configured'},{'name':'第三方海外仓','status':'not_configured'}]}

    def scope(actor,table):
        query=select(table).where(table.c.tenant_id==actor['tenant_id'])
        if actor['role']!='admin':query=query.where(table.c.warehouse_id.in_(actor['warehouse_ids']))
        return query
    @app.get('/api/v1/inventory')
    def inventory(request:Request,warehouse_id:str|None=None):
        actor,_=actor_for(request)
        with engine.connect() as c:
            query=scope(actor,db.balances)
            if warehouse_id:
                get_warehouse(c,actor,warehouse_id)
                query=query.where(db.balances.c.warehouse_id==warehouse_id)
            return [{**dict(r),'available':available(r),'unallocated':unallocated(r),
                     'offline_available':min(max(0,available(r)),r['offline']+max(0,unallocated(r)))} for r in c.execute(query).mappings()]
    @app.get('/api/v1/ledger')
    def history(request:Request,warehouse_id:str|None=None,limit:int=100,offset:int=0):
        actor,_=actor_for(request)
        if not 1<=limit<=200 or offset<0:raise BusinessError('INVALID_PAGE','分页参数无效',422)
        with engine.connect() as c:
            query=scope(actor,db.ledger)
            if warehouse_id:
                get_warehouse(c,actor,warehouse_id)
                query=query.where(db.ledger.c.warehouse_id==warehouse_id)
            return [dict(x) for x in c.execute(query.order_by(db.ledger.c.created_at.desc(),db.ledger.c.id).limit(limit).offset(offset)).mappings()]
    @app.get('/api/v1/documents')
    def documents(request:Request):
        actor,_=actor_for(request)
        with engine.connect() as c:return [dict(x) for x in c.execute(scope(actor,db.documents).order_by(db.documents.c.created_at.desc()).limit(100)).mappings()]

    @app.post('/api/v1/receipts/confirm')
    def receive(body:Receipt,request:Request,idempotency_key:Annotated[str,Header(min_length=8,max_length=120)]):
        actor,_=actor_for(request,True)
        return stock_action(engine,actor,'receipt',body.model_dump(),idempotency_key)
    @app.post('/api/v1/sales/confirm')
    def sell(body:Sale,request:Request,idempotency_key:Annotated[str,Header(min_length=8,max_length=120)]):
        actor,_=actor_for(request,True)
        return stock_action(engine,actor,'sale',body.model_dump(),idempotency_key)
    @app.post('/api/v1/skus',status_code=201)
    def new_sku(body:SKU,request:Request,idempotency_key:Annotated[str,Header(min_length=8,max_length=120)]):
        actor,_=actor_for(request,True);require_role(actor,'admin')
        from .workflows import catalog_create
        return catalog_create(engine,actor,'skus',body.model_dump(),idempotency_key)
    @app.post('/api/v1/warehouses',status_code=201)
    def new_warehouse(body:Warehouse,request:Request,idempotency_key:Annotated[str,Header(min_length=8,max_length=120)]):
        actor,_=actor_for(request,True);require_role(actor,'admin')
        from .workflows import catalog_create
        return catalog_create(engine,actor,'warehouses',body.model_dump(),idempotency_key)
    @app.get('/api/v1/inventory/export')
    def export(request:Request):
        rows=inventory(request)
        actor,_=actor_for(request)
        with engine.connect() as c:
            products={s['id']:s for s in c.execute(select(db.skus).where(db.skus.c.tenant_id==actor['tenant_id'])).mappings()}
            whs={w['id']:w for w in c.execute(select(db.warehouses).where(db.warehouses.c.tenant_id==actor['tenant_id'])).mappings()}
        out=io.StringIO();writer=csv.writer(out)
        writer.writerow(['SKU','商品','规格','仓库','良品','待验','残次','订单占用','调拨占用','冻结','安全量','可分配','线下可用','更新于UTC'])
        def safe(value):
            value=str(value)
            return "'"+value if value.lstrip().startswith(('=','+','-','@')) else value
        for r in rows:
            p=products[r['sku_id']]
            writer.writerow([safe(x) for x in [p['code'],p['name'],p['spec'],whs[r['warehouse_id']]['name'],r['g'],r['q'],r['d'],r['r'],r['t'],r['h'],r['b'],r['available'],r['offline_available'],r['updated_at']]])
        return Response('\ufeff'+out.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="inventory.csv"'})
    @app.get('/health')
    def health():return {'status':'ok','version':'0.3.0','environment':'local-development'}
    from .phase2 import install
    install(app,engine,actor_for,scope)
    from .phase3 import install as install_phase3
    install_phase3(app,engine,actor_for,scope)
    static=Path(__file__).parent/'static'
    app.mount('/static',StaticFiles(directory=static),name='static')
    @app.get('/')
    def index():return FileResponse(static/'index.html')
    return app

app=create_app()
