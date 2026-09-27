"""Admin-controlled accounts and immediate session revocation."""
from sqlalchemy import Table, Column as C, String as S, Integer as I, JSON, select, insert, update, delete
from . import db, workflows as w
from .domain import require_role, get_warehouse, uid, now
from .security import hash_password

settings=Table('user_settings',db.metadata,C('user_id',S,primary_key=True),C('active',I,nullable=False),C('version',I,nullable=False))
audit=Table('admin_audit',db.metadata,C('id',S,primary_key=True),C('tenant_id',S),C('actor_id',S),C('action',S),C('target_id',S),C('before',JSON),C('after',JSON),C('created_at',S))

def active(c,user_id):
    value=c.scalar(select(settings.c.active).where(settings.c.user_id==user_id))
    return value is None or value==1

def safe_user(c,row):
    config=c.execute(select(settings).where(settings.c.user_id==row['id'])).mappings().first()
    return {k:row[k] for k in ('id','username','name','role','warehouse_ids')}|{'active':bool(config['active']) if config else True,'version':config['version'] if config else 0}

def validate_grants(c,actor,body):
    for wid in body['warehouse_ids']:get_warehouse(c,actor,wid)
    if body['role']!='admin' and not body['warehouse_ids']:w.fail('EMPTY_GRANTS','仓管和店员至少需要一个授权仓库',422)
    if body['role']=='admin' and body['warehouse_ids']:w.fail('ADMIN_SCOPE','管理员使用全商户仓库权限，无需逐仓授权',422)

def create_user(engine,actor,body,key):
    require_role(actor,'admin')
    def apply(c):
        validate_grants(c,actor,body)
        row=dict(id=uid(),tenant_id=actor['tenant_id'],username=body['username'],name=body['name'],role=body['role'],warehouse_ids=body['warehouse_ids'],password_hash=hash_password(body['password']))
        c.execute(insert(db.users).values(**row));c.execute(insert(settings).values(user_id=row['id'],active=1,version=1))
        result=safe_user(c,row)
        c.execute(insert(audit).values(id=uid(),tenant_id=actor['tenant_id'],actor_id=actor['id'],action='user.create',target_id=row['id'],before={},after=result,created_at=now()))
        return result
    return w.command(engine,actor,'user.create',body,key,apply)

def edit_user(engine,actor,user_id,body,key):
    require_role(actor,'admin')
    def apply(c):
        row=c.execute(select(db.users).where(db.users.c.id==user_id,db.users.c.tenant_id==actor['tenant_id'])).mappings().first()
        if not row:w.fail('NOT_FOUND','账号不存在',404)
        before=safe_user(c,row)
        if before['version']!=body['expected_version']:w.fail('VERSION_CONFLICT','账号信息已变化')
        if user_id==actor['id'] and (not body['active'] or body['role']!='admin'):w.fail('SELF_LOCKOUT','不能停用或降级当前管理员',403)
        validate_grants(c,actor,body)
        c.execute(update(db.users).where(db.users.c.id==user_id).values(name=body['name'],role=body['role'],warehouse_ids=body['warehouse_ids']))
        config=dict(user_id=user_id,active=int(body['active']),version=before['version']+1)
        if c.execute(select(settings.c.user_id).where(settings.c.user_id==user_id)).first():c.execute(update(settings).where(settings.c.user_id==user_id).values(**config))
        else:c.execute(insert(settings).values(**config))
        c.execute(delete(db.sessions).where(db.sessions.c.user_id==user_id))
        result={**before,'name':body['name'],'role':body['role'],'warehouse_ids':body['warehouse_ids'],'active':body['active'],'version':config['version']}
        c.execute(insert(audit).values(id=uid(),tenant_id=actor['tenant_id'],actor_id=actor['id'],action='user.update',target_id=user_id,before=before,after=result,created_at=now()))
        return result
    return w.command(engine,actor,'user.update:'+user_id,body,key,apply)

def assert_current(c,actor):
    row=c.execute(select(db.users).where(db.users.c.id==actor['id'],db.users.c.tenant_id==actor['tenant_id'])).mappings().first()
    if not row:w.fail('NOT_FOUND','账号不属于当前商户',404)
    if not active(c,actor['id']) or row['role']!=actor['role'] or row['warehouse_ids']!=actor['warehouse_ids']:
        w.fail('UNAUTHENTICATED','账号权限已变化，请重新登录',401)
