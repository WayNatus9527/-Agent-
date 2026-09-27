import os
from pathlib import Path
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, insert, update, func
from app import db
from app.bootstrap import seed
from app.domain import stock_action, BusinessError, uid, FIELDS
from app.main import create_app
from app.security import hash_password

@pytest.fixture
def env(tmp_path):
    url_file=os.environ.get('TEST_DATABASE_URL_FILE')
    control=None;schema=None
    if url_file:
        url=Path(url_file).read_text().strip()
        assert url.startswith('postgresql+psycopg://')
        control=db.make_engine(url);schema='test_'+uuid4().hex
        with control.begin() as conn:conn.exec_driver_sql('CREATE SCHEMA '+schema)
        engine=db.make_engine(url,{'options':'-csearch_path='+schema})
    else:engine=db.make_engine('sqlite:///'+str(tmp_path/'test.db'))
    try:
        seed(engine,password='test-password')
        with TestClient(create_app(engine)) as client:
            r=client.post('/api/v1/session',json={'username':'admin','password':'test-password'})
            assert r.status_code==200
            client.headers['X-CSRF-Token']=r.json()['csrf']
            yield engine,client
    finally:
        engine.dispose()
        if control:
            with control.begin() as conn:conn.exec_driver_sql('DROP SCHEMA '+schema+' CASCADE')
            control.dispose()

def actor(engine,username='admin'):
    with engine.connect() as c:return dict(c.execute(select(db.users).where(db.users.c.username==username)).mappings().one())
def balance(engine):
    with engine.connect() as c:return dict(c.execute(select(db.balances).where(db.balances.c.warehouse_id=='wh-cn',db.balances.c.sku_id=='sku-1')).mappings().one())
def body(version=1):return {'warehouse_id':'wh-cn','sku_id':'sku-1','expected_version':version,'source':'测试供应商','g':10,'q':2,'d':1,'note':''}
def counts(engine):
    with engine.connect() as c:return {t.name:c.scalar(select(func.count()).select_from(t)) for t in [db.ledger,db.documents,db.outbox,db.requests]}
def post(client,payload,key='request-001',path='/receipts/confirm'):
    return client.post('/api/v1'+path,json=payload,headers={'Idempotency-Key':key})

def test_receipt_atomic_and_idempotent(env):
    engine,client=env;before=counts(engine)
    first=post(client,body());assert first.status_code==200
    assert first.json()['balance']['g']==338
    assert first.json()['balance']['q']==2
    assert first.json()['balance']['d']==1
    second=post(client,body());assert second.json()['replayed'] is True
    assert first.json()['document_id']==second.json()['document_id']
    after=counts(engine)
    assert all(after[k]==before[k]+1 for k in before)
    assert balance(engine)['g']==338

def test_same_key_different_payload(env):
    _,client=env
    assert post(client,body()).status_code==200
    r=post(client,{**body(),'g':20})
    assert r.status_code==409 and r.json()['error']['code']=='IDEMPOTENCY_CONFLICT'

def test_stale_version_rolls_back(env):
    engine,client=env;before=counts(engine)
    r=post(client,body(0))
    assert r.status_code==409 and r.json()['error']['code']=='VERSION_CONFLICT'
    assert counts(engine)==before and balance(engine)['g']==328

def test_sale_repeat(env):
    engine,client=env
    payload={k:v for k,v in body().items() if k not in ('g','q','d')};payload['quantity']=3
    assert post(client,payload,path='/sales/confirm').status_code==200
    assert post(client,payload,path='/sales/confirm').json()['replayed']
    assert balance(engine)['g']==325

def test_online_allocation_cannot_be_sold_offline(env):
    engine,client=env
    with db.write(engine) as c:c.execute(update(db.balances).where(db.balances.c.id==balance(engine)['id']).values(online=328))
    payload={k:v for k,v in body().items() if k not in ('g','q','d')};payload['quantity']=1
    r=post(client,payload,path='/sales/confirm')
    assert r.status_code==409 and r.json()['error']['code']=='INSUFFICIENT_STOCK'
    assert balance(engine)['g']==328

def test_last_item_concurrent(env):
    engine,_=env;user=actor(engine);bid=balance(engine)['id']
    with db.write(engine) as c:c.execute(update(db.balances).where(db.balances.c.id==bid).values(g=1))
    gate=Barrier(2)
    payload={k:v for k,v in body().items() if k not in ('g','q','d')};payload['quantity']=1
    def run(n):
        gate.wait()
        try:return stock_action(engine,user,'sale',payload,'concurrent-'+str(n))
        except BusinessError as e:return e.code
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,[1,2]))
    assert sum(isinstance(r,dict) for r in results)==1
    assert balance(engine)['g']==0
    assert 'VERSION_CONFLICT' in results

