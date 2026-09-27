'use strict';
let third={transfers:[],targets:[],returns:[],quality:[],users:[],audit:[],maintenance:null};
const thirdStatus=s=>({draft:'待备货',reserved:'已备货',in_transit:'在途',partial:'部分签收',completed:'已完成',cancelled:'已取消',closed_difference:'差异已结案',awaiting_quality:'待质检',pending:'待审核',approved:'已通过',rejected:'已驳回'}[s]||s);
const stockVersion=(wid,sid)=>state.stocks.find(x=>x.warehouse_id===wid&&x.sku_id===sid)?.version||0;
const transferWh=id=>third.targets.find(w=>w.id===id)?.name||wh(id).name;
const reasonField='<label>原因 / 说明<textarea name="reason" minlength="2" maxlength="500" required></textarea></label>';
async function thirdLoad(){
 third={transfers:[],targets:[],returns:[],quality:[],users:[],audit:[],maintenance:null};
 if(['admin','warehouse'].includes(state.user.role)){
  const [t,d,r,q]=await Promise.all([api('/transfers'),api('/transfer-targets'),api('/returns'),api('/quality')]);Object.assign(third,{transfers:t,targets:d,returns:r,quality:q});
 }
 if(state.user.role==='admin'){const [u,a,m]=await Promise.all([api('/users'),api('/admin-audit'),api('/maintenance')]);Object.assign(third,{users:u,audit:a,maintenance:m})}
}
function transferPage(){
 if(!['admin','warehouse'].includes(state.user.role))return heading('仓间调拨','当前岗位无调拨权限。');
 return heading('仓间调拨','备货只占用；发出后转在途；目的仓按实际签收增加库存。','<button class="primary" id="transfer-new">＋ 新建调拨</button>')+third.transfers.map(t=>{
 const source=state.meta.warehouses.some(w=>w.id===t.warehouse_id),dest=state.meta.warehouses.some(w=>w.id===t.destination_id);
 let buttons='';
 if(source&&t.status==='draft')buttons+=`<button class="primary" data-transfer="${t.id}" data-action="reserve">确认备货</button>`;
 if(source&&t.status==='reserved')buttons+=`<button class="primary" data-transfer="${t.id}" data-action="ship">确认发出</button>`;
 if(source&&['draft','reserved'].includes(t.status))buttons+=`<button class="secondary-button" data-transfer="${t.id}" data-action="cancel">取消调拨</button>`;
 if(dest&&['in_transit','partial'].includes(t.status))buttons+=`<button class="primary" data-transfer="${t.id}" data-action="receive">登记签收</button>`;
 if(['in_transit','partial'].includes(t.status)&&!t.claims.some(c=>c.status==='pending'))buttons+=`<button class="secondary-button" data-transfer="${t.id}" data-action="discrepancy">申报在途差异</button>`;
 return `<section class="card form-card"><div class="card-head"><div><h2>${esc(t.code)} <span class="pill">${thirdStatus(t.status)}</span></h2><p>${esc(transferWh(t.warehouse_id))} → ${esc(transferWh(t.destination_id))}</p></div><div class="action-buttons">${buttons}</div></div><p class="muted">${esc(t.reason)}</p><div class="table-wrap"><table><thead><tr><th>商品</th><th>计划</th><th>发出</th><th>已收</th><th>在途</th><th>确认短少</th></tr></thead><tbody>${t.lines.map(l=>`<tr><td>${productCell(l.sku_id)}</td><td>${l.quantity}</td><td>${l.sent}</td><td>${l.received}</td><td>${l.in_transit}</td><td>${l.lost}</td></tr>`).join('')}</tbody></table></div>${t.claims.map(c=>`<div class="notice"><p>差异申请 · ${thirdStatus(c.status)} · ${esc(c.reason)}${c.note?' / 审核：'+esc(c.note):''}</p>${c.status==='pending'&&state.user.role==='admin'&&c.requester_id!==state.user.id?`<button class="secondary-button" data-claim="${c.id}">审核差异</button>`:''}</div>`).join('')}<details><summary>调拨过程与凭证 (${t.events.length})</summary>${t.events.map(e=>`<p class="muted">${date(e.created_at)} · ${esc(({create:'创建',reserve:'备货',ship:'发出',receive:'签收',cancel:'取消',loss:'差异结案'})[e.kind])} · ${esc(e.reference)}<br>${e.document_ids.map(esc).join(' / ')}</p>`).join('')}</details></section>`;
 }).join('')+(!third.transfers.length?'<section class="card form-card empty">暂无调拨单。请先在商品与仓库中建立至少两个自营仓。</section>':'');
}
function transferEditor(){
 phaseModal('新建仓间调拨',phaseInput('code','调拨单号')+`<label>调出仓<select name="warehouse_id">${warehouseOptions(false,true)}</select></label><label>调入仓<select name="destination_id">${third.targets.map(w=>`<option value="${esc(w.id)}">${esc(w.name)}</option>`).join('')}</select></label>${reasonField}<div id="transfer-lines"></div><button class="secondary-button" type="button" id="add-transfer-line">＋ 添加商品</button>`,async f=>{
 const raw=Object.fromEntries(new FormData(f));const lines=[...f.querySelectorAll('.transfer-line')].map(el=>({sku_id:el.querySelector('select').value,quantity:Number(el.querySelector('input').value)}));await workflowWrite('/transfers',{code:raw.code,warehouse_id:raw.warehouse_id,destination_id:raw.destination_id,reason:raw.reason,lines})});
 const add=()=>{const el=document.createElement('div');el.className='transfer-line form-grid';el.innerHTML=`<label>商品${phaseSkuSelect()}</label><label>调拨数量<input type="number" min="1" max="1000000" step="1" value="1" required></label><button class="text-button" type="button">删除此行</button>`;el.querySelector('button').onclick=()=>el.remove();$('#transfer-lines').append(el)};$('#add-transfer-line').onclick=add;add();
}
function transferDialog(t,action){
 if(action==='receive'){
  phaseModal('签收 · '+t.code,phaseInput('reference','签收凭证号')+'<p class="muted">只填写本次实收。未收到的数量继续保留在途；残次也计入签收。</p>'+t.lines.filter(l=>l.in_transit>0).map(l=>`<fieldset data-sku="${esc(l.sku_id)}"><legend>${esc(sku(l.sku_id).name)} · 在途${l.in_transit}</legend><div class="form-grid">${['g','q','d'].map((k,i)=>`<label>${['良品','待验','残次'][i]}<input name="${k}" type="number" min="0" max="${l.in_transit}" step="1" value="0" required></label>`).join('')}</div></fieldset>`).join(''),f=>{
   const lines=[...f.querySelectorAll('[data-sku]')].map(el=>({sku_id:el.dataset.sku,expected_version:stockVersion(t.destination_id,el.dataset.sku),...Object.fromEntries(['g','q','d'].map(k=>[k,Number(el.querySelector(`[name=${k}]`).value)]))})).filter(l=>l.g+l.q+l.d>0);
   return workflowWrite(`/transfers/${t.id}/receive`,{expected_version:t.version,reference:f.elements.reference.value,lines})});return;
 }
 const label={reserve:'确认备货',ship:'确认发出',cancel:'取消调拨',discrepancy:'申报在途差异'}[action];
 phaseModal(label+' · '+t.code,`<p>${action==='discrepancy'?'此申请将全部剩余在途数量登记为短少；须另一位管理员核对通过。尚未到货请保留在途，不要结案。':action==='reserve'?'将占用源仓未分配库存，保护已分配给销售渠道的额度。':action==='ship'?'确认整单实物已交运，源仓良品和备货占用同步减少。':'仅能取消未发出的调拨；已备货数量将解除占用。'}</p>${reasonField}`,f=>workflowWrite(`/transfers/${t.id}/${action}`,{expected_version:t.version,reason:f.elements.reason.value,...(action==='discrepancy'?{}:{lines:t.lines.map(l=>({sku_id:l.sku_id,expected_version:stockVersion(t.warehouse_id,l.sku_id)}))})}));
}
function returnsPage(){
 if(!['admin','warehouse'].includes(state.user.role))return heading('退货与质检','当前岗位无退货质检权限。');
 return heading('退货与质检','客户退回先进入待验；按原单控制累计退货，质检后再转为良品或残次。','<div class="action-buttons"><button class="secondary-button" id="supplier-return">退供应商</button><button class="secondary-button" id="quality-new">普通待验质检</button><button class="primary" id="customer-return">客户退货</button></div>')+third.returns.map(r=>`<section class="card form-card"><div class="card-head"><h2>${r.kind==='customer'?'客户退货':'退供应商'} · ${esc(r.reference)} <span class="pill">${thirdStatus(r.status)}</span></h2>${r.status==='awaiting_quality'?`<button class="primary" data-inspect="${r.id}">登记质检</button>`:''}</div><p>${esc(sku(r.sku_id).name)} · ${esc(wh(r.warehouse_id).name)}</p><p>退货 ${r.quantity} 件${r.kind==='customer'?` / 质检良品 ${r.good} / 残次 ${r.bad} / 剩余待验 ${r.quantity-r.good-r.bad}`:''}</p><p>${esc(r.reason)}</p><p class="muted">原单据 ${esc(r.original_id)}<br>执行单据 ${esc(r.document_id)}</p></section>`).join('')+`<section class="card form-card"><h2>质检记录</h2>${third.quality.map(q=>`<p>${esc(q.reference)} · ${esc(sku(q.sku_id).name)} · 良品 ${q.good} / 残次 ${q.bad}<br><span class="muted">${esc(q.reason)} · ${date(q.created_at)}</span></p>`).join('')||'<p class="muted">暂无质检记录</p>'}</section>`;
}
function returnEditor(kind){
 const docs=state.ledger.filter(l=>(kind==='customer'?['sale']:['receipt','purchase_receipt']).includes(l.kind));
 phaseModal(kind==='customer'?'登记客户实物退货':'确认退供应商出库',`<label>原单据（最近100条流水）<select name="original_id">${docs.map(d=>`<option value="${esc(d.document_id)}">${esc(d.document_id)} · ${esc(sku(d.sku_id).name)}</option>`).join('')}</select></label>${phaseInput('reference','退货凭证号')}${kind==='customer'?phaseInput('quantity','本次实物退回数量',1,'number'):`<p class="muted">确认实物已退给供应商。不能使用其他客户退货尚未质检的待验库存。</p>${['g','q','d'].map((k,i)=>phaseInput(k,['良品出库','待验出库','残次出库'][i],0,'number')).join('')}`}${reasonField}`,f=>{
 const raw=Object.fromEntries(new FormData(f)),doc=docs.find(d=>d.document_id===raw.original_id);if(!doc)throw new Error('没有可关联的原单据');
 return workflowWrite('/returns',{kind,original_id:raw.original_id,reference:raw.reference,reason:raw.reason,expected_version:stockVersion(doc.warehouse_id,doc.sku_id),...(kind==='customer'?{quantity:Number(raw.quantity)}:{g:Number(raw.g),q:Number(raw.q),d:Number(raw.d)})})});
}
function qualityEditor(ret=null){
 phaseModal(ret?'退货质检 · '+ret.reference:'普通待验库存质检',ret?`<p>${esc(sku(ret.sku_id).name)} · 本单剩余待验 ${ret.quantity-ret.good-ret.bad}</p>`+qualityFields():`<label>仓库<select name="warehouse_id">${warehouseOptions(false,true)}</select></label><label>商品${phaseSkuSelect()}</label><p class="muted">此处处理采购或调拨待验。客户退货待验请在对应退货卡片处理。</p>`+qualityFields(),f=>{const raw=Object.fromEntries(new FormData(f));return workflowWrite('/quality',{reference:raw.reference,reason:raw.reason,good:Number(raw.good),bad:Number(raw.bad),expected_version:stockVersion(ret?ret.warehouse_id:raw.warehouse_id,ret?ret.sku_id:raw.sku_id),...(ret?{return_id:ret.id,return_version:ret.version}:{warehouse_id:raw.warehouse_id,sku_id:raw.sku_id})})});
}
function qualityFields(){return phaseInput('reference','质检凭证号')+phaseInput('good','检验合格数量',0,'number')+phaseInput('bad','检验残次数量',0,'number')+reasonField}
function accountsPage(){
 if(state.user.role!=='admin')return heading('账号与试运行','仅管理员可管理账号与备份。');
 const m=third.maintenance;
 return heading('账号与试运行','按岗位和仓库授权；更改权限或停用后，原登录会话立即失效。','<button class="primary" id="user-new">＋ 新建账号</button>')+`<section class="card form-card"><h2>试运行检查</h2><p>${esc(m?.database)} · 数据结构版本 ${esc(m?.schema_versions.join(' / '))} · ${m?.balance_count}条库存记录</p><p class="${m?.inventory_issues.length?'error':'positive'}">${m?.inventory_issues.length?'存在库存约束异常，请暂停相关操作并核对':'库存数量、额度及退货待验约束检查通过'}</p><p class="muted">外部平台尚未接入。数据库备份可能包含账号哈希，请仅存于受控目录。</p><button class="secondary-button" id="backup-run" ${m?.backup_supported?'':'disabled'}>创建备份并演练恢复</button><p class="muted">恢复演练生成独立副本，不替换当前数据库；副本中的旧登录会话会清除。</p><p id="backup-result" role="status"></p></section><section class="card form-card"><h2>商户账号</h2><div class="table-wrap"><table><thead><tr><th>账号</th><th>岗位</th><th>仓库权限</th><th>状态</th><th>操作</th></tr></thead><tbody>${third.users.map(u=>`<tr><td>${esc(u.name)}<br><small>${esc(u.username)}</small></td><td>${({admin:'管理员',warehouse:'仓管',cashier:'店员'})[u.role]}</td><td>${u.role==='admin'?'全部仓库':u.warehouse_ids.map(id=>esc(wh(id).name)).join('、')}</td><td>${u.active?'启用':'停用'}</td><td><button class="text-button" data-user="${u.id}">编辑授权</button></td></tr>`).join('')}</tbody></table></div></section><section class="card form-card"><h2>管理操作审计</h2>${third.audit.map(a=>`<p class="muted">${date(a.created_at)} · ${esc(({ 'user.create':'创建账号','user.update':'修改授权','backup.restore_drill':'备份与恢复演练'})[a.action]||a.action)} · ${esc(a.target_id)}</p>`).join('')||'<p class="muted">暂无操作记录</p>'}</section>`;
}
function userEditor(user=null){
 let pending=null;
 const html=(user?`<p>账号：${esc(user.username)}</p>`:phaseInput('username','登录账号（字母数字下划线）')+'<label>初始密码<input name="password" type="password" minlength="12" maxlength="256" autocomplete="new-password" required></label>')+phaseInput('name','显示姓名',user?.name||'')+`<label>岗位<select name="role"><option value="warehouse">仓管</option><option value="cashier">店员</option><option value="admin">管理员（全部仓库）</option></select></label><fieldset><legend>授权仓库（管理员无需勾选）</legend>${state.meta.warehouses.map(w=>`<label class="check-line"><input type="checkbox" name="warehouse_ids" value="${esc(w.id)}" ${user?.warehouse_ids.includes(w.id)?'checked':''}>${esc(w.name)}</label>`).join('')}</fieldset>`+(user?`<label>账号状态<select name="active"><option value="true">启用</option><option value="false">停用</option></select></label>`:'')+'<p class="muted">密码只提交到本地服务进行哈希处理，不保存到浏览器待重试存储。管理员不能停用或降级自己。</p>';
 phaseModal(user?'编辑账号授权':'新建账号',html,async f=>{
  const data=new FormData(f);const role=data.get('role');const body={name:data.get('name'),role,warehouse_ids:role==='admin'?[]:data.getAll('warehouse_ids')};
  if(user)return workflowWrite('/users/'+user.id,{...body,active:data.get('active')==='true',expected_version:user.version});
  pending=pending||{body:{...body,username:data.get('username'),password:data.get('password')},key:crypto.randomUUID()};
  try{await api('/users',{method:'POST',headers:{'Idempotency-Key':pending.key},body:JSON.stringify(pending.body)});pending=null;f.reset()}
  catch(e){if(!e.unknown)pending=null;else{e.unknown=false;e.message='结果尚未确认，请勿修改表单，点击确认提交将重试原请求。'}throw e}
 });
 if(user){$('#editor-form [name=role]').value=user.role;$('#editor-form [name=active]').value=String(user.active)}
}
function thirdBind(){
 if($('#transfer-new'))$('#transfer-new').onclick=transferEditor;
 document.querySelectorAll('[data-transfer]').forEach(b=>b.onclick=()=>transferDialog(third.transfers.find(t=>t.id===b.dataset.transfer),b.dataset.action));
 document.querySelectorAll('[data-claim]').forEach(b=>b.onclick=()=>phaseModal('审核在途短少结案','<p>通过后将全部剩余在途记为确认短少，目的仓不增加这些数量。核对承运及实收凭证后再决定。</p><label>审核结果<select name="decision"><option value="approve">通过并结案</option><option value="reject">驳回</option></select></label><label>审核意见<textarea name="note" minlength="2" maxlength="500" required></textarea></label>',f=>workflowWrite('/transfer-claims/'+b.dataset.claim+'/review',Object.fromEntries(new FormData(f)))));
 if($('#customer-return'))$('#customer-return').onclick=()=>returnEditor('customer');if($('#supplier-return'))$('#supplier-return').onclick=()=>returnEditor('supplier');if($('#quality-new'))$('#quality-new').onclick=()=>qualityEditor();
 document.querySelectorAll('[data-inspect]').forEach(b=>b.onclick=()=>qualityEditor(third.returns.find(r=>r.id===b.dataset.inspect)));
 if($('#user-new'))$('#user-new').onclick=()=>userEditor();document.querySelectorAll('[data-user]').forEach(b=>b.onclick=()=>userEditor(third.users.find(u=>u.id===b.dataset.user)));
 if($('#backup-run'))$('#backup-run').onclick=async e=>{e.target.disabled=true;try{const r=await workflowWrite('/maintenance/backup',{});await load();render();$('#backup-result').textContent=`备份 ${r.backup.file}；恢复副本 ${r.restore.restored_file} 已校验，旧会话已清除。`}catch(err){toast(err.message);render()}finally{e.target.disabled=false}};
}
