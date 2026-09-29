"""Permission-scoped read tools. All numbers and findings come from the ledger DB."""
from contextlib import contextmanager
from datetime import datetime, timezone
from sqlalchemy import select, or_
from . import db, operations as op, accounts
from .domain import BusinessError, available, unallocated, now, require_role, get_warehouse, get_sku

LOW_STOCK = 20
TRANSIT_HOURS = 72
LIMIT = 1000

@contextmanager
def snapshot(engine):
    with engine.connect() as c:
        if engine.dialect.name == 'postgresql':
            c = c.execution_options(isolation_level='REPEATABLE READ')
            c.begin()
        else:
            c.exec_driver_sql('BEGIN')
        try:
            yield c
        finally:
            c.rollback()

def catalog(c, actor):
    accounts.assert_current(c, actor)
    require_role(actor, 'admin', 'warehouse', 'cashier')
    query=select(db.warehouses).where(db.warehouses.c.tenant_id==actor['tenant_id'])
    if actor['role']!='admin':query=query.where(db.warehouses.c.id.in_(actor['warehouse_ids']))
    return [dict(r) for r in c.execute(query).mappings()]

def selected_scope(engine, actor, warehouse_id, sku_id):
    with engine.connect() as c:
        accounts.assert_current(c, actor)
        return {'warehouse':dict(get_warehouse(c,actor,warehouse_id))['name'] if warehouse_id else '',
                'sku':dict(get_sku(c,actor,sku_id))['name'] if sku_id else ''}

def bounded(c, query):
    rows = [dict(r) for r in c.execute(query.limit(LIMIT+1)).mappings()]
    if len(rows)>LIMIT:
        raise BusinessError('NARROW_QUERY','查询范围超过1000条，请选择具体商品或仓库',422)
    return rows

def match(text, rows, fields):
    exact=[r for r in rows if any(text.casefold()==str(r[k]).casefold() for k in fields)]
    return exact or [r for r in rows if any(text.casefold() in str(r[k]).casefold() for k in fields)]

def resolve(c, actor, plan, warehouse_id, sku_id):
    warehouses=catalog(c,actor)
    if warehouse_id:
        get_warehouse(c,actor,warehouse_id)
        warehouses=[w for w in warehouses if w['id']==warehouse_id]
    if plan.warehouse_text:
        warehouses=match(plan.warehouse_text,warehouses,('name','code'))
        if len(warehouses)!=1:
            return None, '仓库未匹配或存在多个匹配，请在筛选框选择一个授权仓库。'
    products=bounded(c,select(db.skus).where(db.skus.c.tenant_id==actor['tenant_id']).order_by(db.skus.c.code)) if not sku_id else [dict(get_sku(c,actor,sku_id))]
    if plan.sku_text:
        products=match(plan.sku_text,products,('code','name','barcode'))
        if len(products)!=1:
            return None, '商品未匹配或存在多个规格，请在筛选框选择具体SKU。'
    return (warehouses, products), None