def test_same_key_concurrent(env):
    engine,_=env;user=actor(engine);gate=Barrier(2);before=counts(engine)
    def run(_):
        gate.wait();return stock_action(engine,user,'receipt',body(),'same-key-concurrent')
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,[1,2]))
    assert results[0]['document_id']==results[1]['document_id']
    assert sorted(r['replayed'] for r in results)==[False,True]
    assert balance(engine)['g']==338
    assert counts(engine)['ledger']==before['ledger']+1

def test_cross_tenant_rejected_on_read_write(env):
    engine,client=env
    with db.write(engine) as c:
        c.execute(insert(db.tenants).values(id='tenant-b',name='第二商户'))
        c.execute(insert(db.warehouses).values(id='foreign-wh',tenant_id='tenant-b',code='B',name='隔离仓',country='CN',timezone='Asia/Shanghai',authority='local'))
        c.execute(insert(db.skus).values(id='foreign-sku',tenant_id='tenant-b',code='SECRET',name='隔离商品',spec='秘密规格',unit='件',barcode=''))
    assert client.get('/api/v1/inventory?warehouse_id=foreign-wh').status_code==404
    assert post(client,{**body(),'warehouse_id':'foreign-wh'}).status_code==404
    assert post(client,{**body(),'sku_id':'foreign-sku'}).status_code==404
    assert 'SECRET' not in client.get('/api/v1/bootstrap').text

def test_restricted_role_and_warehouse(env):
    _,client=env
    r=client.post('/api/v1/session',json={'username':'cashier','password':'test-password'})
    client.headers['X-CSRF-Token']=r.json()['csrf']
    assert post(client,body()).status_code==403
    assert client.get('/api/v1/inventory?warehouse_id=wh-us').status_code==404
    assert all(r['warehouse_id']=='wh-cn' for r in client.get('/api/v1/inventory').json())
    assert '洛杉矶' not in client.get('/api/v1/inventory/export').text

def test_manual_warehouse_read_only(env):
    _,client=env
    r=post(client,{**body(),'warehouse_id':'wh-us'})
    assert r.status_code==409 and r.json()['error']['code']=='READ_ONLY_WAREHOUSE'

@pytest.mark.parametrize('change',[{'g':-1},{'g':1.5},{'g':True},{'g':0,'q':0,'d':0},{'tenant_id':'hack'},{'g':1_000_001}])
def test_invalid_quantity_and_injected_scope(env,change):
    engine,client=env;before=counts(engine)
    assert post(client,{**body(),**change}).status_code==422
    assert counts(engine)==before

def test_missing_key_csrf_origin_auth(env):
    _,client=env
    assert client.post('/api/v1/receipts/confirm',json=body()).status_code==422
    assert client.post('/api/v1/receipts/confirm',json=body(),headers={'Idempotency-Key':'missing-csrf','X-CSRF-Token':''}).status_code==403
    assert client.post('/api/v1/receipts/confirm',json=body(),headers={'Idempotency-Key':'evil-origin','Origin':'https://evil.test'}).status_code==403
    assert client.delete('/api/v1/session').status_code==200
    assert client.get('/api/v1/inventory').status_code==401

def test_transaction_failure_keeps_all_records_unchanged(env,monkeypatch):
    engine,_=env;user=actor(engine);before=counts(engine)
    from sqlalchemy.engine import Connection
    original=Connection.execute
    def fail(self,statement,*args,**kwargs):
        if getattr(statement,'table',None) is db.outbox:raise RuntimeError('simulated crash before event persisted')
        return original(self,statement,*args,**kwargs)
    monkeypatch.setattr(Connection,'execute',fail)
    with pytest.raises(RuntimeError):stock_action(engine,user,'receipt',body(),'failed-transaction')
    assert balance(engine)['g']==328
    assert counts(engine)==before

def test_new_sku_stock_and_csv_formula_escape(env):
    _,client=env
    response=client.post('/api/v1/skus',headers={'Idempotency-Key':'create-new-sku'},json={'code':'=1+1','name':'测试商品','spec':'单件','unit':'件'})
    assert response.status_code==201
    sid=response.json()['id']
    assert post(client,{**body(0),'sku_id':sid}).status_code==200
    assert "'=1+1" in client.get('/api/v1/inventory/export').text
    assert client.post('/api/v1/skus',headers={'Idempotency-Key':'create-duplicate-sku'},json={'code':'=1+1','name':'测试商品','spec':'单件'}).status_code==409

def test_new_warehouse_timezone_and_stock(env):
    _,client=env
    assert client.post('/api/v1/warehouses',headers={'Idempotency-Key':'create-new-warehouse'},json={'code':'NEW','name':'新仓','timezone':'no/such'}).status_code==422
    r=client.post('/api/v1/warehouses',headers={'Idempotency-Key':'create-new-warehouse'},json={'code':'NEW','name':'新仓'})
    assert r.status_code==201
    assert post(client,{**body(0),'warehouse_id':r.json()['id']}).status_code==200

def test_sensitive_files_not_served(env):
    _,client=env
    assert client.get('/.local/login.txt').status_code==404
    assert client.get('/static/../.local/login.txt').status_code==404
    assert client.get('/').headers['content-security-policy']
