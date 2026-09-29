from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, insert, update, func
from app import db, operations as op, accounts as ac, workflows as w
from app.domain import BusinessError
from app.main import create_app
from app.backup import snapshot,restore
from test_inventory import env, actor, balance, body, counts, post
from test_phase2 import send,login


def make_target(c):
    r=send(c,'/warehouses',{'code':'CN-NEW','name':'调入测试仓'},'create-target');assert r.status_code==201
    return r.json()['id']

def transfer(c,n=10,extra=None):
    destination=make_target(c)
    payload={'code':'TRANSFER-001','warehouse_id':'wh-cn','destination_id':destination,'reason':'仓间补货测试','lines':[{'sku_id':'sku-1','quantity':n}]}
    if extra:payload['lines'].append(extra)
    r=send(c,'/transfers',payload,'create-transfer');assert r.status_code==200,r.text
    return r.json()

def advance(c,t,action,version,stock_version,extra=None,key=None):
    lines=[{'sku_id':'sku-1','expected_version':stock_version}]
    if extra:lines.append(extra)
    return send(c,f"/transfers/{t['id']}/{action}",{'expected_version':version,'reason':'测试调拨过程','lines':lines},key or 'action-'+action)

def receive(c,t,n,version=3,stock_version=0,ref='SIGN-1',key='sign-transfer'):
    return send(c,f"/transfers/{t['id']}/receive",{'expected_version':version,'reference':ref,'lines':[{'sku_id':'sku-1','g':n,'q':0,'d':0,'expected_version':stock_version}]},key)

def return_customer(c,qty=2,key='customer-return',reference='RT-001'):
    sale=post(c,{'warehouse_id':'wh-cn','sku_id':'sku-1','source':'退货测试客户','expected_version':1,'quantity':5},key='return-original-sale',path='/sales/confirm').json()
    r=send(c,'/returns',{'kind':'customer','original_id':sale['document_id'],'reference':reference,'quantity':qty,'expected_version':2,'reason':'客户实物退回'},key)
    assert r.status_code==200,r.text
    return r.json(),sale

def inspection(c,r,good=1,bad=1,version=3,ret_version=1,reference='QC-001',key='inspect-return'):
    return send(c,'/quality',{'reference':reference,'return_id':r['id'],'return_version':ret_version,'good':good,'bad':bad,'expected_version':version,'reason':'逐件验收完成'},key)

def test_transfer_reserve_ship_partial_receive_conserves_stock(env):
    engine,c=env;t=transfer(c)
    assert balance(engine)['g']==328 and balance(engine)['t']==0
    assert advance(c,t,'reserve',1,1).status_code==200
    assert balance(engine)['g']==328 and balance(engine)['t']==10
    assert advance(c,t,'reserve',1,1).json()['replayed']
    assert advance(c,t,'ship',2,2).status_code==200
    assert balance(engine)['g']==318 and balance(engine)['t']==0
    assert not any(r['warehouse_id']==t['destination_id'] for r in c.get('/api/v1/inventory').json())
    assert receive(c,t,6).json()['status']=='partial'
    current=c.get('/api/v1/transfers').json()[0]
    assert current['lines'][0]['in_transit']==4
    assert receive(c,t,4,4,1,'SIGN-2','sign-second').json()['status']=='completed'
    dest=next(r for r in c.get('/api/v1/inventory').json() if r['warehouse_id']==t['destination_id'])
    assert dest['g']==10 and balance(engine)['g']+dest['g']==328

def test_transfer_cancel_releases_only_reservation(env):
    engine,c=env;t=transfer(c)
    assert advance(c,t,'reserve',1,1).status_code==200
    assert advance(c,t,'cancel',2,2).status_code==200
    assert balance(engine)['g']==328 and balance(engine)['t']==0
    assert advance(c,t,'ship',3,3).status_code==409

def test_transfer_rejects_same_manual_and_foreign_warehouse(env):
    engine,c=env
    base={'code':'BAD','warehouse_id':'wh-cn','destination_id':'wh-cn','reason':'错误仓库测试','lines':[{'sku_id':'sku-1','quantity':1}]}
    assert send(c,'/transfers',base).status_code==422
    assert send(c,'/transfers',{**base,'destination_id':'wh-us'}).status_code==409
    assert send(c,'/transfers',{**base,'destination_id':'not-owned'}).status_code==404

