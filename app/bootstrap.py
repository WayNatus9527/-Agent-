"""Explicit local demo initialization, never run automatically in production."""
import os
import secrets
from pathlib import Path
from sqlalchemy import select, insert
from . import db
from .domain import uid, now, FIELDS
from .security import hash_password

def seed(engine, password=None):
    db.metadata.create_all(engine)
    tenant='demo-yiwu'
    with db.write(engine,tenant) as c:
        if c.execute(select(db.tenants).where(db.tenants.c.id==tenant)).first():return None
        c.execute(insert(db.tenants).values(id=tenant,name='义乌好物商贸 · 演示商户'))
        credentials=[]
        for username,name,role in [('admin','商户管理员','admin'),('warehouse','仓管小陈','warehouse'),('cashier','店员小林','cashier')]:
            secret=password or secrets.token_urlsafe(12)
            c.execute(insert(db.users).values(id=username,tenant_id=tenant,username=username,name=name,
                      role=role,password_hash=hash_password(secret),warehouse_ids=['wh-cn']))
            credentials.append(f'{name}：{username} / {secret}')
        c.execute(insert(db.warehouses),[
            {'id':'wh-cn','tenant_id':tenant,'code':'CN-YW-01','name':'义乌中心仓','country':'CN','timezone':'Asia/Shanghai','authority':'local'},
            {'id':'wh-us','tenant_id':tenant,'code':'US-LA-01','name':'洛杉矶海外仓','country':'US','timezone':'America/Los_Angeles','authority':'manual'}])
        products=[('sku-1','YW-0001','折叠收纳箱','奶油白 / 32L','件','690000000001',328,96),
                  ('sku-2','YW-0002','不锈钢保温杯','雾蓝 / 500ml','件','690000000002',186,64),
                  ('sku-3','YW-0003','旅行分装瓶套装','透明 / 6件套','套','690000000003',42,20),
                  ('sku-4','YW-0004','桌面理线夹','深灰 / 5枚装','包','690000000004',12,0),
                  ('sku-5','YW-0005','便携折叠购物袋','橄榄绿 / 大号','件','690000000005',215,78),
                  ('sku-6','YW-0006','硅胶厨房工具','米杏 / 3件套','套','690000000006',7,12)]
        for sid,code,name,spec,unit,barcode,cn,us in products:
            c.execute(insert(db.skus).values(id=sid,tenant_id=tenant,code=code,name=name,spec=spec,unit=unit,barcode=barcode))
            for wh,count in [('wh-cn',cn),('wh-us',us)]:
                timestamp=now();values=dict.fromkeys(FIELDS,0);values.update(g=count)
                c.execute(insert(db.balances).values(id=uid(),tenant_id=tenant,owner_id=tenant,warehouse_id=wh,sku_id=sid,version=1,updated_at=timestamp,**values))
                document_id='INIT-'+uid()[:8].upper()
                c.execute(insert(db.documents).values(id=document_id,tenant_id=tenant,kind='initial',warehouse_id=wh,sku_id=sid,quantity=count,
                          source='演示期初批次',note='仅供本地开发与验证，无真实仓库数据',actor_id='admin',created_at=timestamp,status='completed'))
                c.execute(insert(db.ledger).values(id=uid(),tenant_id=tenant,owner_id=tenant,warehouse_id=wh,sku_id=sid,document_id=document_id,
                          kind='initial',delta={'g':count,'q':0,'d':0},before=dict.fromkeys(FIELDS,0),after=values,actor_id='admin',created_at=timestamp))
        return credentials

if __name__=='__main__':
    engine=db.make_engine();credentials=seed(engine)
    if credentials:
        path=Path('.local/login.txt');path.parent.mkdir(exist_ok=True)
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as f:f.write('本地演示登录信息，请勿提交版本库或部署到公网。\n\n'+'\n'.join(credentials)+'\n')
        print('演示数据已初始化；随机登录密码保存在 .local/login.txt')
    else:print('演示商户已存在，保留当前数据与密码。')
