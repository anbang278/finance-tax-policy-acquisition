import {bootstrapManagement, refreshManagement, selectReview} from './manage.js';
const reasonLabels = {missing_filter_date: '筛选日期缺失', missing_content: '标题或正文缺失', unreliable_body: '正文定位待确认', unknown_document_type: '资料类型待确认', attachment_not_saved: '附件未保存', primary_attachment_unparsed: '主附件未解析', primary_content_unparsed: '主内容未解析', pdf_extraction_warning: 'PDF 完整性待复核', other_historical: '其他／历史原因'};
const stopLabels = {BUDGET_REACHED: '达到本批预算', INTERRUPTED: '收到中断信号', INTERRUPTED_PREVIOUS_WRITER: '前次运行异常结束', PAUSE_REQUESTED: '收到暂停请求', CANCEL_REQUESTED: '收到取消请求', TRANSIENT_FAILURE_LIMIT: '连续网络失败达到上限', SOURCE_FAILURE: '来源失败待诊断', TRANSIENT_NETWORK: '来源网络失败', INCOMPLETE_QUEUE: '队列尚未完成'};
let uiConfig = {poll_interval_ms: 5000, request_timeout_ms: 10000};
const $ = (selector) => document.querySelector(selector);
const names = {
  source: {mof: '财政部', chinatax: '国家税务总局'},
  type: {policy_file: '政策文件', policy_announcement: '政策公告', official_interpretation: '官方解读', release_message: '发布消息', other: '其他资料', unknown: '类型未确定'},
  quality: {collected: '待复核', validated: '已复核', quarantined: '内容受限', rejected: '已排除'},
  task: {CREATED: '已创建', RUNNING: '运行中（数据库记录）', WAITING_DECISION: '等待复核', PARTIAL: '部分完成', COMPLETED: '已完成', COMPLETED_EMPTY: '完成 · 无发现条目', CANCELLED: '已取消'},
  queue: {PENDING: '待获取', SAVED: '已保存', FAILED: '获取失败', OUT_OF_SCOPE: '范围外', UNCHANGED_SKIP: '未变化跳过'},
  date: {column_date: '栏目日期', issued_date: '成文日期', published_date: '发布日期'},
  download: {saved: '已保存', failed: '下载失败', blocked: '访问受限', pending: '待下载'},
  extraction: {text: '已提取文本', unsupported: '格式暂不支持', scanned: '扫描件未解析', failed: '提取失败', pending: '待解析'},
  role: {primary: '主附件', supplement: '补充附件', unknown: '用途未确定'},
};
const paths = {
  library: '<path d="M4 4h4v16H4zM10 4h4v16h-4zM16 5l4-1 3 15-4 1z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  review: '<path d="M7 4h10v16H5V4h2M9 2h6v4H9zM8 13l2 2 5-5"/>',
  activity: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c-5 5-5 13 0 18M12 3c5 5 5 13 0 18"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  back: '<path d="M19 12H5m5-5-5 5 5 5"/>',
  refresh: '<path d="M20 10a8 8 0 1 0 0 5M20 4v6h-6"/>',
  search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
  file: '<path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
  external: '<path d="M14 3h7v7m0-7L11 13M10 4H4v16h16v-6"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4"/>',
};
const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.file}</svg>`;
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const label = (map, value) => map[value] || value || '未记录';
const badge = (kind, value) => `<span class="badge ${esc(value)}">${esc(label(names[kind], value))}</span>`;
const options = (map, all, selected = '') => `<option value="">${all}</option>` + Object.entries(map).map(([key, value]) => `<option value="${esc(key)}" ${selected === key ? 'selected' : ''}>${esc(value)}</option>`).join('');
const safeUrl = (url) => {try {const value = new URL(url); return ['http:', 'https:'].includes(value.protocol) ? value.href : null;} catch {return null;}};
const external = (url, text = '官方原文', className = '') => {
  const safe = safeUrl(url);
  return safe ? `<a href="${esc(safe)}" target="_blank" rel="noopener noreferrer" class="${className}">${esc(text)} ${icon('external')}</a>` : '<span>原文地址缺失或不可用</span>';
};
const stamp = (value) => {
  if (!value) return '未记录';
  const normalized = /Z$|[+-]\d{2}:\d{2}$/.test(value) ? value : value.replace(' ', 'T') + 'Z';
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN', {timeZone: 'Asia/Taipei', hour12: false});
};
const dateName = (value) => label(names.date, value);
const state = {
  view: 'policies', policyId: null, taskId: null, policyTab: 'body', taskTab: 'queue',
  policyPage: 1, taskPage: 1, queuePage: 1, eventPage: 1, queueState: '', queueSource: '', queueQuery: '',
  policy: null, versions: [], task: null, items: null, events: null, sources: [], lastAt: null,
  revision: 0, busy: false, pending: false, mobileDetail: false,
};

function updateNode(node, html) {
  if (node.dataset.rendered === html) return;
  const scrollTop = node.scrollTop;
  const active = node.contains(document.activeElement) ? document.activeElement : null;
  const focusId = active?.id;
  const selection = active && typeof active.selectionStart === 'number' ? [active.selectionStart, active.selectionEnd] : null;
  node.innerHTML = html;
  node.dataset.rendered = html;
  node.scrollTop = scrollTop;
  if (focusId) {
    const replacement = document.getElementById(focusId);
    replacement?.focus({preventScroll: true});
    if (selection && replacement?.setSelectionRange) replacement.setSelectionRange(...selection);
  }
}

async function api(path, params = {}) {
  const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value !== '' && value != null));
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), uiConfig.request_timeout_ms);
  try {
    const result = await fetch(path + (query.size ? '?' + query : ''), {cache: 'no-store', signal: controller.signal});
    const data = await result.json();
    if (!result.ok) throw new Error(data.error?.message || (result.status === 422 ? '筛选参数无效，请检查日期和筛选条件。' : `查询失败（HTTP ${result.status}）`));
    return data;
  } finally {clearTimeout(timeout);}
}

function pagination(data, target) {
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  return `<button data-page-target="${target}" data-page-number="${data.page - 1}" aria-label="上一页" ${data.page <= 1 ? 'disabled' : ''}>‹</button><span>${data.page} / ${pages} 页 · ${data.total} 条</span><button data-page-target="${target}" data-page-number="${data.page + 1}" aria-label="下一页" ${data.page >= pages ? 'disabled' : ''}>›</button>`;
}

function renderOverview(data) {
  $('#nav-policy-count').textContent = data.policies_total;
  $('#nav-task-count').textContent = data.tasks_total;
  const values = state.view === 'policies' ? [
    [data.policies_total, '已采集资料', ''], [data.quality_counts.validated || 0, '已复核', ''],
    [data.quality_counts.collected || 0, '待复核', 'attention'], [data.quality_counts.quarantined || 0, '内容受限', 'attention'],
  ] : [
    [data.tasks_total, '采集任务', ''], [data.task_counts.RUNNING || 0, '记录为运行中', ''],
    [data.task_counts.PARTIAL || 0, '部分完成', 'attention'], [data.task_counts.WAITING_DECISION || 0, '等待复核', 'attention'],
  ];
  const reasonCounts = state.view === 'policies' && data.quarantine_reasons?.length ? `<p class="body-note">内容受限原因（同一份资料可计入多项）：${data.quarantine_reasons.map((r) => `${esc(reasonLabels[r.code] || '其他／历史原因')} ${r.count}`).join(' · ')}</p>` : '';
  updateNode($('#overview'), values.map(([count, text, style]) => `<div class="stat ${style}"><strong>${count}</strong><span>${text}</span></div>`).join('') + reasonCounts);
}

function renderScheduler(data) {
  const configured = data.configured_settings;
  const effective = data.settings;
  const dateTime = (value) => value ? new Date(value).toLocaleString('zh-CN', {timeZone: effective.timezone}) : '—';
  const running = data.heartbeat_fresh ? '心跳正常' : '未运行或心跳过期';
  const taskLink = (id) => id ? `<a href="#tasks/${encodeURIComponent(id)}">查看关联任务</a>` : '';
  const sources = data.sources.map((source) => {
    const report = source.report;
    const request = report?.request;
    const completion = report?.completion;
    const completed = completion?.coverage === 'COMPLETE' && completion?.downloads === 'COMPLETE';
    const reasons = {MANUAL_PAUSE: '人工暂停', MANUAL_CANCEL: '人工取消', ACCESS_RESTRICTED: '官网访问受限', STRUCTURE_DRIFT: '页面结构变化', BROWSER_ERROR: '浏览器或显示环境故障', UNKNOWN: '原因待确认', EXECUTION_ERROR: '运行异常', RETRIES_EXHAUSTED: '网络重试已耗尽', NO_PROGRESS: '连续三批无进展'};
    const text = source.paused_reason ? `待处理：${reasons[source.paused_reason] || '原因待确认'}` : source.active_task_id ? '正在获取或等待续跑' : completed ? '采集完成' : '等待计划';
    const review = completion?.review === 'PENDING_OR_LIMITED' ? ' · 待复核或存在内容限制' : '';
    const pending = source.pending_window;
    return `<article class="scheduler-source"><strong>${esc(label(names.source, source.source_id))}</strong><p>${esc(text)}${esc(review)}</p>${request ? `<p>任务范围 ${esc(request.date_from)} — ${esc(request.date_to)} · ${source.source_id === 'mof' ? '栏目日期' : '成文日期'}</p>` : ''}${pending ? `<p>待补漏 ${esc(pending.date_from)} — ${esc(pending.date_to)}</p>` : ''}<p>已追加重试 ${source.retry_count} 次 · 下次尝试 ${esc(dateTime(source.next_attempt_at))}</p>${report ? `<p>已检查 ${report.sources[0]?.pages_scanned || 0} 页 · ${completion.coverage === 'COMPLETE' ? '范围核查完成' : '未读匹配数量未知'} · 缺失 ${report.missing_items.length} 项</p>` : ''}<p>${esc(source.next_step)}</p>${taskLink(source.active_task_id || source.last_task_id)}</article>`;
  }).join('');
  updateNode($('#scheduler-status'), `<div class="scheduler-heading"><strong>定时获取 · ${configured.enabled ? '配置已启用' : '配置关闭'}</strong><span>${esc(running)}</span></div><p>${esc(effective.timezone)} · 每天 ${esc(effective.times.join('、'))} · 每次回看 ${effective.lookback_days} 天 · 下次计划 ${esc(dateTime(data.next_run_at))}</p>${data.heartbeat?.activity?.state === 'DATA_LOCKED' ? '<p>资料目录正由其他写入任务使用，定时服务将在 60 秒后重试。</p>' : ''}${!data.config_matches ? '<p class="scheduler-warning">当前工作台配置与最近调度配置不同，请核对配置文件并重启相关服务。</p>' : ''}${!data.heartbeat_fresh && data.next_run_at ? '<p>下次计划仅为计算时点；需要启动定时服务才会执行。</p>' : ''}<div class="scheduler-sources">${sources || '<p>尚未启动定时服务。通过配置文件启用并运行 scheduler run。</p>'}</div>`);
}

function renderPolicies(data) {
  $('#policy-total').textContent = `${data.total} 份${data.date_unknown_count ? ` · 日期未知 ${data.date_unknown_count} 份（范围归属待确认）` : ''}`;
  let unknownGroup = false;
  const cards = data.items.map((d) => {
    const group = !d.listing_date && !unknownGroup ? '<p class="body-note">日期未知：资料已保存，无法确认是否属于指定日期区间。</p>' : '';
    if (!d.listing_date) unknownGroup = true;
    return group + `<button class="policy-card ${d.record_id === state.policyId ? 'selected' : ''}" data-policy="${esc(d.record_id)}" aria-pressed="${d.record_id === state.policyId}"><div class="card-top"><span>${esc(label(names.source, d.source_id))} · ${esc(label(names.type, d.document_type))}</span>${badge('quality', d.quality_state)}</div><h3>${esc(d.title || '标题缺失')}</h3><div class="card-bottom"><span class="number">${esc(d.document_number || '文号未提取')}</span><span>${esc(d.listing_date || '日期缺失')}</span></div></button>`; }).join('');
  updateNode($('#policy-list'), cards || '<div class="empty-state"><h2>没有匹配资料</h2><p>调整关键词或筛选条件后重试。</p></div>');
  updateNode($('#policy-pagination'), pagination(data, 'policyPage'));
}

function downloadLink(id, text) {
  return `<a href="/api/evidence/${encodeURIComponent(id)}/download" class="button" data-download>${icon('download')}${esc(text)}</a>`;
}

function renderBody(d) {
  if (!d.body_text) return '<div class="inline-empty">正文未提取，请查看附件或官方原文。</div>';
  if (d.body_display?.mode === 'structured') {
    const blocks = d.body_display.blocks.map((b) => {
      const align = ['center', 'right'].includes(b.align) ? b.align : 'left';
      const kind = b.type === 'heading' ? 'heading' : 'paragraph';
      return `<p class="body-block body-${kind} align-${align}">${esc(b.text)}</p>`;
    }).join('');
    return `<article class="policy-body structured-body">${blocks}</article>`;
  }
  return `<p class="body-note">无法恢复原文排版，以下展示已保存的提取文本。</p><article class="policy-body">${esc(d.body_text)}</article>`;
}

function renderPolicy(d, versions) {
  const header = `<div class="detail-header"><button class="mobile-back" data-back>${icon('back')}返回政策列表</button><div class="detail-eyebrow"><span>${esc(label(names.type, d.document_type))}</span>${badge('quality', d.quality_state)}<span>来源 v${d.source_version} · 提取 v${d.extraction_version}</span></div><h2>${esc(d.title || '标题缺失')}</h2><div class="doc-meta"><span>来源 <b>${esc(label(names.source, d.source_id))}</b></span><span>文号 <b>${esc(d.document_number || '未提取')}</b></span><span>${esc(dateName(d.listing_date_kind))} <b>${esc(d.listing_date || '缺失')}</b></span><span>成文日期 <b>${esc(d.issued_date || '未提取')}</b></span><span>发布日期 <b>${esc(d.published_date || '未提取')}</b></span></div><div class="origin-row"><div>官方原文${external(d.source_url, d.source_url || '')}</div><button class="button" data-task="${esc(d.task_id)}">${icon('activity')}关联任务</button><a class="button" href="#reviews/${esc(d.record_id)}">资料复核</a></div>${d.limitations.length ? `<div class="limitation">${(d.limitation_reasons || d.limitations.map((item) => ({label: item, original: item}))).map((item) => `<p>${esc(item.label)}${item.original !== item.label ? `：${esc(item.original)}` : ''}</p>`).join('')}</div>` : ''}</div>`;
  const tabs = [['body', '正文'], ['attachments', `附件 ${d.attachments.length}`], ['versions', `版本 ${versions.length}`], ['evidence', '证据']];
  let content = '';
  if (state.policyTab === 'body') {
    content = `<p class="body-note">以下为保存的实际提取文本；资料状态与政策法律效力分别判断。</p>${renderBody(d)}`;
  } else if (state.policyTab === 'attachments') {
    content = d.attachments.map((a) => `<article class="attachment"><h4>${icon('file')} ${esc(a.label || '未命名附件')}</h4><div class="doc-meta"><span>${esc(label(names.role, a.content_role))}</span><span>保存：${esc(label(names.download, a.download_state))}</span><span>解析：${esc(label(names.extraction, a.extraction_state))}</span></div><div class="queue-links">${a.download_state === 'saved' && a.evidence_id ? downloadLink(a.evidence_id, '下载保存原件') : '<span>暂无可下载的本地原件</span>'}${external(a.url, '来源附件')}</div>${a.text ? `<details class="attachment-text"><summary>查看已提取文本</summary><p>${esc(a.text)}</p></details>` : '<p class="body-note">该附件没有已提取文本。</p>'}</article>`).join('') || '<div class="inline-empty">这份资料没有登记直接附件。</div>';
  } else if (state.policyTab === 'versions') {
    content = `<p class="body-note">来源版本与提取版本分别记录；只展示数据库实际保存的版本。</p>` + versions.map((v) => `<div class="version-row ${v.record_id === d.record_id ? 'current' : ''}"><div><strong>来源 v${v.source_version} · 提取 v${v.extraction_version}</strong><p>${esc(v.record_id.slice(0, 12))} · ${esc(label(names.quality, v.quality_state))}</p></div>${v.record_id === d.record_id ? '<span class="badge">当前阅读</span>' : `<button class="button" data-version="${esc(v.record_id)}">阅读此版本</button>`}</div>`).join('');
  } else {
    const evidence = d.evidence;
    const rows = [['记录标识', d.record_id], ['正文证据', d.evidence_id], ['正文哈希', d.body_sha256 || '未记录'], ['原件保存时间', evidence ? stamp(evidence.retrieved_at) : '证据未登记'], ['原件类型', evidence?.media_type || '未记录'], ['原件大小', evidence ? `${evidence.size_bytes.toLocaleString()} 字节` : '未记录'], ['解析器版本', d.parser_version], ['来源效力标注', d.source_status_claim || '未提取'], ['字段出处', JSON.stringify(d.field_evidence)]];
    content = `<dl class="evidence-grid">${rows.map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join('')}</dl><p class="body-note">来源页面的效力标注按原记录展示，不代表系统已判定法律效力。</p>${evidence ? downloadLink(d.evidence_id, '下载保存的原始页面') : ''}`;
  }
  updateNode($('#policy-detail'), header + `<div class="detail-tabs" role="tablist" aria-label="资料内容">${tabs.map(([key, title]) => `<button role="tab" aria-selected="${state.policyTab === key}" class="${state.policyTab === key ? 'active' : ''}" data-policy-tab="${key}">${title}</button>`).join('')}</div><div class="detail-body">${content}</div>`);
  applyMobile();
}

function renderTasks(data) {
  updateNode($('#task-list'), data.items.map((t) => `<button class="task-card ${t.task_id === state.taskId ? 'selected' : ''}" data-task="${esc(t.task_id)}" aria-pressed="${t.task_id === state.taskId}"><div class="card-top">${badge('task', t.state)}<span>${esc(t.task_id.slice(0, 8))}</span></div><h3>${esc(t.request.date_from)} — ${esc(t.request.date_to)}</h3><div class="card-bottom">${t.request.source_ids.map((key) => esc(label(names.source, key))).join(' / ')}</div><div class="task-counts"><span><b>${t.pages_count}</b> 页扫描</span><span><b>${t.queue_counts.SAVED || 0}</b> 条保存</span><span><b>${t.active_failure_count || t.queue_counts.FAILED || 0}</b> 项未解决失败</span></div></button>`).join('') || '<div class="empty-state"><h2>没有匹配任务</h2><p>调整任务状态筛选后重试。</p></div>');
  updateNode($('#task-pagination'), pagination(data, 'taskPage'));
}

function renderTask(t, items, events) {
  const counts = (values) => Object.entries(names.queue).map(([key, text]) => `<span>${text}<b>${values[key] || 0}</b></span>`).join('');
  const sources = t.sources.map((s) => `<article class="source-progress"><div class="source-progress-header"><button data-source="${esc(s.source_id)}">${esc(label(names.source, s.source_id))} ${icon('external')}</button><span class="badge">${s.discovery_done ? '列表枚举结束' : '列表尚未枚举完'}</span></div><p>已扫描 ${s.pages_count} 页 · 已发现 ${s.discovered_count} 条 · ${s.discovery_done ? '无下一页' : `下一页检查点 ${s.next_page}`}<br>保存条目 ${s.queue_counts.SAVED} 条 · 本任务新增资料版本 ${s.new_versions_count} 个</p><div class="queue-counts">${counts(s.queue_counts)}</div></article>`).join('');
  const report = t.report;
  const reportHtml = report ? `<div class="limitation"><p>范围核查：${report.completion.coverage === 'COMPLETE' ? '登记栏目已核查完' : '尚未核查完，未读范围内的匹配数量未知'}；原件：${report.completion.downloads === 'COMPLETE' ? '已发现原件已采齐' : '仍有未获取内容或待解决失败'}；可读性：${report.completion.readability === 'READABLE' ? '已保存内容可读' : report.completion.readability === 'NO_CONTENT' ? '尚无可读内容' : '存在内容限制'}；复核：${report.completion.review === 'DECIDED' ? '已有复核结论，请查看各资料状态' : '仍有待复核或受限资料'}。</p><p>${esc(report.next_step)}</p>${report.missing_items.map((m) => `<p>尚缺：${esc(m.title || m.label || '待处理条目')} ${m.label ? esc(m.label) : ''} · ${esc(m.reason)} ${external(m.url, '来源地址')}</p>`).join('')}${report.failures.filter((f) => !f.resolved).map((f) => `<p>${esc(f.label)}：${esc(f.reason)}<br>${esc(f.next_step)}</p>`).join('')}${report.content_limits.map((m) => `<p>${esc(m.title || '内容限制')}：${esc(m.reason)}<br>${esc(m.next_step)}${m.evidence_id ? downloadLink(m.evidence_id, '下载原件') : ''}</p>`).join('')}</div>` : '';
  const warning = t.pause_requested || t.cancel_requested ? `<div class="limitation">${t.cancel_requested ? '已登记取消请求' : '已登记暂停请求'}；任务状态仍按数据库记录展示。</div>` : '';
  let content = '';
  if (state.taskTab === 'queue') {
    content = `<div class="queue-filters"><input id="queue-query" placeholder="搜索队列标题或地址" aria-label="搜索队列" value="${esc(state.queueQuery)}" maxlength="500"><select id="queue-state" aria-label="队列状态">${options(names.queue, '全部条目状态', state.queueState)}</select><select id="queue-source" aria-label="队列来源">${options(names.source, '全部来源', state.queueSource)}</select></div><div id="queue-items">${items ? renderQueue(items) : ''}</div>${items ? `<div class="pagination">${pagination(items, 'queuePage')}</div>` : ''}`;
  } else if (state.taskTab === 'events') {
    content = `<p class="body-note">这里展示已登记的状态事件和失败记录，不是实时进程日志。</p>${events ? renderEvents(events) + `<div class="pagination">${pagination(events, 'eventPage')}</div>` : ''}`;
  } else {
    content = `<h3 class="section-title">原始任务请求</h3><pre class="request-json">${esc(JSON.stringify(t.request, null, 2))}</pre><p class="body-note">预算是本次采集上限，不是政策全量分母。待语义复核项：${t.pending_decisions} 个。</p>`;
  }
  const tabs = [['queue', '发现队列'], ['events', '事件与失败'], ['request', '任务参数']];
  updateNode($('#task-detail'), `<div class="detail-header"><button class="mobile-back" data-back>${icon('back')}返回任务列表</button><div class="task-heading-row">${badge('task', t.state)}<span class="task-id mono">${esc(t.task_id.slice(0, 12))}</span></div><h2>采集 · ${esc(t.request.date_from)} — ${esc(t.request.date_to)}</h2><div class="doc-meta"><span>创建时间 <b>${esc(stamp(t.created_at))}</b></span><span>最近状态记录 <b>${esc(stamp(t.last_state_at))}</b></span></div><div class="task-id mono">${esc(t.task_id)}</div>${warning}${reportHtml}<p class="body-note">活动检查：${esc(t.writer_activity === 'NO_WRITER_OBSERVED' ? '未观察到写入者' : t.writer_activity === 'WRITER_LOCK_HELD_TASK_UNKNOWN' ? '资料目录有写入者，具体任务未确认' : '无法确认')}。任务状态按数据库记录展示。</p>${t.last_batch?.reasons?.length ? `<div class="limitation">最近批次停止原因：${t.last_batch.reasons.map((r) => esc(stopLabels[r] || r)).join('；')}</div>` : ''}${sources}</div><div class="detail-tabs" role="tablist" aria-label="任务内容">${tabs.map(([key, title]) => `<button role="tab" aria-selected="${state.taskTab === key}" class="${state.taskTab === key ? 'active' : ''}" data-task-tab="${key}">${title}</button>`).join('')}</div><div class="detail-body">${content}</div>`);
  applyMobile();
}

function renderQueue(data) {
  return data.items.map((d) => `<article class="queue-item"><div class="card-top"><span>${esc(label(names.source, d.source_id))} · ${esc(dateName(d.listing_date_kind))} ${esc(d.listing_date || '缺失')}</span>${badge('queue', d.state)}</div><h4>${esc(d.listing_title || '标题缺失')}</h4>${d.error ? `<div class="queue-error">${esc(d.error)}</div>` : ''}<div class="queue-links">${external(d.url, '来源页面')}${d.record_id ? `<button class="text-button" data-policy="${esc(d.record_id)}">查看已保存资料</button><span>${d.record_created_by_task ? '本任务创建该版本' : '关联既有资料，非本任务新增'}</span>` : ''}</div></article>`).join('') || '<div class="inline-empty">没有匹配的发现条目。</div>';
}

function renderEvents(data) {
  return data.items.map((e) => {
    const x = e.explanation;
    const title = x ? `${x.label}${x.resolved ? ' · 已恢复' : ' · 待处理'}` : e.event === 'task_state' ? `任务状态：${label(names.task, e.details.state)}` : e.event === 'batch_stopped' ? '本批已停止' : '任务记录';
    const text = x ? `${x.reason}\n${x.next_step}` : e.event === 'batch_stopped' ? (e.details.reasons || []).map((r) => stopLabels[r] || r).join('；') : '';
    return `<article class="event-row ${esc(e.kind)}"><time>${esc(stamp(e.created_at))}${e.source_id ? ` · ${esc(label(names.source, e.source_id))}` : ''}</time><strong>${esc(title)}</strong><p>${esc(text)}</p>${e.details.url ? external(e.details.url, '关联来源页面') : ''}<details><summary>技术详情</summary><pre>${esc(JSON.stringify(e.details, null, 2))}</pre></details></article>`;
  }).join('') || '<div class="inline-empty">尚无登记事件。</div>';
}

function applyMobile() {
  $('#policy-workspace').classList.toggle('detail-open', state.mobileDetail && state.view === 'policies');
  $('#task-workspace').classList.toggle('detail-open', state.mobileDetail && state.view === 'tasks');
  $('#policy-filters').classList.toggle('detail-hidden', state.mobileDetail && state.view === 'policies');
}

function connected(at) {
  state.lastAt = at;
  $('#connection').textContent = '资料库已连接';
  $('#connection').classList.remove('stale');
  $('#error-banner').hidden = true;
  $('#query-time').textContent = `查询于 ${stamp(at)} · 可见页面每 ${uiConfig.poll_interval_ms / 1000} 秒刷新`;
}

function failed(error) {
  $('#connection').textContent = '数据未更新';
  $('#connection').classList.add('stale');
  const message = error.name === 'AbortError' ? '查询超时，请稍后刷新。' : error.message === 'Failed to fetch' ? '无法连接工作台服务，请检查服务是否运行。' : error.message;
  $('#error-banner').textContent = `${message} ${state.lastAt ? '当前保留上次成功读取的内容。' : '尚未取得政策数据。'}可点击“刷新”重试。`;
  $('#error-banner').hidden = false;
}

async function refresh() {
  if (state.busy) {state.pending = true; return;}
  state.busy = true;
  const revision = state.revision;
  try {
    const overview = await api('/api/overview');
    const sources = state.sources.length ? null : await api('/api/sources');
    if (revision !== state.revision) return;
    if (sources) state.sources = sources.items;
    if (['scheduler','reviews'].includes(state.view)) {
      await refreshManagement(state.view);
    } else if (state.view === 'policies') {
      const params = Object.fromEntries(new FormData($('#policy-filters')));
      const data = await api('/api/policies', {...params, page: state.policyPage});
      if (revision !== state.revision) return;
      let id = state.policyId;
      if (!id && data.items.length) id = data.items[0].record_id;
      const details = id ? await Promise.all([api(`/api/policies/${encodeURIComponent(id)}`), api(`/api/policies/${encodeURIComponent(id)}/versions`)]) : null;
      if (revision !== state.revision) return;
      state.policyId = id;
      if (details) {state.policy = details[0].item; state.versions = details[1].items;}
      renderPolicies(data);
      if (state.policy) renderPolicy(state.policy, state.versions);
    } else {
      const scheduler = await api('/api/scheduler');
      if (revision !== state.revision) return;
      renderScheduler(scheduler);
      const data = await api('/api/tasks', {state: $('#task-state').value, page: state.taskPage});
      if (revision !== state.revision) return;
      let id = state.taskId;
      if (!id && data.items.length) id = data.items[0].task_id;
      let detail = null, items = null, events = null;
      if (id) {
        [detail, items, events] = await Promise.all([
          api(`/api/tasks/${encodeURIComponent(id)}`),
          state.taskTab === 'queue' ? api(`/api/tasks/${encodeURIComponent(id)}/items`, {state: state.queueState, source_id: state.queueSource, q: state.queueQuery, page: state.queuePage}) : null,
          state.taskTab === 'events' ? api(`/api/tasks/${encodeURIComponent(id)}/events`, {page: state.eventPage}) : null,
        ]);
      }
      if (revision !== state.revision) return;
      state.taskId = id;
      state.task = detail?.item;
      state.items = items;
      state.events = events;
      renderTasks(data);
      if (state.task) renderTask(state.task, items, events);
    }
    renderOverview(overview);
    connected(overview.queried_at);
  } catch (error) {if (revision === state.revision) failed(error);}
  finally {
    state.busy = false;
    if (state.pending) {state.pending = false; refresh();}
  }
}

function changed() {state.revision++; refresh();}
function route() {
  const [view, encodedId] = location.hash.slice(1).split('/');
  const nextView = ['tasks','scheduler','reviews'].includes(view) ? view : 'policies';
  let id = null;
  try {id = encodedId ? decodeURIComponent(encodedId) : null;} catch {id = null;}
  state.mobileDetail = Boolean(id);
  if (id && nextView === 'policies') {
    if (id !== state.policyId) {state.policyTab = 'body'; $('#policy-detail').scrollTop = 0;}
    state.policyId = id;
  }
  if (id && nextView === 'tasks') {
    if (id !== state.taskId) {state.taskTab = 'queue'; state.queuePage = 1; state.eventPage = 1; state.queueState = ''; state.queueSource = ''; state.queueQuery = ''; $('#task-detail').scrollTop = 0;}
    state.taskId = id;
  }
  if (nextView === 'reviews') selectReview(id);
  state.view = nextView;
  $('#scheduler-view').hidden = nextView !== 'scheduler';
  $('#reviews-view').hidden = nextView !== 'reviews';
  $('#management-operations').hidden = !['scheduler','reviews'].includes(nextView);
  $('#policies-view').hidden = nextView !== 'policies';
  $('#tasks-view').hidden = nextView !== 'tasks';
  $('#view-name').textContent = $('#page-title').textContent = ({policies:'政策库',tasks:'采集监控',scheduler:'定时任务',reviews:'资料复核'})[nextView];
  $('#eyebrow').textContent = ({policies:'POLICY LIBRARY',tasks:'COLLECTION MONITOR',scheduler:'SCHEDULE & RECOVERY',reviews:'MATERIAL REVIEW'})[nextView];
  $('#page-description').textContent = ({policies:'找到资料，读到原文，保留每一份证据。',tasks:'看清来源进度、发现队列与每一次失败。',scheduler:'调整计划，观察实际执行与补漏积压。',reviews:'逐条核对证据，保存草稿并保留每轮结论。'})[nextView];
  document.querySelectorAll('[data-view]').forEach((node) => node.classList.toggle('active', node.dataset.view === nextView));
  applyMobile();
  changed();
}

function navigate(view, id) {
  const hash = '#' + view + (id ? '/' + encodeURIComponent(id) : '');
  if (location.hash === hash) {state.mobileDetail = Boolean(id); applyMobile(); changed();}
  else location.hash = hash;
}

async function showSources(sourceId) {
  const dialog = $('#sources-dialog');
  updateNode($('#sources-content'), '<p class="body-note">正在读取来源信息…</p>');
  if (!dialog.open) dialog.showModal();
  try {
    const data = await api('/api/sources');
    state.sources = data.items;
    const sources = sourceId ? data.items.filter((s) => s.source_id === sourceId) : data.items;
    updateNode($('#sources-content'), sources.map((s) => `<article class="source-card"><h3>${esc(s.name)}</h3>${external(s.entry, s.entry)}<dl class="evidence-grid"><dt>来源标识</dt><dd>${esc(s.source_id)}</dd><dt>来源日期口径</dt><dd>${esc(dateName(s.listing_date_kind))}</dd><dt>已采集资料</dt><dd>${s.policies_count} 份</dd><dt>登记主机</dt><dd>${s.allowed_hosts.map(esc).join('<br>')}</dd><dt>采集方式</dt><dd>${s.source_id === 'mof' ? 'HTTP · 静态栏目' : '正常浏览器会话 · requests'}</dd></dl><p class="body-note">来源可用性未进行实时检测。</p></article>`).join(''));
  } catch (error) {updateNode($('#sources-content'), `<p class="limitation">${esc(error.message)}</p>`);}
}

let policyDebounce, queueDebounce;
$('#policy-filters').addEventListener('submit', (event) => {event.preventDefault(); clearTimeout(policyDebounce); state.policyPage = 1; changed();});
$('#policy-filters').addEventListener('change', () => {state.policyPage = 1; changed();});
$('#policy-search').addEventListener('input', () => {clearTimeout(policyDebounce); policyDebounce = setTimeout(() => {state.policyPage = 1; changed();}, 300);});
$('#policy-filters').addEventListener('reset', () => {setTimeout(() => {state.policyPage = 1; changed();}, 0);});
$('#task-state').addEventListener('change', () => {state.taskPage = 1; changed();});
$('#refresh').addEventListener('click', () => {changed(); refreshUpdateStatus();});
$('#sources-open').addEventListener('click', () => showSources());
$('#sources-close').addEventListener('click', () => $('#sources-dialog').close());
document.addEventListener('click', async (event) => {
  const node = event.target.closest('button, a[data-download]');
  if (!node) return;
  if (node.hasAttribute('data-download')) {
    event.preventDefault();
    try {
      const result = await fetch(node.href);
      if (!result.ok) {const data = await result.json(); throw new Error(data.error?.message || '下载失败。');}
      const blob = await result.blob();
      const url = URL.createObjectURL(blob), link = document.createElement('a');
      link.href = url;
      link.download = /filename="([^"]+)"/.exec(result.headers.get('Content-Disposition') || '')?.[1] || '原始证据.bin';
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {failed(error);}
    return;
  }
  const ds = node.dataset;
  if (ds.policy) navigate('policies', ds.policy);
  if (ds.task) navigate('tasks', ds.task);
  if (ds.source) showSources(ds.source);
  if (ds.version) {state.policyTab = 'body'; navigate('policies', ds.version);}
  if (node.hasAttribute('data-back')) {state.mobileDetail = false; applyMobile(); history.replaceState(null, '', '#' + state.view);}
  if (ds.policyTab) {state.policyTab = ds.policyTab; if (state.policy) renderPolicy(state.policy, state.versions);}
  if (ds.taskTab) {state.taskTab = ds.taskTab; changed();}
  if (ds.pageTarget) {state[ds.pageTarget] = Number(ds.pageNumber); changed();}
});
document.addEventListener('change', (event) => {
  if (event.target.id === 'queue-state') {state.queueState = event.target.value; state.queuePage = 1; changed();}
  if (event.target.id === 'queue-source') {state.queueSource = event.target.value; state.queuePage = 1; changed();}
});
document.addEventListener('input', (event) => {
  if (event.target.id === 'queue-query') {
    state.queueQuery = event.target.value; clearTimeout(queueDebounce);
    queueDebounce = setTimeout(() => {state.queuePage = 1; changed();}, 300);
  }
});
window.addEventListener('hashchange', route);
document.addEventListener('visibilitychange', () => {if (!document.hidden) {refresh(); refreshUpdateStatus();}});

document.querySelectorAll('[data-icon]').forEach((node) => {node.innerHTML = icon(node.dataset.icon);});
$('#policy-type').innerHTML = options(names.type, '全部资料类型');
$('#policy-quality').innerHTML = options(names.quality, '全部质量状态');
$('#task-state').innerHTML = options(names.task, '全部任务状态');
async function refreshUpdateStatus() {
  try {
    const data = await api('/api/update-status');
    const labels = {UP_TO_DATE: '基准提交与 main 一致', UPDATE_AVAILABLE: '有新版本', LOCAL_AHEAD: '本地领先', DIVERGED: '已分叉', UNKNOWN: '无法确认是否最新', CHECK_FAILED: '检查未完成', DISABLED: '更新检查已关闭'};
    const local = data.local || {};
    const version = `版本 ${local.version || '未知'}${local.commit ? ' · ' + local.commit.slice(0, 7) : ''}`;
    $('#update-version').textContent = `${version} · ${labels[data.state] || '检查中'}`;
    $('#update-version').title = labels[data.state] || '检查中';
    const warnings = [];
    if (data.restart_required) warnings.push('磁盘代码已变化，请重启工作台以加载新版本。');
    if (data.state === 'UPDATE_AVAILABLE') warnings.push('检测到官方 main 有新版本，建议任务结束后更新。本次可继续使用。');
    if (data.state === 'DIVERGED') warnings.push('本地与官方 main 已分叉，更新前需人工核对。');
    if (['UNKNOWN', 'CHECK_FAILED'].includes(data.state)) warnings.push(labels[data.state] + '。');
    if (local.dirty) warnings.push('本地有修改，更新前须保留。');
    const timestamp = data.checked_at ? ` 最近检查：${new Date(data.checked_at * 1000).toLocaleString('zh-CN')}。` : '';
    const stale = data.stale && data.last_success ? ' 上次成功结果已过期。' : '';
    const node = $('#update-notice');
    node.hidden = warnings.length === 0;
    updateNode(node, esc(warnings.join(' ') + timestamp + stale) + (data.state === 'UPDATE_AVAILABLE' ? ' <a href="https://github.com/anbang278/finance-tax-policy-acquisition" target="_blank" rel="noopener noreferrer">查看官方项目</a>' : ''));
  } catch { $('#update-version').textContent = '版本状态暂不可用'; }
}

async function start() {
  try {
    uiConfig = await api('/api/ui-config');
    await bootstrapManagement();
    route();
    refreshUpdateStatus();
    setInterval(() => {if (!document.hidden) refreshUpdateStatus();}, 60000);
    setInterval(() => {if (!document.hidden) refresh();}, uiConfig.poll_interval_ms);
  } catch (error) {
    failed(error);
    setTimeout(start, uiConfig.poll_interval_ms);
  }
}
start();