def test_transfer_multiline_rollback_protects_online(env):
    engine,c=env;t=transfer(c,extra={'sku_id':'sku-2','quantity':10})
    with db.write(engine) as conn:conn.execute(update(db.balances).where(db.balances.c.warehouse_id=='wh-cn',db.balances.c.sku_id=='sku-2').values(online=186))
    before=counts(engine)
    assert advance(c,t,'reserve',1,1,{'sku_id':'sku-2','expected_version':1}).status_code==409
    assert counts(engine)==before and balance(engine)['t']==0
    assert c.get('/api/v1/transfers').json()[0]['status']=='draft'

def test_over_receive_duplicate_and_cancel_after_ship(env):
    engine,c=env;t=transfer(c);advance(c,t,'reserve',1,1);advance(c,t,'ship',2,2)
    assert receive(c,t,11).status_code==409
    assert receive(c,t,6).status_code==200
    assert receive(c,t,6).json()['replayed']
    assert receive(c,t,1,4,1,key='duplicate-sign-new-key').status_code==409
    assert advance(c,t,'cancel',4,3).status_code==409

def test_transfer_source_destination_warehouse_scope(env):
    engine,c=env;t=transfer(c);advance(c,t,'reserve',1,1);advance(c,t,'ship',2,2)
    login(c,'warehouse')
    assert len(c.get('/api/v1/transfers').json())==1
    assert receive(c,t,1).status_code==404
    with db.write(engine) as conn:conn.execute(update(db.users).where(db.users.c.id=='warehouse').values(warehouse_ids=[t['destination_id']]))
    assert receive(c,t,1).status_code==200
    assert advance(c,t,'cancel',4,3).status_code==404

def test_discrepancy_requires_other_admin_and_tracks_loss(env):
    engine,c=env;t=transfer(c);advance(c,t,'reserve',1,1);advance(c,t,'ship',2,2);receive(c,t,7)
    login(c,'warehouse')
    claim=send(c,f"/transfers/{t['id']}/discrepancy",{'expected_version':4,'reason':'签收7件，承运确认短少3件'},'missing-claim').json()
    review=lambda:send(c,f"/transfer-claims/{claim['id']}/review",{'decision':'approve','note':'已核对承运凭证'},'review-missing')
    assert review().status_code==403
    login(c,'admin');assert review().status_code==200
    row=c.get('/api/v1/transfers').json()[0]
    assert row['status']=='closed_difference' and row['lines'][0]['lost']==3 and row['lines'][0]['in_transit']==0
    assert balance(engine)['g']==318
    assert receive(c,t,3,5,1,'SIGN-LATE','sign-late').status_code==409

def test_discrepancy_stale_after_receipt_and_self_approval(env):
    _,c=env;t=transfer(c);advance(c,t,'reserve',1,1);advance(c,t,'ship',2,2)
    claim=send(c,f"/transfers/{t['id']}/discrepancy",{'expected_version':3,'reason':'等待核对差异'},'claim-self').json()
    assert send(c,f"/transfer-claims/{claim['id']}/review",{'decision':'approve','note':'自审测试'}).status_code==403
    # Other admin can reject without touching inventory, but cannot approve a stale quantity.
    create=send(c,'/users',{'username':'reviewer','password':'test-password-strong','name':'复核人','role':'admin','warehouse_ids':[]},'create-reviewer')
    assert create.status_code==200
    receive(c,t,1)
    r=c.post('/api/v1/session',json={'username':'reviewer','password':'test-password-strong'});c.headers['X-CSRF-Token']=r.json()['csrf']
    assert send(c,f"/transfer-claims/{claim['id']}/review",{'decision':'approve','note':'过期测试'}).status_code==409
    assert send(c,f"/transfer-claims/{claim['id']}/review",{'decision':'reject','note':'已有新到货，重新核对'},'reject-old-claim').status_code==200

