from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from sqlalchemy import select, insert, update, func
from app import db, workflows as w
from app.domain import BusinessError
from app.migrations import migrate, versions, BASE
from test_inventory import env, actor, balance, counts, body, post


def send(client,path,data,key='phase-two-request'):
    return post(client,data,key,path)

def login(client,name):
    r=client.post('/api/v1/session',json={'username':name,'password':'test-password'})
    assert r.status_code==200
    client.headers['X-CSRF-Token']=r.json()['csrf']

def po(client,code='PO-001',extra=None):
    payload={'code':code,'supplier':'测试供应商','warehouse_id':'wh-cn','lines':[{'sku_id':'sku-1','quantity':100}]}
    if extra:payload['lines'].append(extra)
    r=send(client,'/purchases',payload,'create-'+code)
    assert r.status_code==200,r.text
    return r.json()

def receive(client,order,g,ref='DEL-1',version=1,stock_version=1,key='receive-order-1',extra=None):
    lines=[{'sku_id':'sku-1','g':g,'expected_version':stock_version}]
    if extra:lines.append(extra)
    return send(client,f"/purchases/{order['id']}/receive",{'reference':ref,'expected_version':version,'lines':lines},key)

def adjustment(client,version=1,delta=None):
    r=send(client,'/approvals',{'kind':'adjustment','warehouse_id':'wh-cn','sku_id':'sku-1','expected_version':version,'delta':delta or {'g':-2},'reason':'盘点复核差异'},'request-adjustment')
    assert r.status_code==200,r.text
    return r.json()

def review(client,a,decision='approve',key='review-approval-1'):
    return send(client,f"/approvals/{a['id']}/review",{'decision':decision,'note':'已核对原始单据'},key)

def preview(client,kind,text):return client.post('/api/v1/imports/preview',json={'kind':kind,'csv_text':text})

def test_migration_preserves_phase_one_and_repeats(tmp_path):
    engine=db.make_engine('sqlite:///'+str(tmp_path/'legacy.db'))
    db.metadata.create_all(engine,tables=[db.metadata.tables[n] for n in BASE])
    with db.write(engine) as c:
        c.execute(insert(db.tenants).values(id='legacy',name='原有商户'))
        c.execute(insert(db.documents).values(id='historic',tenant_id='legacy',note='不可丢失'))
    migrate(engine);migrate(engine)
    with engine.connect() as c:
        assert c.scalar(select(db.documents.c.note))=='不可丢失'
        assert list(c.execute(select(versions.c.version).order_by(versions.c.version)).scalars())==[1,2,3]
    engine.dispose()

def test_catalog_idempotence_and_required_key(env):
    _,c=env;data={'code':'NEW','name':'新商品','spec':'一件'}
    first=send(c,'/skus',data);second=send(c,'/skus',data)
    assert first.status_code==201 and second.json()['id']==first.json()['id'] and second.json()['replayed']
    assert send(c,'/skus',{**data,'name':'改名'}).status_code==409
    assert c.post('/api/v1/skus',json=data).status_code==422

def test_import_preview_no_write_atomic_and_replay(env):
    engine,c=env
    r=preview(c,'skus','code,name,spec\nNEW-A,商品A,一件\nNEW-B,商品B,两件\n');assert r.status_code==200
    batch=r.json();assert batch['status']=='preview'
    assert not any(x['code']=='NEW-A' for x in c.get('/api/v1/bootstrap').json()['skus'])
    path=f"/imports/{batch['id']}/apply"
    assert send(c,path,{}).json()['count']==2
    assert send(c,path,{}).json()['replayed']
    assert send(c,path,{},'another-import-key').json()['already_applied']
    assert preview(c,'skus','code,name,spec\nNEW-A,商品A,一件\nNEW-B,商品B,两件\n').json()['status']=='applied'
    with engine.connect() as conn:assert conn.scalar(select(func.count()).select_from(db.skus))==8

