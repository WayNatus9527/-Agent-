"""Read-only assistant API; no inventory write tool is registered."""
import logging
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Literal
from uuid import uuid4
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from . import agent_model, agent_queries
from .domain import BusinessError, require_role

log=logging.getLogger('inventory.agent')

class Ask(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    mode: Literal['model','inventory','issues']='model'
    question: str=Field(default='',max_length=1000)
    warehouse_id: str|None=Field(default=None,min_length=1,max_length=120)
    sku_id: str|None=Field(default=None,min_length=1,max_length=120)
    issue: Literal['all','low_stock','pending_quality','in_transit']='all'

    @model_validator(mode='after')
    def valid_question(self):
        if self.mode=='model' and not self.question:raise ValueError('请填写问题')
        if self.mode!='model' and self.question:raise ValueError('本地按钮查询不解析自然语言，请使用商品与仓库筛选')
        return self

def install(app,engine,actor_for):
    lock=Lock();attempts=defaultdict(deque);running=set()

    @app.get('/api/v1/agent/status')
    def status(request:Request):
        actor,_=actor_for(request);require_role(actor,'admin','warehouse','cashier')
        return {**agent_model.status(),'read_only':True,'draft_actions':['transfer','purchase','adjustment'] if actor['role'] in ('admin','warehouse') else [],'max_question_length':1000,'model_requests_per_minute':10}

    @app.post('/api/v1/agent/ask')
    def ask(body:Ask,request:Request):
        actor,_=actor_for(request,True);require_role(actor,'admin','warehouse','cashier')
        selection=agent_queries.selected_scope(engine,actor,body.warehouse_id,body.sku_id)
        user=(actor['tenant_id'],actor['id'])
        with lock:
            if user in running:raise BusinessError('AGENT_BUSY','已有一个查询处理中，请等待结果',429)
            if body.mode=='model':
                stamp=time.monotonic()
                for key in list(attempts):
                    while attempts[key] and attempts[key][0]<stamp-60:attempts[key].popleft()
                    if not attempts[key]:del attempts[key]
                if len(attempts[user])>=10:raise BusinessError('AGENT_RATE_LIMIT','模型提问每分钟最多10次，请稍后重试',429)
                attempts[user].append(stamp)
            running.add(user)
        request_id=str(uuid4());started=time.monotonic();outcome='error'
        try:
            if body.mode=='model':plan=agent_model.select_plan(body.question,selection)
            else:plan=agent_model.QueryPlan(intent=body.mode,sku_text='',warehouse_text='',issue=body.issue)
            # Reauthenticate after the external request; revoked sessions cannot read results.
            current,_=actor_for(request,True)
            result=agent_queries.execute(engine,current,plan,body.warehouse_id,body.sku_id)
            actor_for(request,True)
            outcome=result['kind']
            return {**result,'request_id':request_id,'mode':body.mode,'model':agent_model.MODEL if body.mode=='model' else None,
                    'interpretation':plan.model_dump(),'elapsed_ms':round((time.monotonic()-started)*1000)}
        finally:
            with lock:running.discard(user)
            # Do not log raw questions, model output, credentials or inventory values.
            log.info('query_id=%s mode=%s outcome=%s elapsed_ms=%s',request_id,body.mode,outcome,round((time.monotonic()-started)*1000))

class DraftQuestion(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    question:str=Field(min_length=2,max_length=1000)

class ConfirmDraft(BaseModel):
    model_config=ConfigDict(extra='forbid')
    confirmation_token:str=Field(min_length=20,max_length=100)

def install_actions(app,engine,actor_for):
    from . import agent_actions as actions
    from .phase2 import Key
    from contextlib import contextmanager
    from .accounts import assert_current
    lock=Lock();attempts=defaultdict(deque);running=set()
    @contextmanager
    def model_gate(actor):
        user=(actor['tenant_id'],actor['id']);stamp=time.monotonic()
        with lock:
            for key in list(attempts):
                while attempts[key] and attempts[key][0]<stamp-60:attempts[key].popleft()
                if not attempts[key]:del attempts[key]
            if user in running or len(attempts[user])>=10:raise BusinessError('AGENT_RATE_LIMIT','草稿模型请求处理中或过于频繁，请稍后重试',429)
            running.add(user);attempts[user].append(stamp)
        try:yield
        finally:
            with lock:running.discard(user)

    @app.post('/api/v1/agent/drafts')
    def prepare(body:actions.DraftInput,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True)
        return actions.prepare(engine,actor,body.model_dump(),idempotency_key)

    @app.post('/api/v1/agent/drafts/from-question')
    def propose(body:DraftQuestion,request:Request,idempotency_key:Key):
        actor,_=actor_for(request,True);require_role(actor,'admin','warehouse')
        with engine.connect() as c:assert_current(c,actor)
        with model_gate(actor):
            payload=actions.model_input(engine,actor,body.question)
            current,_=actor_for(request,True)
            return actions.prepare(engine,current,payload,idempotency_key,origin='model')

    @app.get('/api/v1/agent/drafts')
    def drafts(request:Request):
        actor,_=actor_for(request);return actions.listing(engine,actor)

    @app.post('/api/v1/agent/drafts/{pid}/confirm')
    def confirm(pid:str,body:ConfirmDraft,request:Request):
        actor,_=actor_for(request,True);return actions.finish(engine,actor,pid,body.confirmation_token,'confirm')

    @app.post('/api/v1/agent/drafts/{pid}/cancel')
    def cancel(pid:str,body:ConfirmDraft,request:Request):
        actor,_=actor_for(request,True);return actions.finish(engine,actor,pid,body.confirmation_token,'cancel')

    @app.get('/api/v1/agent/action-audit')
    def audit(request:Request):
        actor,_=actor_for(request);return actions.audit_list(engine,actor)