def test_return_to_quality_then_good_and_bad(env):
    engine,c=env;r,sale=return_customer(c)
    assert balance(engine)['g']==323 and balance(engine)['q']==2
    assert inspection(c,r).status_code==200
    assert balance(engine)['g']==324 and balance(engine)['q']==0 and balance(engine)['d']==1
    assert inspection(c,r).json()['replayed']
    assert c.get('/api/v1/returns').json()[0]['status']=='completed'

def test_return_cumulative_limit_and_reverse_original_block(env):
    engine,c=env;r,sale=return_customer(c,4)
    assert send(c,'/returns',{'kind':'customer','original_id':sale['document_id'],'reference':'RT-002','quantity':2,'expected_version':3,'reason':'超额退回'},'too-many-return').status_code==409
    assert send(c,'/approvals',{'kind':'reversal','original_id':sale['document_id'],'expected_version':3,'reason':'不能重复恢复库存'},'reverse-after-return').status_code==409
    assert balance(engine)['q']==4

def test_quality_reservation_protected_from_adjustment_and_generic_inspection(env):
    engine,c=env;r,_=return_customer(c)
    assert send(c,'/approvals',{'kind':'adjustment','warehouse_id':'wh-cn','sku_id':'sku-1','expected_version':3,'delta':{'q':-1},'reason':'减少被占用待验'},'adjust-held-quality').status_code==200
    # Approval enforces the hold even when a pre-existing adjustment request was valid at creation.
    login(c,'warehouse')
    a=send(c,'/approvals',{'kind':'adjustment','warehouse_id':'wh-cn','sku_id':'sku-1','expected_version':3,'delta':{'q':-1},'reason':'测试待验保护'},'warehouse-adjust').json()
    login(c,'admin')
    assert send(c,f"/approvals/{a['id']}/review",{'decision':'approve','note':'待验不能挪用'},'held-review').status_code==409
    assert send(c,'/quality',{'warehouse_id':'wh-cn','sku_id':'sku-1','good':1,'reference':'GENERIC-QC','expected_version':3,'reason':'不能挪用退货待验'},'generic-quality').status_code==409
    assert inspection(c,r,1,0).status_code==200
    assert inspection(c,r,1,0,4,2,'QC-002','qc-second').status_code==200

def test_supplier_return_limits_and_purchase_progress_unchanged(env):
    engine,c=env;receipt=post(c,body()).json()
    b={'kind':'supplier','original_id':receipt['document_id'],'reference':'SUP-1','g':3,'q':1,'d':1,'expected_version':2,'reason':'质量问题退给供应商'}
    assert send(c,'/returns',b).status_code==200
    assert balance(engine)['g']==335 and balance(engine)['q']==1 and balance(engine)['d']==0
    assert send(c,'/returns',b).json()['replayed']
    assert send(c,'/returns',{**b,'reference':'SUP-2','g':10,'q':0,'d':0,'expected_version':3},'supplier-over').status_code==409

def test_return_cannot_use_reversed_original(env):
    _,c=env;doc=post(c,body()).json()['document_id'];login(c,'warehouse')
    a=send(c,'/approvals',{'kind':'reversal','original_id':doc,'expected_version':2,'reason':'原收货录错'},'request-reverse').json();login(c,'admin')
    assert send(c,f"/approvals/{a['id']}/review",{'decision':'approve','note':'确认原单错误'},'approve-reverse').status_code==200
    assert send(c,'/returns',{'kind':'supplier','original_id':doc,'reference':'SUP-R','g':1,'expected_version':3,'reason':'重复退供应商'},'return-reversed').status_code==409

def test_quality_outbox_failure_rolls_back_task_and_stock(env,monkeypatch):
    engine,c=env;r,_=return_customer(c);before=counts(engine)
    from sqlalchemy.engine import Connection
    original=Connection.execute
    def fail(self,statement,*args,**kwargs):
        if getattr(statement,'table',None) is db.outbox:raise RuntimeError('test outbox crash')
        return original(self,statement,*args,**kwargs)
    monkeypatch.setattr(Connection,'execute',fail)
    with pytest.raises(RuntimeError):op.inspect_quality(engine,actor(engine),{'return_id':r['id'],'return_version':1,'good':2,'bad':0,'expected_version':3,'reference':'CRASH','reason':'故障测试'},'crash-inspection')
    assert counts(engine)==before and balance(engine)['q']==2
    with engine.connect() as conn:assert conn.scalar(select(op.returns.c.good).where(op.returns.c.id==r['id']))==0

