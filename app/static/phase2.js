'use strict';
const phaseStatus=s=>({open:'待收货',partial:'部分收货',completed:'已收齐',closed:'已关闭',pending:'待审批',approved:'已通过',rejected:'已驳回',preview:'待导入',invalid:'校验未通过',applied:'已导入'}[s]||s);
const phaseViews=['imports','purchases','approvals'];
let phase={imports:[],purchases:[],approvals:[],preview:null,selectedOrder:null};
const workflowStorage=()=>`workflow-pending:${state.user?.id||state.user?.name}`;
function workflowPending(){try{return JSON.parse(sessionStorage.getItem(workflowStorage())||'null')}catch{return null}}
async function workflowWrite(path,body,pending=null){
 if(state.busy)throw new Error('操作处理中，请稍候');
 const storage=workflowStorage();const existing=workflowPending();
 if(existing&&!pending)throw new Error('有一笔结果未确认的操作，请先使用页面顶部按钮重试原请求。');
 const req=pending||{path,body,key:crypto.randomUUID()};sessionStorage.setItem(storage,JSON.stringify(req));state.busy=true;
 try{const result=await api(req.path,{method:'POST',headers:{'Idempotency-Key':req.key},body:JSON.stringify(req.body)});sessionStorage.removeItem(storage);return result}
 catch(e){if(!e.unknown&&e.code!=='UNAUTHENTICATED')sessionStorage.removeItem(storage);throw e}
 finally{state.busy=false}
}
async function phaseLoad(){
 if(!['admin','warehouse'].includes(state.user.role)){phase={...phase,imports:[],purchases:[],approvals:[]};return}
 const [p,a,i]=await Promise.all([api('/purchases'),api('/approvals'),state.user.role==='admin'?api('/imports'):Promise.resolve([])]);
 phase.purchases=p;phase.approvals=a;phase.imports=i;
}
function workflowBanner(){return workflowPending()?'<div class="notice"><p>上一笔操作结果尚未确认。请先重试原请求，系统会防止重复入账。</p><button class="secondary-button" id="workflow-retry">重试原请求</button></div>':''}
function phasePage(){
 if(state.view==='imports')return importPage();
 if(state.view==='purchases')return purchasePage();
 return approvalPage();
}
function importPage(){
 if(state.user.role!=='admin')return heading('批量导入','仅管理员可预览和确认导入。');
 const batch=phase.preview;
 return heading('批量导入','先预览、再确认。整批成功或整批回滚，不会只导入一半。')+`<section class="card form-card"><form id="import-form"><div class="form-grid"><label>导入类型<select name="kind" id="import-kind"><option value="skus">商品资料</option><option value="warehouses">仓库资料</option><option value="opening">期初库存与占用</option></select></label><label>上传 UTF-8 CSV<input id="import-file" type="file" accept=".csv,text/csv"></label></div><a id="import-template" href="/api/v1/imports/template/skus" download>下载所选类型的空白模板</a><p class="muted">Excel 可另存为 UTF-8 CSV。每批最多500行。先导入商品和仓库，再导入期初；已有库存历史的组合不能覆盖。</p><details><summary>期初库存列说明</summary><p class="muted">g 良品、q 待验、d 残次、r 订单占用、t 调拨占用、h 冻结、b 安全量；offline 线下额度、online 线上额度、pending 待发布、withdrawing 待撤回。空白数量按0处理。良品必须覆盖占用和额度。占用为期初汇总，不自动创建原业务单据。</p></details><label>CSV内容<textarea name="csv_text" id="import-text" rows="7" required placeholder="code,name,spec,unit,barcode"></textarea></label><p class="error" id="phase-error" role="alert"></p><button class="primary" type="submit">校验并预览</button></form></section>`+
 (batch?`<section class="card form-card"><h2>预览结果 · ${phaseStatus(batch.status)}</h2><p>有效 ${batch.rows.length} 行 / 错误 ${batch.errors.length} 行</p>${batch.errors.map(e=>`<p class="error">第${e.line}行：${esc(e.message)}</p>`).join('')}<div class="table-wrap"><table><thead><tr>${Object.keys(batch.rows[0]||{}).map(k=>`<th>${esc(k)}</th>`).join('')}</tr></thead><tbody>${batch.rows.map(row=>`<tr>${Object.values(row).map(v=>`<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div><button class="primary" id="apply-import" ${batch.status!=='preview'?'disabled':''}>确认整批导入</button><p id="import-result" role="status"></p></section>`:'')+
 `<section class="card form-card"><h2>最近导入批次</h2><div class="table-wrap"><table><thead><tr><th>创建时间</th><th>类型</th><th>行数</th><th>状态</th></tr></thead><tbody>${phase.imports.map(b=>`<tr><td>${date(b.created_at)}</td><td>${({skus:'商品',warehouses:'仓库',opening:'期初库存'})[b.kind]}</td><td>${b.rows.length}</td><td>${phaseStatus(b.status)}</td></tr>`).join('')}</tbody></table></div></section>`;
}
function purchasePage(){
 if(!['admin','warehouse'].includes(state.user.role))return heading('采购与收货','当前岗位无采购操作权限。');
 return heading('采购与分批收货','到货凭证在同一采购单中唯一；待验与残次计入实收，但不能直接销售。','<button id="purchase-new" class="primary">＋ 新建采购单</button>')+phase.purchases.map(o=>`<section class="card form-card"><div class="card-head"><div><h2>${esc(o.code)} <span class="pill">${phaseStatus(o.status)}</span></h2><p class="muted">${esc(o.supplier)} · ${esc(wh(o.warehouse_id).name)} · ${date(o.created_at)}</p></div><div class="action-buttons">${['open','partial'].includes(o.status)?`<button class="secondary-button" data-close-order="${o.id}">关闭余量</button><button class="primary" data-receive-order="${o.id}">登记到货</button>`:''}</div></div>${o.note?`<p>${esc(o.note)}</p>`:''}<div class="table-wrap"><table><thead><tr><th>商品</th><th>采购数量</th><th>净实收</th><th>${o.status==='closed'?'已关闭未收':'剩余未收'}</th></tr></thead><tbody>${o.lines.map(l=>`<tr><td>${productCell(l.sku_id)}</td><td>${l.ordered}</td><td>${l.received}</td><td>${l.ordered-l.received}</td></tr>`).join('')}</tbody></table></div>${o.close_reason?`<p class="muted">关闭原因：${esc(o.close_reason)}</p>`:''}${(o.receipt_history||[]).length?`<details><summary>到货凭证与入账单据 (${o.receipt_history.length})</summary>${o.receipt_history.map(r=>`<p>${esc(r.reference)} · ${date(r.created_at)}</p><p class="muted">${r.document_ids.map(id=>`${esc(id)}${r.reversed_document_ids.includes(id)?'（已冲正）':''}`).join(' / ')}</p>`).join('')}</details>`:''}</section>`).join('')+(!phase.purchases.length?'<section class="card form-card empty">暂无采购单，点击右上角开始。</section>':'');
}
function approvalPage(){
 if(!['admin','warehouse'].includes(state.user.role))return heading('调整与冲正','当前岗位无调整申请权限。');
 return heading('调整与冲正','申请不改库存；另一位管理员审核通过后才入账，原始流水始终保留。','<div class="action-buttons"><button class="secondary-button" id="reversal-new">申请冲正</button><button class="primary" id="adjustment-new">申请调整</button></div>')+phase.approvals.map(a=>`<section class="card form-card"><div class="card-head"><h2>${a.kind==='reversal'?'单据冲正':'库存调整'} <span class="pill">${phaseStatus(a.status)}</span></h2>${a.status==='pending'&&state.user.role==='admin'&&!a.is_own?`<button class="primary" data-review="${a.id}">审核申请</button>`:''}</div><p>${esc(sku(a.sku_id).name)} · ${esc(wh(a.warehouse_id).name)}</p><p>${Object.entries(a.delta).filter(([,v])=>v).map(([k,v])=>`${esc(({g:'良品',q:'待验',d:'残次',offline:'线下额度'})[k]||k)} ${v>0?'+':''}${v}`).join(' / ')}</p><p>申请原因：${esc(a.reason)}</p><p class="muted">申请人：${esc(a.requester_name)} · ${date(a.created_at)}${a.is_own&&a.status==='pending'?' · 需由其他管理员审核':''}</p>${a.original_id?`<p class="muted">原单据：${esc(a.original_id)}</p>`:''}${a.document_id?`<p class="muted">执行单据：${esc(a.document_id)}</p>`:''}${a.review_note?`<p>审核意见：${esc(a.review_note)} · ${esc(a.reviewer_name)}</p>`:''}</section>`).join('')+(!phase.approvals.length?'<section class="card form-card empty">暂无申请。仓管可发起申请，再由管理员审核。</section>':'');
}
function phaseModal(title,html,submit){
 $('#editor-title').textContent=title;$('#editor-fields').innerHTML=html;$('#editor-error').textContent='';$('#editor-form button[type=submit]').textContent='确认提交';$('#editor').showModal();
 $('#editor-form').onsubmit=async e=>{e.preventDefault();if(state.busy)return;e.submitter.disabled=true;let saved=false;try{await submit(e.target);saved=true;$('#editor').close();await load();render();toast('操作已保存')}catch(err){if(saved)toast('已保存，请刷新列表查看');else if(err.unknown){$('#editor').close();render();toast(err.message)}else $('#editor-error').textContent=err.message}finally{e.submitter.disabled=false}};
}
const phaseInput=(name,label,value='',type='text')=>`<label>${label}<input name="${name}" type="${type}" value="${esc(value)}" required ${type==='number'?'step="1"':'maxlength="120"'}></label>`;
const phaseSkuSelect=()=>`<select name="sku_id">${state.meta.skus.map(s=>`<option value="${esc(s.id)}">${esc(s.code)} · ${esc(s.name)}</option>`).join('')}</select>`;
function purchaseEditor(){
 phaseModal('新建采购单',phaseInput('code','采购单号')+phaseInput('supplier','供应商')+`<label>收货仓库<select name="warehouse_id">${warehouseOptions(false,true)}</select></label><label>备注<textarea name="note" maxlength="500"></textarea></label><div id="purchase-lines"></div><button id="add-purchase-line" type="button" class="secondary-button">＋ 添加商品行</button>`,async form=>{const raw=Object.fromEntries(new FormData(form));const items=[...form.querySelectorAll('.purchase-line')].map(el=>({sku_id:el.querySelector('select').value,quantity:Number(el.querySelector('input').value)}));await workflowWrite('/purchases',{code:raw.code,supplier:raw.supplier,warehouse_id:raw.warehouse_id,note:raw.note,lines:items})});
 const add=()=>{const row=document.createElement('div');row.className='purchase-line form-grid';row.innerHTML=`<label>商品${phaseSkuSelect()}</label><label>采购数量<input type="number" min="1" max="1000000" step="1" value="1" required></label><button type="button" class="text-button">删除此行</button>`;row.querySelector('button').onclick=()=>row.remove();$('#purchase-lines').append(row)};$('#add-purchase-line').onclick=add;add();
}
function receiveEditor(order){
 const rows=order.lines.filter(l=>l.received<l.ordered);
 phaseModal('登记到货 · '+order.code,phaseInput('reference','到货凭证号（同单不可重复）')+`<p class="muted">仅填写本次实收数量。留0的商品不会入账。</p>`+rows.map(l=>`<fieldset data-sku="${esc(l.sku_id)}"><legend>${esc(sku(l.sku_id).name)} · 剩余 ${l.ordered-l.received}</legend><div class="form-grid">${['g','q','d'].map((f,i)=>`<label>${['良品','待验','残次'][i]}<input name="${f}" type="number" min="0" max="${l.ordered-l.received}" step="1" value="0" required></label>`).join('')}</div></fieldset>`).join(''),async form=>{const items=[...form.querySelectorAll('[data-sku]')].map(el=>({sku_id:el.dataset.sku,expected_version:state.stocks.find(s=>s.warehouse_id===order.warehouse_id&&s.sku_id===el.dataset.sku)?.version||0,...Object.fromEntries(['g','q','d'].map(f=>[f,Number(el.querySelector(`[name=${f}]`).value)]))})).filter(l=>l.g+l.q+l.d>0);await workflowWrite(`/purchases/${order.id}/receive`,{reference:form.elements.reference.value,expected_version:order.version,lines:items})});
}
function approvalEditor(kind){
 const documents=state.ledger.filter(l=>['receipt','sale','purchase_receipt','adjustment'].includes(l.kind));
 let html=kind==='adjustment'?`<label>仓库<select name="warehouse_id">${warehouseOptions(false,true)}</select></label><label>商品${phaseSkuSelect()}</label><p class="muted">填写增减量，例如 -2 表示减少2件。不会释放订单占用或线上额度。</p><div class="form-grid">${['g','q','d'].map((f,i)=>phaseInput(f,['良品增减','待验增减','残次增减'][i],0,'number')).join('')}</div>`:`<label>原单据（最近100条流水）<select name="original_id">${documents.map(l=>`<option value="${esc(l.document_id)}">${esc(l.document_id)} · ${esc(labelKind(l.kind))} · ${esc(sku(l.sku_id).name)}</option>`).join('')}</select></label><p class="muted">冲正会反向恢复原单据的库存与额度变化；采购收货冲正还会回退采购实收数量。</p>`;
 html+='<label>申请原因<textarea name="reason" minlength="2" maxlength="500" required></textarea></label>';
 phaseModal(kind==='adjustment'?'申请库存调整':'申请单据冲正',html,async form=>{const raw=Object.fromEntries(new FormData(form));let body;
 if(kind==='adjustment'){body={kind,warehouse_id:raw.warehouse_id,sku_id:raw.sku_id,expected_version:state.stocks.find(s=>s.warehouse_id===raw.warehouse_id&&s.sku_id===raw.sku_id)?.version||0,delta:{g:Number(raw.g),q:Number(raw.q),d:Number(raw.d)},reason:raw.reason}}
 else{const doc=documents.find(l=>l.document_id===raw.original_id);if(!doc)throw new Error('请选择可冲正的原单据');body={kind,original_id:raw.original_id,expected_version:state.stocks.find(s=>s.warehouse_id===doc.warehouse_id&&s.sku_id===doc.sku_id)?.version||0,reason:raw.reason}}
 await workflowWrite('/approvals',body)});
}
function phaseBind(){
 const retry=$('#workflow-retry');if(retry)retry.onclick=async()=>{retry.disabled=true;try{await workflowWrite(null,null,workflowPending());await load();render();toast('原请求已确认，未重复入账')}catch(e){toast(e.message);if(state.user)render()}finally{retry.disabled=false}};
 if($('#import-form')){
 $('#import-kind').onchange=e=>{$('#import-template').href='/api/v1/imports/template/'+e.target.value};
 $('#import-file').onchange=async e=>{const f=e.target.files[0];if(!f)return;if(f.size>1000000){toast('文件不能超过1MB');return}$('#import-text').value=await f.text()};
 $('#import-form').onsubmit=async e=>{e.preventDefault();e.submitter.disabled=true;try{phase.preview=await api('/imports/preview',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});render()}catch(err){$('#phase-error').textContent=err.message}finally{e.submitter.disabled=false}};
 }
 if($('#apply-import'))$('#apply-import').onclick=async e=>{e.target.disabled=true;try{await workflowWrite(`/imports/${phase.preview.id}/apply`,{});phase.preview=null;await load();render();toast('整批导入完成')}catch(err){toast(err.message);render()}};
 if($('#purchase-new'))$('#purchase-new').onclick=purchaseEditor;
 document.querySelectorAll('[data-receive-order]').forEach(b=>b.onclick=()=>receiveEditor(phase.purchases.find(o=>o.id===b.dataset.receiveOrder)));
 document.querySelectorAll('[data-close-order]').forEach(b=>b.onclick=()=>{const o=phase.purchases.find(o=>o.id===b.dataset.closeOrder);phaseModal('关闭采购单剩余数量','<p class="muted">已收库存保留，关闭后不能继续收货。</p><label>关闭原因<textarea name="reason" minlength="2" maxlength="500" required></textarea></label>',f=>workflowWrite(`/purchases/${o.id}/close`,{expected_version:o.version,reason:f.elements.reason.value}))});
 if($('#adjustment-new'))$('#adjustment-new').onclick=()=>approvalEditor('adjustment');if($('#reversal-new'))$('#reversal-new').onclick=()=>approvalEditor('reversal');
 document.querySelectorAll('[data-review]').forEach(b=>b.onclick=()=>{const a=phase.approvals.find(r=>r.id===b.dataset.review);phaseModal('审核库存申请',`<p>${esc(a.requester_name)}：${esc(a.reason)}</p><p class="muted">${esc(sku(a.sku_id).name)} · ${esc(wh(a.warehouse_id).name)}。若库存已变化，通过操作会被拒绝，应驳回并重新申请。</p><label>审核结果<select name="decision"><option value="approve">通过并入账</option><option value="reject">驳回，不改库存</option></select></label><label>审核意见<textarea name="note" minlength="2" maxlength="500" required></textarea></label>`,f=>workflowWrite(`/approvals/${a.id}/review`,Object.fromEntries(new FormData(f))))});
}
