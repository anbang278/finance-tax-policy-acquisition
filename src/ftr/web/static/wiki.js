const $=s=>document.querySelector(s);
const escape=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const label={queued:'待启动',api_queued:'等待 API',awaiting_result:'等待宿主回传',running:'API 执行中',completed:'加工完成，待人工审核',stale:'输入或编辑冲突',failed:'失败',uncertain:'远端结果不确定，请显式重试',cancelled:'已取消',pending:'待处理',claimed:'已建立修订任务',dismissed:'人工判定无影响',resolved:'已核查',checked:'依赖可用',needs_review:'依赖待核查',blocked:'依赖受限',unknown:'尚未核查'};
let authorized=false,overview=null,tab='domains',current=null,currentJob=null,dirty=false;
let detailRevision = 0;
let selectedDetail = null;
function message(t){$('#wiki-message').textContent=t;}
async function request(url,body){const r=await fetch(url,{cache:'no-store',...(body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});const d=await r.json();if(!r.ok)throw Error(d.error?.message||d.detail||`请求失败 ${r.status}`);return d;}
function writes(){document.querySelectorAll('[data-write]').forEach(b=>b.disabled=!authorized);}
async function command(operation,args={}){if(!authorized)throw Error('请通过本机管理入口打开');const r=await request('/api/manage/commands',{id:crypto.randomUUID().replaceAll('-',''),action:'knowledge.apply',payload:{operation,arguments:args}});message(`操作已入队 ${r.id}，等待后台实际生效`);await operations();return r;}
async function operations(){const d=await request('/api/manage/operations');$('#wiki-operations').innerHTML=d.items.slice(0,30).map(o=>`<p>${escape(o.action)} · ${escape(o.state)} ${escape(o.result?.code||'')} ${escape(o.result?.message||'')}${o.result?.results?escape(JSON.stringify(o.result.results)):''}</p>`).join('');}
function card(title,subtitle,attrs=''){return `<div class="wiki-card"><button ${attrs}>${escape(title)}</button><small>${escape(subtitle)}</small></div>`;}
function lists(){if(!overview?.available){$('#wiki-list').textContent=overview?.message||'尚无内容';return;}const objects=overview.objects;
 if(tab==='domains')$('#wiki-list').innerHTML=overview.domains.map(d=>`<h2>${escape(d.data.name)}</h2><p>${escape(d.data.scope)}</p>${overview.domain_topics.filter(x=>x.domain_id===d.id).map(rel=>{const t=overview.topics.find(t=>t.id===rel.topic_id);const os=objects.filter(o=>o.topic_id===rel.topic_id);return card(t?.data.name||rel.topic_id,`${os.filter(o=>o.current_version&&o.dependency_state==='checked').length} 项可用内容 · ${os.filter(o=>o.current_version&&o.dependency_state!=='checked').length} 项待核 · ${os.filter(o=>!o.current_version).length} 项候选`, `data-topic="${escape(rel.topic_id)}"`);}).join('')}`).join('')||'<p>尚未配置领域。可在加工与维护中建立导航。</p>';
 if(['items','pages'].includes(tab))$('#wiki-list').innerHTML=objects.filter(o=>o.kind===(tab==='items'?'item':'page')).map(o=>`<label><input type="checkbox" data-batch-version="${escape(o.candidate_version)}" data-batch-object="${escape(o.id)}">选择此版本</label>`+card(o.title,o.current_version?(label[o.dependency_state]||'有正式版本'):'候选待审核',`data-object="${escape(o.id)}"`)).join('')||'<p>尚无内容。先创建加工任务并回传结果。</p>';
 if(tab==='jobs')$('#wiki-list').innerHTML=overview.jobs.map(j=>card(j.job_type,label[j.state]||j.state,`data-job="${escape(j.id)}"`)).join('')||'<p>尚无加工任务。</p>';
 if(tab==='impacts')$('#wiki-list').innerHTML=overview.impacts.map(i=>card(i.reason,label[i.state],`data-impact="${escape(i.id)}"`)).join('')||'<p>未发现待处理变化。</p>';
 document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));
}
function citations(content){let ids=content.citations||[];return ids.map(a=>`<p><button data-segment="${escape(a.segment_id)}">查看${a.source_part==='attachment'?'附件':'正文'}依据${a.page_number?' · 第 '+a.page_number+' 页':''}</button> <q>${escape(a.quote)}</q></p>`).join('');}
function contentView(v){const c=v.content;if(c.sections)return `<p>${escape(c.overview)}</p><p>${escape(c.coverage)}</p>${c.sections.map(s=>`<h3>${escape(s.title)}${s.human_maintained?' · 人工维护':''}</h3><p>${escape(s.text)}</p><small>知识版本 ${s.knowledge_versions.map(escape).join('、')}</small>${s.knowledge_versions.map(id=>`<button data-knowledge-version="${escape(id)}">阅读知识与依据</button>`).join('')}`).join('')}<p class="wiki-warning">证据缺口：${escape(c.limitations.join('；')||'未报告；仍需核查覆盖')}</p>`;
return `<h3>${escape(c.statement)}</h3><p>性质：${escape(c.nature)}</p><p>条件：${escape(c.conditions.join('；'))}</p><p>例外：${escape(c.exceptions.join('；'))}</p><p>地区：${escape(c.regions?.join('、')||'未知')}；主体：${escape(c.subjects?.join('、')||'未知')}</p><p>适用期间：${escape(c.valid_from||'未知')} 至 ${escape(c.valid_to||(c.valid_to_kind==='open_ended'?'未规定终止':'待核实'))} · 人工核验：${c.applicability_verified?'是':'否'}</p><p>${escape(c.reasoning)}</p>${temporalView(c)}${citations(c)}<p class="wiki-warning">证据缺口：${escape(c.limitations.join('；')||'未报告；仍需核查覆盖')}</p>`;}
async function object(id) {
  const revision = ++detailRevision;
  selectedDetail = {kind: 'object', id};
  history.replaceState(null, '', '#object/' + encodeURIComponent(id));
  const o = await request('/api/knowledge/objects/' + encodeURIComponent(id));
  if (revision !== detailRevision) return;
  current = o;
  currentJob = null;
  dirty = false;
const published=o.versions.find(v=>v.id===o.current_version);$('#wiki-detail').innerHTML=`<h2>${escape(published?.title||o.title)}</h2><p class="wiki-status">${escape(!o.current_version&&o.preview_available?'候选来源可用，等待审核':label[o.dependency_state])} · 人工审核 ${escape(({approved:'通过',withdrawn:'已撤回',rejected:'退回',draft:'待核对'})[o.review_state]||o.review_state)} · 编辑修订 ${o.edit_revision}</p><p>${published?(o.formally_available?'当前正式版本 ':'发布历史版本（当前受限） ')+escape(published.id):'尚无正式版本；以下为候选内容'}</p>${published?contentView(published):''}<h3>候选与历史版本</h3>${o.versions.map(v=>`<section class="wiki-version"><small>${escape(v.id)} · ${escape(v.created_at)} · ${escape(v.origin)}</small><details ${!published?'open':''}><summary>阅读版本</summary>${contentView(v)}</details><div class="wiki-actions"><button data-diff="${escape(v.id)}">查看差异</button><button data-edit="${escape(v.id)}" data-write>编辑为新草稿</button><button data-precheck="${escape(v.id)}" data-write>发起 AI 预审</button><button data-review="${escape(v.id)}" data-decision="approved" data-write>人工审核通过</button><button data-review="${escape(v.id)}" data-decision="rejected" data-write>退回</button><button data-review="${escape(v.id)}" data-decision="withdrawn" data-write>撤回审核</button><button data-publish="${escape(v.id)}" data-write>${o.kind==='page'?'发布 Wiki':'启用正式知识'}</button><a href="/api/knowledge/objects/${encodeURIComponent(o.id)}/export?version=${encodeURIComponent(v.id)}&preview=true">导出 Markdown</a></div></section>`).join('')}<label>审核理由<textarea id="wiki-review-reason" placeholder="说明条件、例外、适用范围和依据核对情况"></textarea></label><label><input id="wiki-confirm" type="checkbox">我已核对该版本及其全部依据，确认本次操作</label><h3>审核与预审记录</h3>${o.reviews.map(r=>`<p>${escape(r.actor)} · ${escape(r.decision)} · ${escape(r.reason)}</p>`).join('')||'<p>尚无审核记录</p>'}<div id="wiki-editor"></div><pre id="wiki-diff" hidden></pre>`;writes();}
async function job(id) {
  const revision = ++detailRevision;
  selectedDetail = {kind: 'job', id};
  history.replaceState(null, '', '#job/' + encodeURIComponent(id));
  const j = await request('/api/knowledge/jobs/' + encodeURIComponent(id));
  if (revision !== detailRevision) return;
  currentJob = j;
  current = null;
  dirty = false;
$('#wiki-detail').innerHTML=`<h2>加工任务 · ${escape(j.job_type)}</h2><p class="wiki-status">${escape(label[j.state])}</p><p>任务 ${escape(j.id)}</p><p>输入摘要 ${escape(j.input_digest)}</p><p class="wiki-warning">${escape(j.error?.code||'')} ${escape(j.error?.message||'候选生成后仍需人工审核与正式启用/发布。')}</p><div class="wiki-actions"><button data-job-start="host" data-write>交给 Codex/Hermes</button><button data-job-start="api" data-write>使用配置的模型 API</button><button data-job-action="retry" data-write>新建尝试 / 更新输入</button><button data-job-action="cancel" data-write>取消任务</button><button id="wiki-package">下载任务包</button></div><label>提交宿主加工结果<input id="wiki-result-file" type="file" accept="application/json,.json" data-write></label><h3>冻结输入</h3><p>资料 ${j.package.sources.length} 份；知识 ${j.package.knowledge_versions.length} 条</p>${j.result?.versions?.map(id=>`<p>已保存候选 ${escape(id)}</p>`).join('')||''}<p>预审问题：${escape(j.result?.findings?.join('；')||'无回传')}</p>`;writes();}
function editVersion(id){detailRevision++;const v=current.versions.find(v=>v.id===id);let fields='';if(current.kind==='item')fields=`<label>核心陈述<textarea name="statement">${escape(v.content.statement)}</textarea></label>${['conditions','exceptions','limitations'].map(k=>`<label>${({conditions:'条件',exceptions:'例外',limitations:'缺口'})[k]}（每行一项）<textarea name="${k}">${escape(v.content[k].join('\n'))}</textarea></label>`).join('')}`;
if(current.kind==='item')fields+=`<label>适用开始日期<input name="valid_from" type="date" value="${escape(v.content.valid_from||'')}"></label><label>适用结束日期<input name="valid_to" type="date" value="${escape(v.content.valid_to||'')}"></label><label>终止日期状态<select name="valid_to_kind"><option value="unknown">待核实</option><option value="specified" ${v.content.valid_to_kind==='specified'?'selected':''}>明确日期</option><option value="open_ended" ${v.content.valid_to_kind==='open_ended'?'selected':''}>未规定终止</option></select></label><label><input name="applicability_verified" type="checkbox" ${v.content.applicability_verified?'checked':''}>我已对照依据核验上述适用期间</label>`;
else fields=v.content.sections.map((s,i)=>`<label>章节：${escape(s.title)}<textarea name="section-${i}">${escape(s.text)}</textarea></label><label><input name="human-${i}" type="checkbox" ${s.human_maintained?'checked':''}>人工维护</label><label>本节使用的知识<select name="knowledge-${i}" multiple>${[...new Set([...s.knowledge_versions,...overview.objects.filter(o=>o.kind==='item'&&o.current_version).map(o=>o.current_version)])].map(id=>{const obj=overview.objects.find(o=>o.current_version===id);return `<option value="${escape(id)}" ${s.knowledge_versions.includes(id)?'selected':''}>${escape(obj?.title||'历史知识版本')} · ${escape(id.slice(0,8))}</option>`;}).join('')}</select></label>`).join('');$('#wiki-editor').innerHTML=`<form id="wiki-edit-form" data-version="${escape(id)}"><h3>保存新草稿</h3><label>标题<input name="title" value="${escape(current.title)}"></label>${fields}<p>引用保持原版本，新增实质结论须先形成有依据的知识。</p><button data-write>保存新草稿</button></form>`;writes();dirty=true;}
async function refresh(){if(document.hidden)return;overview=await request('/api/knowledge/overview');lists();const index=overview.index?.index?JSON.parse(overview.index.index):null;$('#wiki-index-status').innerHTML=index?`<p>${index.mode==='substring'?'精确子串检索（全文索引不可用）':'中文全文检索与短词匹配'} · ${index.issues?.length||0} 项解析缺口</p>${(index.issues||[]).map(i=>`<p>${escape(i.code)} · ${escape(i.message)} <a href="/#reviews/${encodeURIComponent(i.record_id)}">查看资料与原件</a>${i.code==='EXTRACTION_CHANGED'?`<button data-refresh-record="${escape(i.record_id)}" data-write>重新解析 PDF 并生成待复核版本</button>`:''}</p>`).join('')}`:'<p>尚未建立证据索引。加工前请显式重建索引。</p>';if(overview.available){const existing=$('#wiki-topic').value;$('#wiki-topic').innerHTML=overview.topics.map(t=>`<option value="${escape(t.id)}">${escape(t.data.name)}</option>`).join('');if(existing)$('#wiki-topic').value=existing;
const materials=await request('/api/knowledge/materials');$('#wiki-materials').innerHTML=materials.items.map(r=>`<label><input type="checkbox" name="record" value="${escape(r.id)}">${escape(r.title)}</label>`).join('')||'<p>没有可用的已复核资料。请先核对采集内容和资料复核状态。</p>';
renderKnowledgeChoices();}
for(const [selector,values,all] of [['#wiki-search-domain',overview.domains||[],'全部领域'],['#wiki-search-topic',overview.topics||[],'全部专题']]){const select=$(selector);const old=select.value;select.innerHTML=`<option value="">${all}</option>`+values.map(x=>`<option value="${escape(x.id)}">${escape(x.data.name)}</option>`).join('');select.value=old;}await operations();writes();}
$('#wiki-search').onsubmit=async e=>{e.preventDefault();try{const f=new FormData(e.target);f.set('include_limited',e.target.elements.include_limited.checked?'true':'false');const q=new URLSearchParams([...f].filter(([,v])=>v));const d=await request('/api/knowledge/search?'+q);$('#wiki-list').innerHTML=d.items.map(i=>card(i.title,`${i.kind} · ${label[i.dependency_state]}`,i.kind==='material'?`data-material="${escape(i.id)}"`:`data-object="${escape(i.id)}"`)).join('')||'<p>无匹配的可用结果。可检查范围、适用时点或受限资料。</p>';message(d.limitations?.join('；')||'检索完成');}catch(e){message(e.message);}};
$('#wiki-job-form').onsubmit=async e=>{e.preventDefault();try{const f=e.target;const a={job_type:f.elements.job_type.value,topic_id:f.elements.topic_id.value,records:[...f.querySelectorAll('[name=record]:checked')].map(x=>x.value),knowledge_versions:[...f.querySelectorAll('[name=knowledge]:checked')].map(x=>x.value),output_mode:['compile','revise'].includes(f.elements.job_type.value)?f.elements.output_mode.value:'candidate',max_calls:Number(f.elements.max_calls.value)};if(f.elements.use_current.checked){if(!current)throw Error('请先选择要修订的对象');a.object_id=current.id;}await command('job.create',a);}catch(e){message(e.message);}};
document.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b||b.disabled)return;try{if(b.dataset.tab){tab=b.dataset.tab;history.replaceState(null,'','#tab/'+tab);lists();}if(b.dataset.object)await object(b.dataset.object);if(b.dataset.job)await job(b.dataset.job);if(b.dataset.material)location.href='/#policies/'+encodeURIComponent(b.dataset.material);if(b.dataset.topic){detailRevision++;selectedDetail=null;current=null;currentJob=null;dirty=false;const t=overview.topics.find(t=>t.id===b.dataset.topic);$('#wiki-detail').innerHTML=`<h2>${escape(t.data.name)}</h2><p>问题：${escape(t.data.questions?.join('；'))}</p>${overview.objects.filter(o=>o.topic_id===t.id).map(o=>card(o.title,o.current_version?'正式版本可阅读':'候选待审核',`data-object="${escape(o.id)}"`)).join('')||'<p>覆盖缺口：尚无内容，请选择资料创建加工任务。</p>'}`;$('#wiki-topic').value=t.id;}
if(b.dataset.operation)await command(b.dataset.operation);if(b.dataset.refreshRecord)await command('index.refresh',{id:b.dataset.refreshRecord});
if(b.dataset.batch){if(!$('#wiki-batch-confirm').checked)throw Error('请先确认所选版本');const selected=[...document.querySelectorAll('[data-batch-version]:checked')];if(!selected.length)throw Error('请先勾选版本');const ops=selected.map(x=>{const o=overview.objects.find(o=>o.id===x.dataset.batchObject);return b.dataset.batch==='review'?{operation:'review',arguments:{version_id:x.dataset.batchVersion,decision:'approved',reason:$('#wiki-batch-reason').value,confirmed:true}}:{operation:'publish',arguments:{version_id:x.dataset.batchVersion,expected_current:o.current_version,expected_revision:o.edit_revision,confirmed:true}};});await command('batch',{operations:ops});}
if(b.dataset.segment){const s=await request('/api/knowledge/segments/'+b.dataset.segment);$('#wiki-evidence-content').innerHTML=`<h2>${s.source_part==='attachment'?'附件':'正文'}依据</h2><p>资料 ${escape(s.record_id)} · 页 ${escape(s.page_number||'不适用')}</p><a href="/api/evidence/${encodeURIComponent(s.evidence_id)}/download" target="_blank" rel="noopener">下载原件</a><pre>${escape(s.text)}</pre>`;$('#wiki-evidence').showModal();}
if(b.dataset.knowledgeVersion){const o=overview.objects.find(o=>o.current_version===b.dataset.knowledgeVersion);if(o)await object(o.id);else await openKnowledgeVersion(b.dataset.knowledgeVersion);}
if(b.dataset.jobStart)await command('job.start',{id:currentJob.id,executor:b.dataset.jobStart});if(b.dataset.jobAction)await command('job.'+b.dataset.jobAction,{id:currentJob.id});
if(b.id==='wiki-package'){const p=await request('/api/knowledge/jobs/'+currentJob.id+'/package');download(JSON.stringify(p,null,2),'task-package.json','application/json');}
if(b.dataset.precheck)await command('job.create',{job_type:'precheck',topic_id:current.topic_id,records:[],target_version:b.dataset.precheck,object_id:current.id,output_mode:current.kind==='page'?'preview':'candidate'});
if(b.dataset.review){if(!$('#wiki-confirm').checked)throw Error('请先确认已经核对版本与依据');await command('review',{version_id:b.dataset.review,decision:b.dataset.decision,reason:$('#wiki-review-reason').value,confirmed:true});}
if(b.dataset.publish){if(!$('#wiki-confirm').checked)throw Error('请先确认本次启用或发布');await command('publish',{version_id:b.dataset.publish,expected_revision:current.edit_revision,expected_current:current.current_version,confirmed:true});}
if(b.dataset.diff){const d=await request(`/api/knowledge/objects/${current.id}/diff/${b.dataset.diff}`);$('#wiki-diff').hidden=false;$('#wiki-diff').textContent=d.diff||'无内容差异';}
if(b.dataset.edit)editVersion(b.dataset.edit);
if(b.dataset.impact){detailRevision++;selectedDetail=null;current=null;currentJob=null;dirty=false;const i=overview.impacts.find(i=>i.id===b.dataset.impact);$('#wiki-detail').innerHTML=`<h2>变化核查</h2><p>${escape(i.reason)} · ${escape(label[i.state])}</p><pre>${escape(JSON.stringify(i.data,null,2))}</pre><label>归属专题<select id="wiki-impact-topic">${overview.topics.map(t=>`<option value="${escape(t.id)}">${escape(t.data.name)}</option>`).join('')}</select></label><label>核查理由<textarea id="wiki-impact-reason"></textarea></label><button data-resolve="${escape(i.id)}" data-write>建立加工或修订任务</button><button data-dismiss="${escape(i.id)}" data-write>记录为无内容影响</button><button data-revision-resolved="${escape(i.id)}" data-write>确认修订已发布</button>`;writes();}
if(b.dataset.resolve||b.dataset.dismiss||b.dataset.revisionResolved)await command('impact.resolve',{id:b.dataset.resolve||b.dataset.dismiss||b.dataset.revisionResolved,topic_id:$('#wiki-impact-topic').value,resolution:b.dataset.revisionResolved?'revision_published':b.dataset.dismiss?'no_impact':'create_job',reason:$('#wiki-impact-reason').value});
if(b.id==='wiki-evidence-close')$('#wiki-evidence').close();if (b.id === 'wiki-refresh') {
  const revision = detailRevision;
  const selected = selectedDetail;
  await refresh();
  if (revision !== detailRevision) return;
  if (!dirty && selected?.kind === 'object') await object(selected.id);
  else if (selected?.kind === 'job') await job(selected.id);
}
}catch(e){message(e.message);}});
document.addEventListener('change',async e=>{try{if(e.target.id==='wiki-domain-file'){const f=e.target.files[0];if(f)await command('domain.import',JSON.parse(await f.text()));}if(e.target.id==='wiki-result-file'){const f=e.target.files[0];if(!f)return;if(f.size>4*1024*1024)throw Error('结果超过 4 MiB');const r=await fetch('/api/manage/knowledge/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:await f.text()});const d=await r.json();if(!r.ok)throw Error(d.detail||d.error?.message);await command('job.submit',{blob_id:d.blob_id});}}catch(e){message(e.message);}});
document.addEventListener('submit',async e=>{if(e.target.id!=='wiki-edit-form')return;e.preventDefault();try{const f=e.target;const v=current.versions.find(v=>v.id===f.dataset.version);const c=structuredClone(v.content);if(current.kind==='item'){c.valid_from=f.elements.valid_from.value||null;c.valid_to=f.elements.valid_to.value||null;c.valid_to_kind=f.elements.valid_to_kind.value;c.applicability_verified=f.elements.applicability_verified.checked;c.statement=f.elements.statement.value;for(const key of ['conditions','exceptions','limitations'])c[key]=f.elements[key].value.split('\n').filter(x=>x.trim());}else c.sections.forEach((s,i)=>{s.text=f.elements['section-'+i].value;s.human_maintained=f.elements['human-'+i].checked;s.knowledge_versions=[...f.elements['knowledge-'+i].selectedOptions].map(o=>o.value);});await command('object.edit',{object_id:current.id,title:f.elements.title.value,content:c,expected_revision:current.edit_revision});dirty=false;}catch(e){message(e.message);}});
function download(text,name,type){const url=URL.createObjectURL(new Blob([text],{type}));const a=document.createElement('a');a.href=url;a.download=name;a.click();URL.revokeObjectURL(url);}
try{const s=await request('/api/manage/session');authorized=s.authorized;$('#wiki-mode').textContent=authorized?'本机管理 · 人工确认正式内容':'只读 · 从政策工作台管理入口进入';await refresh();const [kind,id]=location.hash.slice(1).split('/');if(kind==='object'&&id)await object(decodeURIComponent(id));if(kind==='job'&&id)await job(decodeURIComponent(id));if(kind==='tab'&&id){tab=id;lists();}}catch(e){message(e.message);}
setInterval(()=>{if(!document.hidden)operations().catch(e=>message(e.message));},5000);

function temporalView(content) {
  const times = (content.temporal_basis || []).map(basis =>
    `<p>时间依据 ${escape(basis.kind)} · ${escape(basis.value)}</p>${citations(basis)}`);
  const relations = (content.legal_relations || []).map(relation =>
    `<p>法律关系 ${escape(relation.kind)} · ${escape(relation.scope)}</p>${citations(relation)}`);
  return times.concat(relations).join('');
}

function renderKnowledgeChoices() {
  const preview = $('#wiki-job-form').elements.output_mode.value === 'preview';
  const eligible = overview.objects.filter(item => item.kind === 'item' &&
    (preview ? item.candidate_usable : item.current_version));
  $('#wiki-knowledge').innerHTML = eligible.map(item => {
    const version = preview ? item.candidate_version : item.current_version;
    return `<label><input type="checkbox" name="knowledge" value="${escape(version)}">${escape(item.title)} · ${preview ? '候选' : '正式'}</label>`;
  }).join('') || '<p>当前范围暂无可用知识版本。</p>';
}

async function openKnowledgeVersion(version) {
  const revision = ++detailRevision;
  selectedDetail = null;
  for (const item of overview.objects.filter(item => item.kind === 'item')) {
    const detail = await request('/api/knowledge/objects/' + encodeURIComponent(item.id));
    if (revision !== detailRevision) return;
    if (detail.versions.some(value => value.id === version)) {
      await object(item.id);
      return;
    }
  }
  message('该知识版本尚未找到，请刷新后重试。');
}

$('#wiki-job-form').elements.output_mode.addEventListener('change', renderKnowledgeChoices);
$('#wiki-policy-refresh').addEventListener('click', async () => {
  try {
    const result = await request('/api/knowledge/policies');
    $('#wiki-policy-groups').innerHTML = (result.items || []).map(group =>
      `<section class="wiki-version"><h3>${escape(group.identity.original_number || group.identity.title)}</h3><p>${escape(({unresolved:'文号待识别',candidate:'政策归组待核对'})[group.identity.identity_status])} · ${escape(({single_source:'单一来源',same_text:'正文一致',different_text:'正文存在差异'})[group.comparison])}</p>${group.sources.map(source =>
        `<p><a href="/#policies/${encodeURIComponent(source.record_id)}">查看资料</a> · ${escape(({validated:'资料已复核',collected:'资料待复核',quarantined:'资料受限',rejected:'资料已拒绝'})[source.quality_state])} · ${escape(source.limitations.join('、'))}</p>${source.attachments.map(attachment => `<p>${escape(attachment.label)} · ${escape(({saved:'原件已保存',pending:'等待下载',failed:'下载失败',blocked:'访问受限'})[attachment.download])} · ${escape(({text:'文本可读',unsupported:'格式待支持',scanned:'扫描件待解析',failed:'解析失败',pending:'等待解析'})[attachment.extraction])}</p>`).join('')}`).join('')}</section>`
    ).join('') || '<p>尚无政策身份记录。</p>';
  } catch (error) { message(error.message); }
});

let researchPreview = null;
$('#wiki-research-file').addEventListener('change', async event => {
  researchPreview = null;
  $('#wiki-research-download').disabled = true;
  $('#wiki-research-preview').textContent = '';
  try {
    const file = event.target.files[0];
    if (!file) return;
    if (file.size > 4 * 1024 * 1024) throw Error('报告超过4 MiB');
    researchPreview = await request('/api/manage/research/preview', JSON.parse(await file.text()));
    $('#wiki-research-status').textContent = researchPreview.report_state === 'needs_review' ? '报告待核实，请查看逐项检查和缺口' : 'AI检查完成，供使用者复核';
    $('#wiki-research-status').dataset.state = researchPreview.report_state;
    $('#wiki-research-preview').textContent = researchPreview.markdown;
    $('#wiki-research-download').disabled = false;
  } catch (error) {
    $('#wiki-research-status').textContent = error.message;
  }
});
$('#wiki-research-download').addEventListener('click', () => {
  if (researchPreview) download(researchPreview.markdown, 'research-report.md', 'text/markdown');
});