def test_user_create_update_revokes_sessions_and_no_secret_exposure(env):
    engine,c=env
    data={'username':'new-worker','password':'trial-strong-password','name':'测试仓管','role':'warehouse','warehouse_ids':['wh-cn']}
    r=send(c,'/users',data,'new-worker-account');assert r.status_code==200
    user=r.json();assert data['password'] not in r.text and 'password_hash' not in r.text
    with TestClient(create_app(engine)) as other:
        assert other.post('/api/v1/session',json={'username':'new-worker','password':data['password']}).status_code==200
        assert other.get('/api/v1/inventory').status_code==200
        edit={'name':user['name'],'role':'cashier','warehouse_ids':['wh-cn'],'active':False,'expected_version':1}
        assert send(c,'/users/'+user['id'],edit,'disable-worker').status_code==200
        assert other.get('/api/v1/inventory').status_code==401
        assert other.post('/api/v1/session',json={'username':'new-worker','password':data['password']}).status_code==401
    assert data['password'] not in c.get('/api/v1/admin-audit').text
    with engine.connect() as conn:
        assert data['password'] not in json.dumps(list(conn.execute(select(db.requests.c.response)).scalars()))

def test_users_scope_self_lockout_and_invalid_grants(env):
    _,c=env
    assert send(c,'/users/admin',{'name':'管理员','role':'cashier','warehouse_ids':['wh-cn'],'active':True,'expected_version':0}).status_code==403
    assert send(c,'/users',{'username':'invalid','password':'long-test-password','name':'无效授权','role':'warehouse','warehouse_ids':['other-tenant-wh']},'foreign-grant').status_code==404
    login(c,'warehouse');assert c.get('/api/v1/users').status_code==403 and c.get('/api/v1/maintenance').status_code==403

def test_stale_actor_cannot_write_after_role_change(env):
    engine,c=env;old=actor(engine,'warehouse')
    assert send(c,'/users/warehouse',{'name':'仓管','role':'cashier','warehouse_ids':['wh-cn'],'active':True,'expected_version':0}).status_code==200
    with pytest.raises(BusinessError) as e:op.transfer_create(engine,old,{'code':'RACE','warehouse_id':'wh-cn','destination_id':'wh-us','reason':'旧岗位写入','lines':[{'sku_id':'sku-1','quantity':1}]},'stale-role')
    assert e.value.status==401

def test_backup_restore_new_copy_and_checksum(tmp_path):
    import sqlite3
    source=tmp_path/'source.db'
    with sqlite3.connect(source) as c:c.executescript('CREATE TABLE sample (value TEXT); INSERT INTO sample VALUES ("retained"); CREATE TABLE sessions (token TEXT); INSERT INTO sessions VALUES ("old-session");')
    report=snapshot(source,tmp_path/'backups');file=tmp_path/'backups'/report['file']
    result=restore(file,tmp_path/'restored.db');assert result['verified'] and result['counts']['sessions']==0
    with sqlite3.connect(tmp_path/'restored.db') as c:assert c.execute('SELECT value FROM sample').fetchone()[0]=='retained'
    with pytest.raises(ValueError):restore(file,source)
    with file.open('ab') as f:f.write(b'tamper')
    with pytest.raises(ValueError):restore(file,tmp_path/'tampered.db')

def test_maintenance_backup_api(env,tmp_path,monkeypatch):
    engine,c=env
    if engine.dialect.name!='sqlite':pytest.skip('SQLite snapshot endpoint; PostgreSQL uses pg_dump drill')
    monkeypatch.chdir(tmp_path)
    r=send(c,'/maintenance/backup',{},'backup-operation');assert r.status_code==200,r.text
    assert r.json()['restore']['verified']
    assert send(c,'/maintenance/backup',{},'backup-operation').json()['replayed']
    assert c.get('/api/v1/maintenance').json()['inventory_issues']==[]