def test_import_errors_and_stale_conflict_roll_back(env):
    _,c=env
    bad=preview(c,'skus','code,name,spec\nA,商品,一件\nA,商品,一件\n').json()
    assert bad['errors'][0]['line']==3
    assert send(c,f"/imports/{bad['id']}/apply",{}).status_code==409
    b=preview(c,'skus','code,name,spec\nFRESH,商品,一件\nLATER,商品,一件\n').json()
    assert send(c,'/skus',{'code':'LATER','name':'另建','spec':'一件'},'catalog-conflict').status_code==201
    assert send(c,f"/imports/{b['id']}/apply",{},'import-stale').status_code==409
    assert 'FRESH' not in [x['code'] for x in c.get('/api/v1/bootstrap').json()['skus']]

@pytest.mark.parametrize('text',['code,code\nX,Y','code,name,spec\nX,Y','code,name,spec,unknown\nX,Y,Z,W'])
def test_csv_shape_rejected(env,text):
    _,c=env;r=preview(c,'skus',text)
    assert r.status_code==422 or r.json()['status']=='invalid'

def test_opening_with_commitments_and_duplicate_protection(env):
    engine,c=env
    b=preview(c,'warehouses','code,name\nNEW-W,新仓\n').json()
    assert send(c,f"/imports/{b['id']}/apply",{}).status_code==200
    content='warehouse_code,sku_code,g,q,d,r,t,h,b,offline,online,pending,withdrawing\nNEW-W,YW-0001,100,3,2,10,5,3,2,10,20,4,1\n'
    b=preview(c,'opening',content).json();assert b['status']=='preview'
    assert send(c,f"/imports/{b['id']}/apply",{},'opening-apply').status_code==200
    rows=c.get('/api/v1/inventory').json();row=next(r for r in rows if r['g']==100)
    assert row['available']==80 and row['offline_available']==55
    changed=preview(c,'opening',content.replace('100,3','101,3')).json()
    assert changed['status']=='invalid'
    assert counts(engine)['ledger']==13

def test_invalid_opening_quota_and_readonly(env):
    _,c=env
    b=preview(c,'warehouses','code,name\nNEW-W,新仓\n').json();send(c,f"/imports/{b['id']}/apply",{})
    for text in ['NEW-W,YW-0001,1,2','CN-YW-01,YW-0001,1,0','US-LA-01,YW-0001,1,0']:
        assert preview(c,'opening','warehouse_code,sku_code,g,online\n'+text).json()['status']=='invalid'

def test_purchase_60_then_40_and_business_reference(env):
    engine,c=env;o=po(c)
    a=receive(c,o,60);assert a.status_code==200 and a.json()['status']=='partial'
    assert receive(c,o,60).json()['replayed']
    duplicate=receive(c,o,60,version=2,stock_version=2,key='new-key-same-delivery')
    assert duplicate.status_code==409 and duplicate.json()['error']['code']=='RECEIPT_EXISTS'
    b=receive(c,o,40,'DEL-2',2,2,'second-delivery');assert b.json()['status']=='completed'
    assert balance(engine)['g']==428
    assert c.get('/api/v1/purchases').json()[0]['lines'][0]['received']==100

def test_purchase_multi_line_rollback_and_over_receipt(env):
    engine,c=env;o=po(c,extra={'sku_id':'sku-2','quantity':10});before=counts(engine)
    r=receive(c,o,1,extra={'sku_id':'sku-2','g':11,'expected_version':1})
    assert r.status_code==409 and counts(engine)==before and balance(engine)['g']==328
    assert all(l['received']==0 for l in c.get('/api/v1/purchases').json()[0]['lines'])
    assert receive(c,o,101).status_code==409

def test_purchase_qd_close_and_stale(env):
    engine,c=env;o=po(c)
    payload={'reference':'Q-D','expected_version':1,'lines':[{'sku_id':'sku-1','g':0,'q':5,'d':2,'expected_version':1}]}
    assert send(c,f"/purchases/{o['id']}/receive",payload).status_code==200
    assert balance(engine)['g']==328 and balance(engine)['q']==5
    assert receive(c,o,1,'STALE',1,2,'stale-order').status_code==409
    assert send(c,f"/purchases/{o['id']}/close",{'expected_version':2,'reason':'供应商无法补货'},'close-order').status_code==200
    assert receive(c,o,1,'LATE',3,2,'closed-receive').status_code==409
    assert balance(engine)['g']==328

