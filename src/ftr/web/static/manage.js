const $ = s => document.querySelector(s);
const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {QUEUED:'等待执行',RUNNING:'执行中',APPLIED:'已生效',FAILED:'失败'};
const actions={'plan.save':'保存计划','plan.enable':'启用计划','plan.disable':'停用计划','plan.now':'立即获取一轮','source.pause':'暂停来源','source.resume':'恢复来源','review.draft':'保存草稿','review.submit':'提交人工结论','review.reopen':'开启新一轮复核','review.recover':'补取登记缺失内容'};
const quality={collected:'待复核',validated:'已复核',quarantined:'内容受限',rejected:'已排除'};
const downloads={saved:'已保存',failed:'下载失败',blocked:'访问或预算受限',pending:'待下载'};
const extraction={text:'文本已提取',unsupported:'格式暂不支持',scanned:'扫描件未解析',failed:'提取失败',pending:'待解析'};
let authorized = false, plan = null, current = null, category = 'pending', previewed = null, dirtyPlan = false, dirtyReview = false;
let busy = false, selected = null, lastCommand = null, lastState = null;
const pendingCommands = new Map();
export function selectReview(id) {if (id && id !== selected) {selected=id;dirtyReview=false;}}
async function get(path, body) {
  const response = await fetch(path, {cache:'no-store', ...(body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})});
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || data.error?.message || `请求失败（${response.status}）`);
  return data;
}
function message(text, error=false) {
  $('#management-message').textContent = text;
  $('#management-message').classList.toggle('error-banner',error);
}
export async function bootstrapManagement() {
  if (location.hash.startsWith('#manage-token=')) {
    const token = location.hash.slice('#manage-token='.length);
    history.replaceState(null,'','#scheduler');
    try {await get('/api/manage/session',{token});} catch(e) {message(e.message,true);}
  }
  const session = await get('/api/manage/session'); authorized = session.authorized;
  $('#run-now').disabled=true;
  document.querySelector('[data-manage="now-preview"]').disabled=!authorized;
  $('#management-mode').textContent = authorized ? '本机管理 · 操作留痕' : '只读 · 使用 workbench start --manage --open 打开管理入口';
}
function configFromForm() {
  const f = $('#schedule-form');
  return {enabled:$('#schedule-enabled').checked, timezone:f.elements.timezone.value,
    times:f.elements.times.value.split(/[,，\s]+/).filter(Boolean),lookback_days:Number(f.elements.lookback_days.value),
    batch_interval_seconds:Number(f.elements.batch_interval_seconds.value),
    retry_delays_seconds:f.elements.retry_delays_seconds.value.split(/[,，\s]+/).filter(Boolean).map(Number)};
}
async function command(action,payload,expected_revision) {
  if (!authorized) throw new Error('只读模式：请通过本机管理入口重新打开');
  const signature=JSON.stringify({action,payload,expected_revision});
  const id=pendingCommands.get(signature)||crypto.randomUUID().replaceAll('-','');
  pendingCommands.set(signature,id);
  const result = await get('/api/manage/commands',{id,action,payload,expected_revision});
  lastCommand=result.id;lastState='QUEUED';
  $('#management-operations').open=true;
  message(`操作 ${result.id.slice(0,8)} 已排队，等待后台实际执行。`);
  return result;
}
function form() {
  const s = plan.settings;
  $('#schedule-form').innerHTML = `<fieldset ${authorized?'':'disabled'}><label class="check"><input id="schedule-enabled" type="checkbox" ${s.enabled?'checked':''}>启用日常计划</label><div class="management-fields"><label>执行时间<input name="times" value="${esc(s.times.join(', '))}" required placeholder="09:00, 18:00"></label><label>时区<input name="timezone" value="${esc(s.timezone)}" required></label><label>回看天数（含当天）<input name="lookback_days" type="number" min="1" max="366" value="${s.lookback_days}" required></label></div><details><summary>高级设置</summary><div class="management-fields"><label>批次间隔（秒）<input name="batch_interval_seconds" type="number" min="1" value="${s.batch_interval_seconds}" required></label><label>跨批重试等待（秒，最多三次）<input name="retry_delays_seconds" value="${esc(s.retry_delays_seconds.join(', '))}"></label></div></details><div class="management-actions"><button class="button" type="button" data-manage="preview">预览计划</button><button class="button" type="submit" id="save-plan" disabled>确认预览并保存</button><button class="button subtle" type="button" data-manage="${s.enabled?'disable':'enable'}">${s.enabled?'停用计划':'启用计划'}</button></div></fieldset>`;
}
export async function refreshManagement(view) {
  if (busy) return;
  busy=true;
  try {
    const op = await get('/api/manage/operations');
    const observed=op.items.find(x=>x.id===lastCommand);
    const phase=observed?observed.state+(op.worker.activity?.state==='WAITING_LOCK'?':LOCK':''):null;
    if (observed && phase!==lastState) {lastState=phase;message(`操作 ${observed.id.slice(0,8)}：${labels[observed.state]}。${observed.result?.message||(observed.action==='plan.now'&&observed.state==='APPLIED'?'本轮窗口已合并；实际获取进度以来源与任务记录为准。':observed.state==='QUEUED'&&phase.endsWith(':LOCK')?'等待其他 CLI 释放写锁。':'')}`,observed.state==='FAILED');}
    for (const [signature,id] of pendingCommands) if(op.items.some(x=>x.id===id&&['APPLIED','FAILED'].includes(x.state)))pendingCommands.delete(signature);
    $('#operation-list').innerHTML = op.items.slice(0,20).map(x=>`<li><strong>${esc(labels[x.state]||x.state)}</strong> ${esc(actions[x.action]||x.action)} · ${esc(x.id.slice(0,8))}<small>${esc(x.result?.message||(x.result?.next_attempt_at?`部分内容已保存，下批 ${new Date(x.result.next_attempt_at).toLocaleTimeString()}`:x.result?.draft_saved?'草稿已保存，质量状态不变':x.result?.quality_state?`本轮结论：${({validated:'通过',rejected:'排除',quarantined:'需进一步核查'})[x.result.quality_state]}`:x.result?.revision?`已应用版本 ${x.result.revision}`:x.result?.needs_review?'补取完成，新版本需要重新复核':''))}</small>${x.result?.record_id?`<a href="#reviews/${esc(x.result.record_id)}">查看补取版本与复核</a>`:''}</li>`).join('') || '<li>尚无管理操作</li>';
    if (view === 'scheduler') {
      const [saved,status] = await Promise.all([get('/api/manage/plan'),get('/api/scheduler')]);
      const changed = !plan || saved.revision !== plan.revision;
      plan=saved;
      $('#plan-state').textContent = `来源：${saved.config_source} · 保存版本 ${saved.saved_revision??saved.revision} · 已应用版本 ${saved.applied_revision}${saved.pending_operation_id?' · 等待检查点应用':''} · 后台 ${saved.worker.state} · 心跳 ${status.heartbeat_fresh?'新鲜':'未观察到 / 已过期'}`;
      if (changed && !dirtyPlan || !$('#schedule-form').children.length) form();
      $('#next-schedule').textContent = status.next_run_at ? `下次执行：${new Date(status.next_run_at).toLocaleString()} · ${status.next_window.date_from} 至 ${status.next_window.date_to}` : '日常计划已停用；可以明确立即获取一轮。';
      $('#source-controls').innerHTML = ['mof','chinatax'].map(source=>{
        const state=status.sources.find(x=>x.source_id===source)||{};
        return `<article class="management-card"><h3>${source==='mof'?'财政部':'国家税务总局'}</h3><p>日期口径：${source==='mof'?'栏目日期':'成文日期'}</p><p>${state.user_paused?'来源由本人暂停':state.paused_reason?`来源受阻：${esc(state.paused_reason)}`:state.active_task_id?'正在采集 / 检查点保留':'等待下一轮'}</p><p>活动窗口：${state.prepared_request?esc(state.prepared_request.date_from+' 至 '+state.prepared_request.date_to):'无'} · 已扫描 ${state.report?.sources?.[0]?.pages_scanned||0} 页 · 保存 ${state.report?.sources?.[0]?.documents_saved||0} 条</p><p>待补漏：${state.pending_window?esc(state.pending_window.date_from+' 至 '+state.pending_window.date_to):'无'}</p>${state.active_task_id?`<a href="#tasks/${esc(state.active_task_id)}">查看当前任务</a>`:''}<p>${esc(state.last_result?.status||'尚无执行结果')} · ${state.report?.completion?.coverage==='COMPLETE'?'采集完成 · 待复核另行处理':''}</p><button class="button subtle" ${authorized?'':'disabled'} data-managed-source="${source}" data-source-action="${state.user_paused||state.paused_reason?'resume':'pause'}" data-revision="${state.management_revision||0}">${state.user_paused||state.paused_reason?'处理后恢复来源':'暂停来源'}</button></article>`;
      }).join('');
    } else {
      const queue=await get('/api/reviews?category='+category);
      $('#review-list').innerHTML=queue.items.map(x=>`<button class="review-row ${selected===x.record_id?'selected':''}" data-review-id="${esc(x.record_id)}"><strong>${esc(x.title)}</strong><small>${esc(quality[x.quality_state]||x.quality_state)} · 第 ${x.round} 轮</small></button>`).join('')||'<p class="empty-state">当前队列没有资料</p>';
      if (!selected && queue.items.length) selected=queue.items[0].record_id;
      if (selected && !dirtyReview) await detail(selected);
    }
  } catch(e) {message(e.message,true);} finally {busy=false;}
}
async function detail(id, force=false) {
  const data=await get('/api/reviews/'+encodeURIComponent(id)); if (!force && current?.record_id===id && current.revision===data.revision && current.input_digest===data.input_digest && current.latest===data.latest) return;
  current=data; selected=id;
  const d=data.item, draft=data.draft;
  const stale=draft && draft.input_digest!==data.input_digest;
  $('#review-detail').innerHTML=`<header><h2>${esc(d.title)}</h2><p>第 ${data.round} 轮 · ${data.latest?'最新版本':'历史版本'} · ${esc(quality[d.quality_state]||d.quality_state)}</p><div class="management-actions"><a href="#policies/${esc(id)}">资料与历史版本</a><a href="#tasks/${esc(data.task_id)}">原采集任务</a><a href="/api/evidence/${esc(d.evidence_id)}/download" target="_blank" rel="noopener">查看登记原件</a></div></header>${!data.latest||stale?'<p class="error-banner">资料已有新版本或草稿摘要过期。草稿已保留；请重新检查正文和证据后再提交。</p>':''}<p class="monitor-note">通过仅表示采集质量符合门槛，不表示法律效力或企业适用性。</p><details open><summary>正文</summary><pre class="review-body">${esc(d.body_text||'正文缺失')}</pre></details><section><h3>附件与限制</h3>${d.attachments.map(a=>`<p>${esc(a.label)} · 下载 ${esc(downloads[a.download_state]||a.download_state)} · 解析 ${esc(extraction[a.extraction_state]||a.extraction_state)} ${a.evidence_id?`<a href="/api/evidence/${esc(a.evidence_id)}/download" target="_blank" rel="noopener">原件</a>`:''}</p>`).join('')||'<p>无登记附件</p>'}<p>${esc(d.limitations.join('；')||'未标记内容限制')}</p></section><form id="review-form"><fieldset ${authorized?'':'disabled'}><label>复核结论<select name="result"><option value="PASS" ${data.can_pass?'':'disabled'}>通过</option><option value="REJECT">排除</option><option value="UNCERTAIN">需进一步核查</option></select></label><label>理由<textarea name="reasons" rows="4" required>${esc(draft?.reasons?.join('\n')||'')}</textarea></label><label class="check"><input type="checkbox" name="confirmed">已逐项核对正文、登记原件及全部附件证据</label><p>${data.can_pass?'':'存在硬性内容限制或内容不完整，禁止通过。'}</p><div class="management-actions"><button class="button" type="button" data-manage="draft" ${data.submitted?'disabled':''}>保存草稿</button><button class="button" type="submit" ${data.submitted||!data.latest?'disabled':''}>提交人工结论</button><button class="button subtle" type="button" data-manage="reopen" ${data.submitted?'':'disabled'}>开启新一轮复核</button><button class="button subtle" type="button" data-manage="recover" ${data.can_recover?'':'disabled'}>补取已登记缺失内容</button></div></fieldset></form><details><summary>历史结论与证据快照（${data.history.length}）</summary>${data.history.map(x=>`<article><p>第 ${x.round} 轮 · ${esc(x.actor)} · ${esc(x.submitted_at)}</p><pre>${esc(JSON.stringify(x.result_json,null,2))}</pre></article>`).join('')||'<p>尚无本机处理记录；既有决策可在采集任务中追溯。</p>'}</details>`;
  $('#review-form').elements.result.value = draft?.result || (data.can_pass?'PASS':'UNCERTAIN');
}
function reviewPayload(submit) {
  const f=$('#review-form');
  if (submit && !f.elements.confirmed.checked) throw new Error('请先确认已经核对当前资料的全部登记证据');
  return {record_id:current.record_id,input_digest:current.input_digest,result:f.elements.result.value,
          reasons:f.elements.reasons.value.split('\n').filter(x=>x.trim()),evidence_ids:submit?current.evidence_ids:[]};
}
async function action(event) {
  const button=event.target.closest('[data-manage],[data-source-action],[data-review-id]');
  if (!button || button.disabled) return;
  try {
    if (button.dataset.reviewId) {dirtyReview=false;await detail(button.dataset.reviewId,true);return;}
    button.disabled=true;
    if (button.dataset.sourceAction) await command('source.'+button.dataset.sourceAction,{source_id:button.dataset.managedSource},Number(button.dataset.revision));
    const a=button.dataset.manage;
    if (a==='preview') {const value=configFromForm(); const data=await get('/api/manage/preview',value);previewed=JSON.stringify(value); $('#schedule-preview').textContent=data.upcoming.map(x=>`${new Date(x.at).toLocaleString('zh-CN',{timeZone:value.timezone})} (${value.timezone})：${x.window.date_from} 至 ${x.window.date_to}`).join('；');$('#save-plan').disabled=false;}
    if (a==='enable'||a==='disable') await command('plan.'+a,{},plan.revision);
    if (a==='now-preview') {const p=await get('/api/manage/preview',plan.settings); const sources=[...document.querySelectorAll('[name="now-source"]:checked')].map(x=>x.value); if(!sources.length)throw new Error('至少选择一个来源');$('#run-window').textContent=`本轮 ${p.startup_window.date_from} 至 ${p.startup_window.date_to} · ${sources.join(', ')}。合并已有补漏，不改变活动任务范围。`; $('#run-now').dataset.window=JSON.stringify({...p.startup_window,sources});$('#run-now').disabled=false;}
    if (a==='now') {await command('plan.now',JSON.parse(button.dataset.window),plan.revision);button.dataset.window='';}
    if (a==='draft') {await command('review.draft',reviewPayload(false),current.revision);dirtyReview=false;}
    if (a==='reopen'||a==='recover') {await command('review.'+a,{record_id:current.record_id,input_digest:current.input_digest},current.revision);dirtyReview=false;}
  } catch(e) {message(e.message,true);} finally {if (button.dataset.manage!=='now') button.disabled=!authorized;}
}
document.addEventListener('click',action);
document.addEventListener('input',e=>{if(e.target.closest('#schedule-form')){dirtyPlan=true;previewed=null;$('#save-plan').disabled=true;}if(e.target.closest('#review-form'))dirtyReview=true;});
document.addEventListener('change',e=>{if(e.target.id==='review-category'){category=e.target.value;selected=null;dirtyReview=false;refreshManagement('reviews');}if(e.target.name==='now-source')$('#run-now').disabled=true;});
document.addEventListener('submit',async e=>{
  if(!['schedule-form','review-form'].includes(e.target.id))return;
  e.preventDefault();const button=e.target.querySelector('[type="submit"]');button.disabled=true;
  try {if(e.target.id==='schedule-form'){const value=configFromForm();if(JSON.stringify(value)!==previewed)throw new Error('请先预览当前计划');await command('plan.save',value,plan.revision);dirtyPlan=false;}else{await command('review.submit',reviewPayload(true),current.revision);dirtyReview=false;}}catch(error){message(error.message,true);button.disabled=false;}
});