def test_parallel_return_cannot_exceed_original_sale(env):
    engine,c=env
    sale=post(c,{'warehouse_id':'wh-cn','sku_id':'sku-1','source':'测试','expected_version':1,'quantity':1},path='/sales/confirm').json();user=actor(engine);gate=Barrier(2)
    def task(n):
        gate.wait()
        try:return op.return_create(engine,user,{'kind':'customer','original_id':sale['document_id'],'reference':f'R-{n}','quantity':1,'expected_version':2,'reason':'并发退货测试'},f'return-concurrent-{n}')
        except BusinessError as e:return e.code
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(task,[1,2]))
    assert sum(isinstance(r,dict) for r in results)==1 and balance(engine)['q']==1

def test_phase_two_database_upgrades_without_losing_records(env):
    engine,c=env
    from app.migrations import PHASE3,versions,migrate
    before=counts(engine);stock=balance(engine)
    with db.write(engine) as conn:
        for name in reversed(PHASE3):db.metadata.tables[name].drop(conn)
        conn.execute(versions.delete().where(versions.c.version==3))
    migrate(engine);migrate(engine)
    assert counts(engine)==before and balance(engine)==stock
    with engine.connect() as conn:assert list(conn.execute(select(versions.c.version).order_by(versions.c.version)).scalars())==[1,2,3,4]

def test_complete_purchase_transfer_sale_return_quality_flow(env):
    engine,c=env
    purchase=send(c,'/purchases',{'code':'E2E-PO','warehouse_id':'wh-cn','supplier':'闭环供应商','lines':[{'sku_id':'sku-1','quantity':20}]},'e2e-purchase').json()
    for n,(qty,ov,sv) in enumerate([(12,1,1),(8,2,2)]):
        r=send(c,f"/purchases/{purchase['id']}/receive",{'reference':f'E2E-DEL-{n}','expected_version':ov,'lines':[{'sku_id':'sku-1','g':qty,'expected_version':sv}]},f'e2e-receive-{n}');assert r.status_code==200
    t=transfer(c,10);assert advance(c,t,'reserve',1,3).status_code==200;assert advance(c,t,'ship',2,4).status_code==200
    assert receive(c,t,10).status_code==200
    sale=post(c,{'warehouse_id':t['destination_id'],'sku_id':'sku-1','source':'闭环客户','quantity':4,'expected_version':1},key='e2e-sale',path='/sales/confirm').json()
    ret=send(c,'/returns',{'kind':'customer','reference':'E2E-RT','original_id':sale['document_id'],'quantity':2,'expected_version':2,'reason':'闭环测试实物退回'},'e2e-return').json()
    assert inspection(c,ret).status_code==200
    stocks=c.get('/api/v1/inventory').json();dest=next(r for r in stocks if r['warehouse_id']==t['destination_id'])
    assert dest['g']==7 and dest['d']==1 and dest['q']==0
    assert balance(engine)['g']==338
    assert c.get('/api/v1/maintenance').json()['inventory_issues']==[]

def test_parallel_transfer_receipts_cannot_double_credit(env):
    engine,c=env;t=transfer(c,1);advance(c,t,'reserve',1,1);advance(c,t,'ship',2,2)
    user=actor(engine);gate=Barrier(2)
    def task(n):
        gate.wait()
        try:return op.transfer_action(engine,user,t['id'],'receive',{'expected_version':3,'reference':f'P-{n}','lines':[{'sku_id':'sku-1','g':1,'q':0,'d':0,'expected_version':0}]},f'parallel-sign-{n}')
        except BusinessError as e:return e.code
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(task,[1,2]))
    assert sum(isinstance(r,dict) for r in results)==1
    dest=next(r for r in c.get('/api/v1/inventory').json() if r['warehouse_id']==t['destination_id']);assert dest['g']==1


def test_validation_does_not_echo_password(env):
    engine,c=env
    r=send(c,'/users',{'username':'invalid-user','password':'tiny','name':'测试','role':'warehouse','warehouse_ids':['wh-cn']},'short-user-secret')
    assert r.status_code==422 and 'tiny' not in r.text
    assert all(set(e)=={'loc','msg','type'} for e in r.json()['detail'])