def test_two_person_adjustment_idempotence_and_self_rejection(env):
    engine,c=env;a=adjustment(c)
    assert review(c,a).status_code==403 and balance(engine)['g']==328
    login(c,'warehouse');a=adjustment(c)
    assert review(c,a).status_code==403
    login(c,'admin');r=review(c,a);assert r.status_code==200 and balance(engine)['g']==326
    assert review(c,a).json()['replayed']
    assert review(c,a,key='review-again-different').status_code==409
    rows=c.get('/api/v1/approvals').json();assert next(x for x in rows if x['id']==a['id'])['reviewer_name']=='商户管理员'

def test_stale_approval_and_rejection_do_not_mutate(env):
    engine,c=env;login(c,'warehouse');a=adjustment(c)
    login(c,'admin');assert post(c,body()).status_code==200
    before=counts(engine);assert review(c,a).status_code==409 and counts(engine)==before
    assert review(c,a,'reject','reject-stale').status_code==200 and counts(engine)['ledger']==before['ledger']

def test_reversal_sale_restores_offline_and_retains_original(env):
    engine,c=env
    with db.write(engine) as conn:conn.execute(update(db.balances).where(db.balances.c.id==balance(engine)['id']).values(offline=10))
    r=post(c,{'warehouse_id':'wh-cn','sku_id':'sku-1','source':'测试客户','expected_version':1,'quantity':3},path='/sales/confirm').json()
    login(c,'warehouse')
    a=send(c,'/approvals',{'kind':'reversal','original_id':r['document_id'],'expected_version':2,'reason':'交货录入错误'}).json()
    login(c,'admin');assert review(c,a).status_code==200
    assert balance(engine)['g']==328 and balance(engine)['offline']==10
    assert send(c,'/approvals',{'kind':'reversal','original_id':r['document_id'],'expected_version':3,'reason':'重复冲正申请'},'duplicate-reverse').status_code==409
    with engine.connect() as conn:assert conn.scalar(select(db.documents.c.status).where(db.documents.c.id==r['document_id']))=='completed'

def test_reversal_purchase_updates_net_received(env):
    engine,c=env;o=po(c);r=receive(c,o,100).json();login(c,'warehouse')
    a=send(c,'/approvals',{'kind':'reversal','original_id':r['document_ids'][0],'expected_version':2,'reason':'重复录入到货'}).json()
    login(c,'admin');assert review(c,a).status_code==200
    order=c.get('/api/v1/purchases').json()[0]
    assert order['status']=='open' and order['lines'][0]['received']==0 and order['version']==3
    assert balance(engine)['g']==328
    # The original supplier delivery reference remains consumed even after reversal.
    assert receive(c,o,100,'DEL-1',3,3,'after-reversal').status_code==409

def test_adjustment_cannot_consume_online_commitment(env):
    engine,c=env
    with db.write(engine) as conn:conn.execute(update(db.balances).where(db.balances.c.id==balance(engine)['id']).values(online=328))
    r=send(c,'/approvals',{'kind':'adjustment','warehouse_id':'wh-cn','sku_id':'sku-1','expected_version':1,'delta':{'g':-1},'reason':'盘点减少'})
    assert r.status_code==409 and r.json()['error']['code']=='COMMITMENT_CONFLICT'

def test_receive_concurrency_only_one_commits(env):
    engine,c=env;o=po(c);user=actor(engine);gate=Barrier(2)
    def run(n):
        gate.wait()
        try:return w.purchase_receive(engine,user,o['id'],{'reference':'SAME','expected_version':1,'lines':[{'sku_id':'sku-1','g':100,'q':0,'d':0,'expected_version':1}]},f'concurrent-receive-{n}')
        except BusinessError as e:return e.code
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(run,[1,2]))
    assert sum(isinstance(r,dict) for r in results)==1 and balance(engine)['g']==428

def test_approval_failure_rolls_back_everything(env,monkeypatch):
    engine,c=env;login(c,'warehouse');a=adjustment(c);before=counts(engine)
    from sqlalchemy.engine import Connection
    original=Connection.execute
    def fail(self,statement,*args,**kwargs):
        if getattr(statement,'table',None) is db.outbox:raise RuntimeError('simulated outbox failure')
        return original(self,statement,*args,**kwargs)
    monkeypatch.setattr(Connection,'execute',fail)
    with pytest.raises(RuntimeError):w.approval_review(engine,actor(engine),a['id'],{'decision':'approve','note':'复核通过'},'crash-approval')
    assert counts(engine)==before and balance(engine)['g']==328
    with engine.connect() as conn:assert conn.scalar(select(w.approvals.c.status).where(w.approvals.c.id==a['id']))=='pending'