def execute(engine, actor, plan, warehouse_id=None, sku_id=None):
    with snapshot(engine) as c:
        scope, clarification=resolve(c,actor,plan,warehouse_id,sku_id)
        timestamp=now()
        base={'queried_at':timestamp,'read_only':True,'rules':{'low_stock_below':LOW_STOCK,'transit_attention_hours':TRANSIT_HOURS},'rows':[],'issues':[],'transfers':[], 'sources':[]}
        if clarification:return {**base,'kind':'clarify','answer':clarification}
        warehouses, products=scope
        wm={w['id']:w for w in warehouses};sm={s['id']:s for s in products}
        base['scope']={'warehouses':[{'id':w['id'],'name':w['name']} for w in warehouses], 'sku_count':len(products), 'sku_id':products[0]['id'] if len(products)==1 else None}
        if plan.intent in ('unsupported','clarify'):
            return {**base,'kind':plan.intent,'answer':'本查询入口只分析当前库存，不执行改账或审批，也不预测销量；需要建单请进入“辅助建单”，生成草稿后人工确认。' if plan.intent=='unsupported' else '请写明商品和仓库，或用筛选框选择。本阶段查询当前状态；异常规则固定为可分配低于20、待验大于0及未签收在途。'}
        stocks=bounded(c,select(db.balances).where(db.balances.c.tenant_id==actor['tenant_id'],db.balances.c.warehouse_id.in_(wm),db.balances.c.sku_id.in_(sm)).order_by(db.balances.c.warehouse_id,db.balances.c.sku_id))
        for r in stocks:
            w=wm[r['warehouse_id']];s=sm[r['sku_id']]
            local=w['authority']=='local'
            row={k:r[k] for k in ('warehouse_id','sku_id','g','q','d','r','t','h','b','offline','online','pending','withdrawing','version','updated_at')}
            row.update(sku_name=s['name'],sku_code=s['code'],spec=s['spec'],unit=s['unit'],warehouse_name=w['name'],authority=w['authority'],available=available(r),unallocated=unallocated(r),offline_available=min(max(0,available(r)),r['offline']+max(0,unallocated(r))) if local else None,source='本地库存账本' if local else '人工参考快照，未接入实时库存',source_id='balance:'+r['id'])
            base['rows'].append(row)
            if local and plan.issue in ('all','low_stock') and row['available']<LOW_STOCK:
                base['issues'].append({'type':'low_stock','title':s['name']+' · '+w['name']+' 可分配偏低','evidence':f"可分配 {row['available']} {s['unit']} < 提醒阈值 {LOW_STOCK}；良品{r['g']} − 订单占用{r['r']} − 调拨占用{r['t']} − 冻结{r['h']} − 安全量{r['b']}。",'suggestion':'核对占用与近期需求，再决定是否采购或调拨；此提示不等于缺货预测。','source_id':row['source_id'],'view':'inventory'})
            if local and plan.issue in ('all','pending_quality') and r['q']>0:
                base['issues'].append({'type':'pending_quality','title':s['name']+' · '+w['name']+' 有待验库存','evidence':f"当前待验 {r['q']} {s['unit']}，不计入良品可售；未据此判定超期。",'suggestion':'仓管核对退货单及普通待验批次，检验后登记良品或残次。','source_id':row['source_id'],'view':'returns' if actor['role'] in ('admin','warehouse') else 'inventory'})
        if actor['role'] in ('admin','warehouse'):
            query=select(op.transfers.c.id,op.transfers.c.code,op.transfers.c.warehouse_id,op.transfers.c.destination_id,op.transfers.c.status,op.transfers.c.version,op.transfer_lines.c.sku_id,op.transfer_lines.c.sent,op.transfer_lines.c.received,op.transfer_lines.c.lost).join(op.transfer_lines,op.transfer_lines.c.transfer_id==op.transfers.c.id).where(op.transfers.c.tenant_id==actor['tenant_id'],or_(op.transfers.c.warehouse_id.in_(wm),op.transfers.c.destination_id.in_(wm)),op.transfer_lines.c.sku_id.in_(sm),op.transfer_lines.c.sent>op.transfer_lines.c.received+op.transfer_lines.c.lost).order_by(op.transfers.c.id,op.transfer_lines.c.sku_id)
            transit=bounded(c,query)
            ids={r['id'] for r in transit}
            # Events are scoped by the already authorized transfer IDs.
            shipped={r['transfer_id']:r['created_at'] for r in c.execute(select(op.transfer_events.c.transfer_id,op.transfer_events.c.created_at).where(op.transfer_events.c.transfer_id.in_(ids),op.transfer_events.c.kind=='ship')).mappings()}
            for r in transit:
                qty=r['sent']-r['received']-r['lost'];s=sm[r['sku_id']]
                stamp=shipped.get(r['id']);hours=None
                if stamp:
                    try:hours=max(0,round((datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).total_seconds()/3600,1))
                    except (ValueError,TypeError):pass
                row={'id':r['id'],'code':r['code'],'sku_id':r['sku_id'],'sku_name':s['name'],'quantity':qty,'unit':s['unit'],'source_warehouse':wm.get(r['warehouse_id'],{}).get('name','另一端仓库（当前筛选外）'),'destination_warehouse':wm.get(r['destination_id'],{}).get('name','另一端仓库（当前筛选外）'),'shipped_at':stamp,'hours_in_transit':hours,'version':r['version'],'source_id':'transfer:'+r['id']}
                base['transfers'].append(row)
                if plan.issue in ('all','in_transit'):
                    base['issues'].append({'type':'in_transit','title':r['code']+' · '+s['name']+' 尚有在途','evidence':f"发出{r['sent']} − 签收{r['received']} − 已确认损失{r['lost']} = 在途{qty} {s['unit']}。"+(f'距发出{hours}小时。' if hours is not None else '暂无可用发出时间。'),'suggestion':'已达到72小时关注阈值，请核实运输与签收；尚不能据此确认丢货。' if hours is not None and hours>=TRANSIT_HOURS else '向目的仓核对签收进度，实际收货后再登记。','source_id':row['source_id'],'view':'transfers'})
        base['transit_visible']=actor['role'] in ('admin','warehouse')
        base['sources']=[{'id':r['source_id'],'label':r['sku_name']+' · '+r['warehouse_name'],'version':r['version'],'updated_at':r['updated_at'],'view':'inventory'} for r in base['rows']]+[{'id':r['source_id'],'label':r['code'],'version':r['version'],'updated_at':r['shipped_at'],'view':'transfers'} for r in base['transfers']]
        if plan.intent=='inventory':
            answer=f"已查询{len(stocks)}条库存记录、{len(base['transfers'])}条在途商品记录。按SKU和仓库分别列示，人工参考仓不作为可售承诺。" if stocks or base['transfers'] else '当前范围没有库存或可见在途记录；无记录不等于确认库存为零。'
        else:
            answer=f"发现{len(base['issues'])}项待关注事项，具体依据与处理建议见下方。" if base['issues'] else '当前范围未触发所选检查规则；这不代表所有业务都无异常。'
        base.update(kind=plan.intent,answer=answer,notes=['可分配 = G − R − T − H − B；线下可用还受渠道额度约束。','结果为本次查询快照，不代表后续操作一定可用。','未接入外部平台；不把人工参考仓与本地可售混算。']+([] if base['transit_visible'] else ['当前店员岗位不开放调拨单查询；在途记录未纳入本次结果。']))
        return base