def test_scope_roles_and_forged_body(env):
    engine,c=env;o=po(c)
    user=actor(engine);user={**user,'tenant_id':'another-tenant'}
    with pytest.raises(BusinessError) as err:w.purchase_close(engine,user,o['id'],{'expected_version':1,'reason':'跨商户访问'},'tenant-isolation')
    assert err.value.status==404
    login(c,'cashier')
    assert c.get('/api/v1/purchases').status_code==403
    assert c.get('/api/v1/approvals').status_code==403
    assert preview(c,'skus','code,name,spec\nX,Y,Z').status_code==403
    login(c,'warehouse');assert preview(c,'skus','code,name,spec\nX,Y,Z').status_code==403
    assert send(c,'/purchases',{'code':'P','supplier':'S','warehouse_id':'wh-us','lines':[{'sku_id':'sku-1','quantity':1}]}).status_code==404
    assert send(c,'/approvals',{'kind':'adjustment','warehouse_id':'wh-cn','sku_id':'sku-1','expected_version':1,'delta':{'g':1,'online':-1},'reason':'伪造额度'}).status_code==422

def test_opening_stale_second_row_rolls_back_first(env):
    engine,c=env
    b=preview(c,'warehouses','code,name\nNEW-W,新仓\n').json();send(c,f"/imports/{b['id']}/apply",{})
    b=preview(c,'opening','warehouse_code,sku_code,g\nNEW-W,YW-0001,10\nNEW-W,YW-0002,20\n').json()
    wid=next(x['id'] for x in c.get('/api/v1/bootstrap').json()['warehouses'] if x['code']=='NEW-W')
    assert post(c,{**body(0),'warehouse_id':wid,'sku_id':'sku-2'},key='intervening-receipt').status_code==200
    before=counts(engine)
    assert send(c,f"/imports/{b['id']}/apply",{},'stale-opening').status_code==409
    assert counts(engine)==before
    assert not any(x['warehouse_id']==wid and x['sku_id']=='sku-1' for x in c.get('/api/v1/inventory').json())

def test_duplicate_reversal_requests_only_one_can_approve(env):
    engine,c=env
    doc=post(c,body()).json()['document_id'];login(c,'warehouse')
    payload={'kind':'reversal','original_id':doc,'expected_version':2,'reason':'原始录入有误'}
    a=send(c,'/approvals',payload,'reverse-request-a').json();b=send(c,'/approvals',payload,'reverse-request-b').json()
    login(c,'admin')
    assert review(c,a).status_code==200
    before=counts(engine)
    assert review(c,b,key='review-second-request').status_code==409
    assert counts(engine)==before and balance(engine)['g']==328

def test_purchase_positive_multi_sku_and_duplicate_line_reject(env):
    engine,c=env;o=po(c,extra={'sku_id':'sku-2','quantity':10})
    assert receive(c,o,100,extra={'sku_id':'sku-2','g':8,'q':1,'d':1,'expected_version':1}).json()['status']=='completed'
    order=c.get('/api/v1/purchases').json()[0]
    assert len(order['receipt_history'][0]['document_ids'])==2
    r=send(c,'/purchases',{'code':'DUP','supplier':'S','warehouse_id':'wh-cn','lines':[{'sku_id':'sku-1','quantity':1}]*2},'duplicate-lines')
    assert r.status_code==422

def test_cross_tenant_import_and_approval_not_found(env):
    engine,c=env;b=preview(c,'skus','code,name,spec\nTEST-NEW,测试,一件').json();a=adjustment(c)
    foreign={**actor(engine),'tenant_id':'foreign-tenant'}
    for action in [lambda:w.import_apply(engine,foreign,b['id'],'foreign-import'),lambda:w.approval_review(engine,foreign,a['id'],{'decision':'approve','note':'越权测试'},'foreign-review')]:
        with pytest.raises(BusinessError) as e:action()
        assert e.value.status==404
