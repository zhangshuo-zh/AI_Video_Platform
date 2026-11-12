/**
 * 生成中心：唯一队列 + 成本账的唯一出口。
 *
 * 三条边界写在这一页里：
 *   * 队列只读服务端，页面不自己算状态、不自己推进度；
 *   * 成本只汇总 Job 的成本列，界面不做第二本账；
 *   * 参数模板只预填下一次提交，不改已经落库的 Job 快照。
 */
import { store } from '../store.js';
import { api } from '../api.js';
import { escapeHtml, run, errorBox, emptyBox, confirmDanger, bindModalDismiss, formatMoney,
         animateRows, skeleton } from '../util.js';
import { HEALTH_LABEL, JOB_ACTIVE, JOB_FINISHED_BAD, alertLabel, jobLabel, jobTone, publicStatus }
  from '../validation.js';
import { CAPS, MODES, UNITS, unitLabel } from '../enums.js';
import { takeJobPrefill } from '../navstate.js';
import { diagnosisHtml } from './diagnosis.js';

// 作业状态的说法与配色在 js/validation.js，能力／运行位置／计费单位在 js/enums.js。
// 原来这两套字典在本文件里各有一份（其中 STATUS 与 writing.js 那份逐字相同），
// 后端加一个终态时这里会安静地把它染成"还在跑"。
// 健康状态的英文枚举不往界面上端：人要看的是"现在缺什么"，不是 missing 这个词。
// 译名从 js/validation.js 取——模型中心与这一页共用一份，
// 别让同一个 ok 在两屏一个叫"就绪"一个叫"可用"。
// 一页取多少条。100 与从前的硬上限一致，所以「第一页」的观感没变；
// 区别在于超出的部分现在**有路可走**（往下点「更早」），而不是消失。
const PAGE_SIZE = 100;

// 筛选条件和模块级状态放在外面：轮询重画、切走再回来都还在
const state = {
  projectId: '', status: '', capability: '', mode: '', q: '',
  jobs: [], costs: null, worker: null, error: null,
  // 翻页：total 是**与当前筛选同口径**的总数（响应头 X-Total-Count），
  // ended 表示已经翻到底（再问一次也是空页）。翻页状态放在 state 里而不是
  // 局部变量，因为轮询每 3 秒会重跑一次 refresh——局部变量会被打回第一页。
  total: 0, paging: false, ended: false,
  options: [], current: [], profiles: [],
  // 九个任务场景的只读骨架（`/model-policies/scenes`，登录即可）。
  // 读不到时留空数组——提交框照旧可用，只是少一格"按场景看候选"。
  scenes: [],
  optionsError: '',
};
let pollTimer = null;
let searchTimer = null;
/* 无主作业（所属项目已删除）默认折叠。
 * 为什么默认可折叠：管理员在「全部项目」视图里，这类行可能占绝大多数——实测
 * 本地库 16 条作业里 13 条是孤儿（81%），而且它们都是旧作业（入队时间早），
 * 按"最近入队"倒序排在真正的活上面，把有价值的三条挤到下面去了。
 *
 * 但它们**不能删也不能藏死**：迁移 0039 专门把 job.project_id 从
 * ON DELETE CASCADE 改成 SET NULL，就是为了"删项目别把花过的钱一起抹掉"
 * （作业是花钱的记录）。所以这里只是**收起来**，一行入口随时展开审计。 */
let showOrphanJobs = false;

const money = formatMoney;

function projectName(id) {
  const p = store.projects.find((x) => x.id === id);
  if (id === null || id === undefined) return '已删除项目';
  return p ? p.name : `项目 #${id}`;
}

function duration(ms) {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  return `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`;
}

function machineTime(ms) {
  const value = Number(ms || 0);
  return value >= 3_600_000 ? `${(value / 3_600_000).toFixed(2)} 小时` : duration(value);
}

// 日志"现在动没动"的事实：只有正文分不清慢慢写和早就卡住。
// 静默时长、字节数、更新时间都来自服务端 stat，界面不自己推算。
function logMetaHtml(job, logs) {
  const bits = [];
  const silent = logs?.silent_seconds ?? job.log_silent_seconds;
  if (silent != null) bits.push(`距上次写入 ${duration(silent * 1000)}`);
  const size = logs?.size ?? job.log_size;
  if (size) bits.push(`${(size / 1024).toFixed(1)} KB`);
  if (logs?.mtime) bits.push(`更新于 ${String(logs.mtime).slice(5, 16)}`);
  if (job.running_seconds != null) bits.push(`已运行 ${duration(job.running_seconds * 1000)}`);
  const lease = job.lease_expired ? '<span class="chip red">租约已过期</span>' : '';
  if (!bits.length && !lease) return '';
  return `<p class="modal-meta log-meta">${bits.map(escapeHtml).join(' · ')} ${lease}</p>`;
}

function timeText(value) {
  return value ? String(value).slice(5, 16) : '—';
}

function statusChip(status, label, fine = false) {
  /* §12.3：列表这一层只给对外五档（待处理／进行中／需要处理／完成／失败），
     细粒度原词留在 title 与抽屉详情里。后端状态语义一个字都没合并，
     只是不再让制作人员在六七个同义词之间自己找规律。
     `fine=true` 是详情那一处（抽屉头部与副标题），那里要的就是原词。 */
  const text = fine ? jobLabel(status, label) : publicStatus(status);
  return `<span class="chip ${jobTone(status)}" title="${escapeHtml(jobLabel(status, label))}">`
    + `${escapeHtml(text)}</span>`;
}

function rowHtml(job) {
  const cap = CAPS[job.capability] || job.capability;
  const link = [job.episode_id ? `集 ${job.episode_id}` : '',
    job.shot_id ? `镜 ${job.shot_id}` : ''].filter(Boolean).join(' · ');
  // 行内三个动作按接口要的权限点显隐（与抽屉里同一套口径）：列表只要 job:read
  // 就能出这一行，不隐的话只读的人每行都挂着三个点了必 403 的钮。
  const canCancel = store.can('job:cancel');
  const canAct = store.can('job:submit');
  // 「能不能取消/重试」由服务端下发（`JobOut.cancelable/retryable`）。
  // 老服务端没有这两个字段时才退回本地集合——本地集合只是兜底，不再是判据。
  const cancelable = typeof job.cancelable === 'boolean'
    ? job.cancelable : JOB_ACTIVE.includes(job.status);
  const retryable = typeof job.retryable === 'boolean'
    ? job.retryable : JOB_FINISHED_BAD.includes(job.status);
  const retryHint = job.retry_hint ? ` title="${escapeHtml(job.retry_hint)}"` : '';
  const actionCancel = cancelable && canCancel
    ? `<button class="btn xs" data-cancel="${job.id}">取消</button>` : '';
  const actionRetry = retryable && canAct
    ? `<button class="btn xs" data-retry="${job.id}"${retryHint}>重试</button>` : '';
  // 待核对的云端作业先核对再谈重试：那笔钱可能已经花了，默认重试是最坏的建议
  const actionReconcile = job.status === 'needs_attention' && canAct
    ? `<button class="btn xs" data-reconcile="${job.id}">核对</button>` : '';
  // 所属项目已被删除的作业：这类行只增不减，一屏能排七八条一模一样的。
  // 打一个标记交给 CSS 做视觉降级——不是藏起来（它们是孤儿数据，
  // 人正要看"哪些作业没了归属"），而是让它退到背景，别和正常作业抢注意力。
  //
  // 判据直接用 projectName() 的结论，不另外比 store.projects：
  // 界面上写「已删除项目」的那句话和这里的标记必须同源，否则会出现
  // "写着已删除项目、却没被弱化"或反过来。
  const orphan = projectName(job.project_id) === '已删除项目';
  return `
    <tr data-job="${job.id}"${orphan ? ' data-orphan="1"' : ''}>
      <td>
        <b class="job-id">#${job.id}</b>
        <span class="t-desc" title="${escapeHtml(job.prompt_preview || '')}">${escapeHtml(job.prompt_preview || '（无提示词）')}</span>
      </td>
      <td>${escapeHtml(projectName(job.project_id))}
        ${link ? `<span class="t-desc">${escapeHtml(link)}</span>` : ''}</td>
      <td><span class="chip plain">${escapeHtml(cap)}</span>
        <span class="t-desc">${escapeHtml(job.kind)}</span></td>
      <td>${escapeHtml(job.execution_mode_label || MODES[job.execution_mode] || job.execution_mode)}
        <span class="t-desc" title="${escapeHtml(job.provider_code + '/' + job.model_code)}">${escapeHtml(job.provider_code)} / ${escapeHtml(job.model_code)}</span></td>
      <td>${statusChip(job.status, job.status_label)}
        ${job.retry_of_job_id ? `<span class="t-desc">重试 #${job.retry_of_job_id}</span>` : ''}
        ${job.retry_count ? `<span class="t-desc">已被重试 ${job.retry_count} 次</span>` : ''}</td>
      <td class="r">${jobCostCell(job)}</td>
      <td class="r">${duration(job.elapsed_ms)}</td>
      <td class="t-dim">${timeText(job.queued_at)}</td>
      <td class="r job-actions">
        <button class="btn xs" data-open="${job.id}">详情</button>
        ${actionReconcile}${actionCancel}${actionRetry}
      </td>
    </tr>`;
}

function jobCostCell(job) {
  if (job.execution_mode === 'local') {
    // 与云端那一支同样的两行结构：<b>金额</b> + <span class="t-desc">地点</span>。
    // 原来这里是「¥0 · 本地」一行带分隔点，云端是「¥0.04」+「待核对」两行——
    // 同一列两种排版，扫下来像两套东西。现在两边都是「金额在上、说明在下」。
    return '<b>¥0</b><span class="t-desc">本地</span>';
  }
  if (job.cost_micros == null && job.actual_cost_micros == null) {
    return '<span class="chip amber">待补价</span>';
  }
  // 人核对过就以实际金额为准；预计与实际差额单独标出来，不让两笔混成一个数
  if (job.actual_cost_micros != null) {
    const actual = money(job.actual_cost_micros, job.currency);
    const delta = job.delta_micros;
    const deltaText = delta == null || delta === 0 ? ''
      : ` · 较预计 ${delta > 0 ? '+' : ''}${money(delta, job.currency)}`;
    return `<b>${actual}</b><span class="t-desc">已核对${escapeHtml(deltaText)}</span>`;
  }
  const amount = money(job.cost_micros, job.currency);
  if (job.billable_status === 'not_billable') {
    return `<span class="t-dim">预计 ${amount}</span>
      <span class="t-desc">不计费</span>`;
  }
  // 账单中文标签**以服务端 `billable_status_label` 为准**；下面这行兜底
  // 只在遇到不带该字段的旧响应时生效，新数据一律读后端那份，不在这里另立说法。
  const note = job.billable_status_label
    || (job.billable_status === 'known' ? '已确认' : '待核对');
  return `<b>${amount}</b><span class="t-desc">${note}</span>`;
}

function emptyHtml() {
  if (state.q || state.status || state.capability || state.mode || state.projectId) {
    return emptyBox('没有符合条件的作业',
      '<p>换一个状态或项目看看；作业一提交就会出现在这里。</p>');
  }
  return emptyBox('队列是空的', `
    <p>到分镜里挑一条镜头提交，或在这里直接提交一条作业。本地一次只跑一个，
       云端可并发提交、串行落库；配音与音乐也排这一条队。</p>
    <p><button class="btn primary" data-new-job>提交第一条作业</button></p>
    <p class="hint">提交前先跑一遍基准清单，速度数字才是真的。</p>`);
}

/**
 * 翻页条。三种状态：
 *   * 第一页且没有更多 → 整条不渲染（不摆一个点不动的按钮）；
 *   * 还有更早的     → 「更早的 N 条」按钮 + 已显示几／共几；
 *   * 已经翻到底     → 一行只读说明，明确「到底了」，别让人反复点。
 * 分母用 state.total（响应头里的同口径总数），不是数组长度——
 * 数组长度只是「这一屏装了几条」，拿它当分母就是这个缺口最初的样子。
 */
function pagerHtml() {
  const loaded = state.jobs.length;
  const total = state.total || loaded;
  if (!loaded) return '';
  // 分母是**筛完的总数**，但这个数里含无主作业（所属项目已删除），
  // 而它们在表格里默认是折叠的。所以「已显示」必须按真正列出来的行算，
  // 否则会出现「已显示 146」但屏幕上只有 133 行——那是我这版最不该犯的错，
  // 与原来那格停在 100 的假数字是同一类。
  const visible = showOrphanJobs || state.projectId === '__orphan__'
    ? loaded
    : state.jobs.filter((j) => j.project_id != null).length;
  const more = total > loaded;
  const left = Math.max(0, total - loaded);
  const hiddenOrphans = loaded - visible;
  const count = `<span class="t-desc">已显示 ${visible} / 共 ${total} 条`
    + (hiddenOrphans > 0 ? `（另有 ${hiddenOrphans} 条无主作业折叠中）` : '')
    + '</span>';
  if (!more && !state.ended && loaded <= PAGE_SIZE) return '';
  if (state.paging) {
    return `<div class="gen-more-row"><span class="gen-more-spin">正在取更早的</span>${count}</div>`;
  }
  return `<div class="gen-more-row">${more
    ? `<button class="btn" id="gen-more-btn" data-more>更早的 ${Math.min(left, PAGE_SIZE)} 条</button>`
    : '<span class="t-desc">已经到底了</span>'}${count}</div>`;
}

function statsHtml() {
  const q = state.worker?.queue || { queued: 0, running: 0 };
  const costs = state.costs;
  const pending = (costs?.currencies || []).reduce((n, c) => n + c.pending, 0);
  const unpriced = costs?.unpriced_jobs || 0;
  // 「当前筛选」只是**这一页拿到的行数**，队列请求带着 limit 100（`generate.js:562`）。
  // 作业超过 100 条时，那个数字会停在 100 而看起来像"总共就这些"——
  // 正是在最需要知道"积压了多少"的时候骗人。补上真数就能分开"就这些"和"还有"。
  //
  // 但 `total_jobs` **只按项目过滤**（`jobs_parts/cost.py` 的 `cost_summary`
  // 走的是 `_scope(project_ids)`），它不知道 `q`／`status`／`capability`／`mode`。
  // 所以一旦有额外筛选，两者口径就不是一回事（"共 N" 会变成项目总数，
  // 而显示的是搜索结果），那种情况下宁可不显示，也不能给假分母。
  const shown = state.jobs.length;
  const total = Number(costs?.total_jobs) || 0;
  const narrowed = Boolean(state.q || state.status || state.capability || state.mode);
  const totalNote = (!narrowed && total > shown)
    ? ` <i class="t-desc">／共 ${total}</i>` : '';
  return `
    <span class="gen-stat"><b>${q.queued}</b> 等待</span>
    <span class="gen-stat"><b>${q.running}</b> 运行</span>
    <span class="gen-stat"><b>${state.jobs.length}</b> 当前筛选${totalNote}</span>
    <span class="gen-stat"><b>${pending}</b> 笔待核对</span>
    <span class="gen-stat"><b>${machineTime(costs?.local?.elapsed_ms)}</b> 本地机时</span>
    ${unpriced ? `<span class="chip amber">${unpriced} 笔云端作业没记价</span>` : ''}`;
}

// 队列告警条。这些是"现在可能卡住了"的事实，不是新状态——
// 处置仍然是人取消或 worker 自己的 deadline 生效，界面不替它落 failed。
function alertsHtml() {
  const alerts = state.worker?.alerts || [];
  if (!alerts.length) return '';
  return `<div class="gen-alerts">${alerts.map((a) => `
    <div class="gen-alert ${a.level === 'bad' ? 'bad' : 'warn'}">
      <span class="chip ${a.level === 'bad' ? 'red' : 'amber'}">${escapeHtml(alertLabel(a.code))}</span>
      <span>${escapeHtml(a.text)}</span>
    </div>`).join('')}</div>`;
}

function costHtml() {
  const costs = state.costs;
  if (!costs) return skeleton(2, 'card');
  const local = costs.local || { jobs: 0 };
  if (!costs.currencies.length && !local.jobs) {
    return `<div class="empty">
      <div class="empty-t">还没有花费</div>
      <p>第一条作业跑完，这里就会按币种和能力自动汇总。项目中心只显示累计数，明细只在这一页。</p>
    </div>`;
  }
  const tiles = costs.currencies.map((c) => `
    <div class="cost-tile">
      <div class="cost-top"><span>${escapeHtml(c.currency)}</span>
        <b>${money(c.total_micros, c.currency)}</b></div>
      <div class="cost-meta">${c.jobs} 笔 · 其中待核对 ${c.pending} 笔</div>
      <div class="cost-caps">
        ${c.by_capability.map((x) => `<span class="cost-cap">
          <span>${escapeHtml(x.capability_label)}</span>
          <b>${money(x.total_micros, c.currency)}</b>
          <i>${x.jobs} 笔</i></span>`).join('')}
      </div>
    </div>`).join('');
  const localTile = local.jobs ? `
    <div class="cost-tile local">
      <div class="cost-top"><span>本地</span><b>${machineTime(local.elapsed_ms)}</b></div>
      <div class="cost-meta">${local.jobs} 笔 · 不按量计费</div>
      <div class="cost-caps">
        <span class="cost-cap"><span>CPU</span><b>${duration(local.cpu_ms)}</b></span>
        <span class="cost-cap"><span>峰值内存</span><b>${Math.round(Number(local.max_rss_kb || 0) / 1024)} MB</b></span>
      </div>
      <p class="cost-note">数字来自 worker 的 getrusage；峰值内存是进程生命周期峰值，不是单条作业独占。</p>
    </div>` : '';
  return `<div class="cost-grid">${tiles}${localTile}</div>
    <p class="note cost-caliber"><b>口径</b>：Job 记单价 × 量，本表只汇总、不能手改。
      多币种分桶，不跨币种相加；失败不把成本清零，待核对就是待核对。
      本地作业记 0 元，但保留机时——它是判断"产能溢出该不该转云端"的依据。</p>`;
}

function filtersHtml() {
  // 「无主（项目已删除）」是前端专用哨兵值：后端不认识它，请求时不传 project_id，
  // 拿到全量后在前端筛出 project_id 为空的那批。为一个纯展示分组去改接口不划算，
  // 而且那条语义本来就只有管理员拿得到（access.ensure 对 NULL 只放行管理员）。
  const orphanOpt = `<option value="__orphan__"` + (state.projectId === '__orphan__' ? ' selected' : '') + '>无主（项目已删除）</option>';
  const projectOpts = [`<option value="">全部项目</option>`, orphanOpt]
    .concat(store.projects.map((p) => `<option value="${p.id}" ${String(p.id) === state.projectId ? 'selected' : ''}>${escapeHtml(p.name)}${p.status === 'archived' ? '（已归档）' : ''}</option>`));
  const statusOpts = [
    ['', '全部状态'], ['queued,starting,running,cancel_requested', '进行中'],
    ['succeeded', '成功'], ['failed', '失败'], ['cancelled', '已取消'],
    ['needs_attention', '待核对'],
  ].map(([v, l]) => `<option value="${v}" ${v === state.status ? 'selected' : ''}>${l}</option>`);
  const capOpts = [['', '全部能力']].concat(Object.entries(CAPS))
    .map(([v, l]) => `<option value="${v}" ${v === state.capability ? 'selected' : ''}>${l}</option>`);
  const modeOpts = [['', '全部执行方式'], ['local', '本地'], ['cloud', '云端']]
    .map(([v, l]) => `<option value="${v}" ${v === state.mode ? 'selected' : ''}>${l}</option>`);
  return `
    <select id="gen-project" class="ctl">${projectOpts.join('')}</select>
    <select id="gen-status" class="ctl">${statusOpts.join('')}</select>
    <select id="gen-capability" class="ctl">${capOpts.join('')}</select>
    <select id="gen-mode" class="ctl">${modeOpts.join('')}</select>
    <input id="gen-q" class="ctl" placeholder="搜作业号或提示词" value="${escapeHtml(state.q)}">
    <button class="btn xs" id="gen-refresh">刷新</button>`;
}

export default {
  async render() {
    const canSubmit = store.can('job:submit');
    return `
    <section class="view on gen-view">
      <div class="page-head">
        <h1>生成中心</h1>
        <p>唯一队列 · 本地优先 · 云端逐条确认成本</p>
        <div class="right">
          <span class="gen-worker" id="gen-worker"><span class="dot idle"></span> worker 未接入</span>
          <button class="btn" id="gen-profiles">参数模板</button>
          ${canSubmit ? '<button class="btn primary" id="gen-new">提交作业</button>' : ''}
        </div>
      </div>

      <div class="asset-stats gen-stats" id="gen-stats"></div>

      <div id="gen-alerts"></div>

      <div class="card">
        <div class="gen-filters">${filtersHtml()}</div>
        <div id="gen-error"></div>
        <div class="table-wrap">
          <table class="gen-table">
            <thead><tr>
              <th>作业</th><th>项目</th><th>能力</th><th>执行</th>
              <th>状态</th><th class="r">成本</th><th class="r">耗时</th>
              <th>入队</th><th class="r">操作</th>
            </tr></thead>
            <tbody id="gen-rows"></tbody>
          </table>
        </div>
        <div id="gen-empty"></div>
        <!-- 翻页条：只在「还有更早的」或「已经翻过页」时出现，
             第一页且没有更多时整条不渲染——不给一个永远点不动的按钮占位置。 -->
        <div id="gen-more" class="gen-more"></div>
        <!-- 无主作业（所属项目已删除）的折叠入口：默认收着，一行按钮展开审计 -->
        <div id="gen-orphan-bar" class="gen-orphan-bar"></div>
      </div>

      <div class="card mt-card" >
        <div class="card-head"><h2>成本账</h2>
          <span class="sub">由 Job 汇总，不可手改</span></div>
        <div id="gen-cost">${skeleton(2, 'card')}</div>
      </div>

      <div class="drawer" id="gen-drawer" hidden tabindex="-1">
        <div class="drawer-panel" role="dialog" aria-modal="true" aria-labelledby="gen-drawer-title">
          <div class="drawer-head">
            <div><h2 id="gen-drawer-title">作业</h2><div id="gen-drawer-sub"></div></div>
            <button class="btn ghost" id="gen-drawer-close" aria-label="关闭">关闭</button>
          </div>
          <div class="drawer-body" id="gen-drawer-body"></div>
        </div>
      </div>

      <div class="modal" id="job-modal" hidden>
        <div class="modal-box wide">
          <div class="modal-head neutral">提交作业</div>
          <div class="modal-body">
            <div class="form-grid gen-form">
              <label for="jm-project">项目</label>
              <select id="jm-project" class="ctl"></select>
              <label for="jm-scene">任务场景</label>
              <select id="jm-scene" class="ctl"></select>
              <div class="gen-scene-panel" id="jm-scene-panel" hidden></div>
              <label for="jm-capability">能力</label>
              <select id="jm-capability" class="ctl"></select>
              <label for="jm-mode">执行路线</label>
              <select id="jm-mode" class="ctl"></select>
              <label for="jm-route">槽位</label>
              <select id="jm-route" class="ctl"></select>
              <!-- 预填留痕：路线与槽位刚被"按场景预填"改过时，这一行横跨两列
                   说清是谁改的、从哪条改成哪条、怎么撤销。没有这一行，
                   预填就是"控件自己动了"，人只会以为下拉坏了。 -->
              <div class="gen-scene-prefill" id="jm-scene-prefill" hidden></div>
              <label for="jm-template">参数模板</label>
              <select id="jm-template" class="ctl"><option value="">不使用模板</option></select>
              <span class="pf-label top">参数</span>
              <div id="jm-params" class="gen-params"></div>
              <label class="top" for="jm-prompt">提示词</label>
              <textarea id="jm-prompt" rows="4" maxlength="8000"
                        placeholder="这次要生成什么；参数在上一栏，不要写成命令行"></textarea>
              <span class="pf-label top" id="jm-cost-label" hidden>预计成本</span>
              <div id="jm-cost" class="gen-cost" hidden></div>
            </div>
            <p class="modal-meta" id="jm-link"></p>
            <p class="modal-meta" id="jm-hint"></p>
            <p class="modal-err" id="jm-error"></p>
          </div>
          <div class="modal-actions">
            <button class="btn" id="jm-cancel">取消</button>
            <button class="btn primary" id="jm-submit">提交</button>
          </div>
        </div>
      </div>

      <div class="modal" id="profile-modal" hidden>
        <div class="modal-box wide">
          <div class="modal-head neutral">参数模板</div>
          <div class="modal-body">
            <p class="modal-meta" style="margin-top:0">模板只预填下一次提交，不改已经落库的 Job 参数快照。</p>
            <div id="pf-list" class="pf-list"></div>
            <div class="pf-edit">
              <h3 id="pf-form-title">新建模板</h3>
              <div class="form-grid">
                <label for="pf-name">模板名</label>
                <input id="pf-name" maxlength="60" placeholder="例如 竖屏 704×1280 · 4 步">
                <label for="pf-capability">能力</label>
                <select id="pf-capability" class="ctl"></select>
                <label for="pf-scope">范围</label>
                <select id="pf-scope" class="ctl"></select>
                <label for="pf-route">绑定槽位</label>
                <select id="pf-route" class="ctl"></select>
                <label class="top" for="pf-params">参数</label>
                <textarea id="pf-params" rows="3" spellcheck="false">{}</textarea>
                <span class="pf-label">默认</span>
                <label class="pf-check"><input type="checkbox" id="pf-default"> 同类能力默认用这一套</label>
              </div>
              <p class="modal-err" id="pf-error"></p>
            </div>
          </div>
          <div class="modal-actions">
            <button class="btn" id="pf-reset" hidden>取消编辑</button>
            <button class="btn" id="pf-close">关闭</button>
            <button class="btn primary" id="pf-save">保存模板</button>
          </div>
        </div>
      </div>

      <div class="modal" id="reconcile-modal" hidden>
        <div class="modal-box">
          <div class="modal-head neutral" id="rc-title">核对云端作业</div>
          <div class="modal-body">
            <p class="modal-meta" id="rc-meta" style="margin-top:0"></p>
            <div class="form-grid">
              <label for="rc-decision">核对结果</label>
              <select id="rc-decision" class="ctl">
                <option value="pending">继续待核对（只留备注）</option>
                <option value="succeeded">确认可用</option>
                <option value="failed">确认不可用</option>
              </select>
              <label for="rc-amount">实际金额（元）</label>
              <input id="rc-amount" class="ctl" type="number" min="0" step="0.000001"
                     placeholder="确认结果时必填；不确定就选继续待核对">
              <label for="rc-note">备注</label>
              <input id="rc-note" class="ctl" maxlength="500"
                     placeholder="供应商后台核对到的用量、请求 ID、为什么是这个金额">
            </div>
            <p class="modal-meta">核对只改账单与状态，<b>不重新提交</b>、不改提示词与参数快照。
              确认「可用／不可用」必须填实际金额——不给金额就等于替人断定这次没花钱。
              重投是另一个决定，请用重试。</p>
            <p class="modal-err" id="rc-error"></p>
          </div>
          <div class="modal-actions">
            <button class="btn" id="rc-cancel">取消</button>
            <button class="btn primary" id="rc-submit">保存核对</button>
          </div>
        </div>
      </div>
    </section>`;
  },

  mount(root) {
    const rows = root.querySelector('#gen-rows');
    const moreHost = root.querySelector('#gen-more');
    const empty = root.querySelector('#gen-empty');
    const orphanBar = root.querySelector('#gen-orphan-bar');
    const stats = root.querySelector('#gen-stats');
    const alerts = root.querySelector('#gen-alerts');
    const cost = root.querySelector('#gen-cost');
    const errorHost = root.querySelector('#gen-error');
    const drawer = root.querySelector('#gen-drawer');
    const drawerBody = root.querySelector('#gen-drawer-body');
    const jobModal = root.querySelector('#job-modal');
    const profileModal = root.querySelector('#profile-modal');
    const reconcileModal = root.querySelector('#reconcile-modal');
    let openJobId = null;
    let editingProfile = null;
    let drawerBusy = false;
    // 提交中标志：挡住"从点击到按钮变灰"之间的第二下点击（云端重复计费）。
    // 与 `drawerBusy` 同层，都是这个视图的互斥位。
    let submitBusy = false;
    // 上一次挂到 document 上的 visibilitychange 闭包（用来成对解绑，见下面轮询段）
    let visHandler = null;
    let profileProjectId = null;
    let refreshing = false;
    let pendingRefresh = false;   // "在飞期间又有人要过一次数据"，一轮结束补发
    // 分镜带过来的关联；只对这一次提交生效，关掉弹窗就清
    let linked = null;

    if (pollTimer) clearInterval(pollTimer);
    if (searchTimer) clearTimeout(searchTimer);

    /** 这一轮挂载还挂在页面上吗？
     *
     * `root` 是 `#view-root`，换页时它**仍然在 document.body 里**，只是里面的内容
     * 被整体换掉了——所以原来那句 `document.body.contains(root)` 永远为真，
     * 轮询根本停不下来。人从生成中心切到别的页之后，每 3 秒就会往一个已经不存在的
     * `#gen-worker` 上写 innerHTML，控制台每轮一条未捕获 TypeError。
     * 判活要看自己抓到的那个节点，`isConnected` 才说真话。
     */
    const alive = () => stats.isConnected;

    function paintWorker() {
      const el = root.querySelector('#gen-worker');
      if (!el) return;
      const online = !!state.worker?.online;
      el.innerHTML = `<span class="dot ${online ? 'live' : 'idle'}"></span>
        ${online ? 'worker 在线' : 'worker 未接入'}`;
    }

    function paintError() {
      if (!state.error) { errorHost.innerHTML = ''; return; }
      errorHost.innerHTML = `<div class="gen-inline-err">${escapeHtml(state.error)}
        <button class="btn xs" data-retry-load>重试</button></div>`;
    }

    function paint() {
      if (!alive()) return;
      paintWorker();
      stats.innerHTML = statsHtml();
      alerts.innerHTML = alertsHtml();
      paintError();
      // 按「有没有所属项目」拆两批：
      //   有项目的 —— 日常要看的那批，默认全显示；
      //   无主的   —— 所属项目已删除（迁移 0039 刻意保留的记账记录），
      //               默认收在列表下方一行入口里，点开才列。
      // 只有管理员在不筛项目时才拿得到无主作业（后端 access.ensure 对
      // project_id IS NULL 只放行管理员），所以这里不用再判权限。
      const live = state.jobs.filter((j) => j.project_id != null);
      const orphans = state.jobs.filter((j) => j.project_id == null);
      // 选了「无主（项目已删除）」就只看这一批：这时的列表本来就是审计视图，
      // 不再折叠，也不用再给一行「点开看」。
      const auditOnly = state.projectId === '__orphan__';
      if (auditOnly) showOrphanJobs = true;
      if (orphanBar) {
        orphanBar.innerHTML = (orphans.length && !auditOnly) ? `
          <button class="btn xs" id="gen-orphan-toggle">
            ${showOrphanJobs ? '收起无主作业'
              : `另有 ${orphans.length} 条无主作业 · 所属项目已删除（点开看）`}</button>
          <span class="t-desc">作业与成本账不会随项目删除而消失（迁移 0039 改 SET NULL 就是为了保住这笔账）。
            它们不进任何项目的成本汇总，也不占日常列表——项目已经不在了，硬算进谁的账都是假账。</span>` : '';
      }
      const shown = showOrphanJobs ? state.jobs : live;
      if (state.error) {
        rows.innerHTML = '';
        empty.innerHTML = '';
      } else if (!shown.length) {
        rows.innerHTML = '';
        // 只有孤儿作业、又收着的时候，别说「队列是空的」——那是在骗人
        empty.innerHTML = (orphans.length && !showOrphanJobs)
          ? emptyBox('这个范围内只有无主作业',
              `<p>${orphans.length} 条所属项目已删除的作业收在下面，点开看。</p>`)
          : emptyHtml();
      } else {
        empty.innerHTML = '';
        // 走 animateRows：轮询每 3 秒重画一次，新作业进来、跑完的换状态时，
        // 新行会淡入一次（2px 位移）。没有这层的话整块重绘看着像"屏幕闪了一下
        // 但说不出哪变了"。整批同时播，不逐条错峰。
        animateRows(rows, () => { rows.innerHTML = shown.map(rowHtml).join(''); }, 'data-job');
      }
      // 翻页条跟着列表一起重画：轮询改了 total（有人新提交作业）也要跟着变。
      if (moreHost) moreHost.innerHTML = state.error ? '' : pagerHtml();
      cost.innerHTML = costHtml();
    }

    /** 筛选变了就回到第一页：留着上一次翻出来的行，会让人以为新筛选有那些数据。
     *  轮询**不能**走这一条（它每 3 秒跑一次，走一次就把翻页打回原点）。
     */
    function resetPaging() {
      state.ended = false;
      state.paging = false;
      state.total = 0;
      // 行也要清：只清标志的话，下一次 refresh 会把「第一页 + 旧筛选翻出来的更早行」
      // 拼在一起，屏幕上出现一批根本不符合新筛选条件的作业。
      state.jobs = [];
    }

    async function refresh({ quiet = false } = {}) {
      // 轮询 3 秒一次，慢请求会叠在一起；后回来的旧结果会把新结果盖掉，
      // 所以同一时刻只允许一轮在飞。但**丢请求比叠请求更糟**：在飞的那一轮里
      // 人改了筛选或按了「重试」，原来这里直接 return，那一次意图就永远不会再发——
      // 列表停在旧筛选上，最长要等下一次轮询（3 秒）才像"自己好了"。
      // 改成补一轮：只记"还欠一次"，不排队滚出一串请求。
      if (refreshing) { pendingRefresh = true; return; }
      refreshing = true;
      try {
        const [page, costs, worker] = await Promise.all([
          api.jobs({
            // __orphan__ 是前端哨兵值，别传给后端（它不认识，会当成项目 id）
            project_id: state.projectId === '__orphan__' ? '' : state.projectId,
            status: state.status,
            capability: state.capability, execution_mode: state.mode,
            q: state.q, limit: PAGE_SIZE,
          }),
          // 成本汇总同样不能吃哨兵值：__orphan__ 传给后端会 404，
          // 而 Promise.all 里一条失败整批失败——那样队列也会跟着空白。
          // 无主作业不进任何项目的账（迁移 0039 的口径），所以这里传 undefined。
          api.jobCosts(state.projectId === '__orphan__' ? undefined : (state.projectId || undefined)),
          api.workerStatus(),
        ]);
        // 轮询只重取**第一页**。已经翻出来的更早那些页要留着——否则人翻到第 3 页，
        // 3 秒后轮询一跑就把他弹回第一页，等于翻页根本不能用。
        // 第一页的行按 id 覆盖刷新（状态会从 queued 变 running 再变成功），
        // 更早那些行原样留着；两段都按 id 倒序，拼接顺序天然正确。
        const first = page.items;
        // 只保留「比第一页更早」的行：第一页本身按 id 覆盖刷新。
        // 这里同时起到筛选切换后的清场作用——旧筛选的行不会被拼进来，
        // 因为 resetPaging 会把它们从 state.jobs 里剔掉（见 resetPaging 调用点）。
        const older = state.jobs.filter((j) => !first.some((f) => f.id === j.id));
        state.jobs = first.concat(older);
        state.total = page.total || state.jobs.length;
        // 第一页没取满，说明本来就没有更多了
        if (first.length < PAGE_SIZE) state.ended = true;
        state.costs = costs;
        state.worker = worker;
        state.error = null;
        // 请求回来的时候人可能已经切走了：这一轮不画，也不去开抽屉
        if (!alive()) return;
        paint();
        if (openJobId && !drawer.hidden && jobModal.hidden && profileModal.hidden
            && reconcileModal.hidden) {
          // 抽屉开着时跟着队列一起刷新：作业从排队→失败/成功，正文里的
          // 错误、日志、产物必须自己长出来，不能等人关掉再开一次。
          loadDrawer(openJobId, true);
        }
      } catch (err) {
        // 读失败画在页内，不弹 alert：轮询每 3 秒一次，弹窗会把人挡在页面外，
        // 而且 alert 会阻塞主线程，连"重试"按钮都点不到。
        state.error = err.message || '未知错误';
        paint();
      } finally {
        refreshing = false;
        if (pendingRefresh) {
          pendingRefresh = false;
          if (alive()) refresh();     // 切走了就别再补这一轮
        }
      }
    }

    /* ---------- 抽屉 ---------- */
    function kv(label, value) {
      return `<div class="kv"><dt>${escapeHtml(label)}</dt><dd>${value}</dd></div>`;
    }

    function drawerLoading(id) {
      if (!alive()) return;
      root.querySelector('#gen-drawer-title').textContent = `作业 #${id}`;
      root.querySelector('#gen-drawer-sub').innerHTML = '';
      drawerBody.innerHTML = skeleton(3, 'text');
    }

    function providerHtml(job, res) {
      const p = res.provider || null;
      if (!p && !job.provider_request_id) return '';
      const usage = p?.usage || {};
      const parts = [];
      if (usage.prompt_tokens != null || usage.completion_tokens != null) {
        parts.push(`输入 ${usage.prompt_tokens ?? '—'} / 输出 ${usage.completion_tokens ?? '—'} / 合计 ${usage.total_tokens ?? '—'} token`);
      }
      if (usage.cached_tokens != null) parts.push(`缓存命中 ${usage.cached_tokens} token`);
      if (usage.duration != null) {
        parts.push(`${usage.duration} 秒${usage.resolution ? ` · ${usage.resolution}` : ''}`);
      }
      const err = p?.error
        ? `<span class="chip red">${escapeHtml(p.error.code || 'error')}</span> ${escapeHtml(p.error.message || '')}`
        : '';
      return `<h3 class="drawer-h">供应商回执</h3>
        <div class="kv-grid">
          ${kv('请求 / 任务 ID', escapeHtml(job.provider_request_id || p?.request_id || '—'))}
          ${kv('HTTP 状态', p?.http_status ?? '—')}
          ${kv('任务状态', escapeHtml(p?.task_status || '—'))}
          ${kv('实际用量', parts.length ? escapeHtml(parts.join('；')) : '—')}
          ${kv('供应商延迟', p?.latency_ms == null ? '—' : duration(p.latency_ms))}
        </div>
        ${err ? `<p class="modal-meta">${err}</p>` : ''}`;
    }


    // 异常诊断那一块抽到 `./diagnosis.js` 与产线节点共用：
    // 七字段目录的意义就是「界面各处说同一句话」，画两遍迟早对不上。

    function metricsHtml(res) {
      const m = res.adapter_metrics || {};
      const rows = [];
      const labels = {
        prompt_encode_s: '提示词编码', mlx_dit_load_s: '模型加载',
        mlx_denoise_s: '去噪', vae_decode_export_s: '解码出片',
        total_s: '模型总耗时', denoise_peak_gb: '去噪峰值显存',
        load_peak_gb: '加载峰值显存',
      };
      Object.entries(labels).forEach(([key, label]) => {
        const value = m[key];
        if (value == null) return;
        rows.push(kv(label, key.endsWith('_gb') ? `${value} GB` : `${Number(value).toFixed(2)} s`));
      });
      if (!rows.length) return '';
      return `<h3 class="drawer-h">模型内部耗时</h3><div class="kv-grid">${rows.join('')}</div>`;
    }

    function commandHtml(res) {
      const argv = Array.isArray(res.command) ? res.command : null;
      if (!argv) return '';
      const text = argv.map((part) => /\s/.test(part) ? JSON.stringify(part) : part).join(' ');
      return `<details class="cmd-details">
        <summary>实际命令（argv 数组，未经过 shell）</summary>
        <pre class="code-block">${escapeHtml(text)}</pre>
      </details>`;
    }

    function drawerHtml(job, logs, arts, diag) {
      const res = job.resources || {};
      const price = job.unit_price_micros == null ? '—'
        : `${money(job.unit_price_micros, job.currency)} / ${unitLabel(job.unit)}`;
      const cloudCost = job.execution_mode === 'cloud' ? `
        ${kv('预计记账', job.cost_micros == null ? '待补价' : money(job.cost_micros, job.currency))}
        ${kv('账单状态', `<span class="chip ${job.billable_status === 'pending_reconciliation'
          ? 'amber' : job.billable_status === 'known' ? 'green' : 'gray'}">${escapeHtml(job.billable_status_label)}</span>`)}
        ${job.actual_cost_micros != null ? kv('实际金额（已核对）',
          `<b>${money(job.actual_cost_micros, job.currency)}</b>`
          + (job.delta_micros ? ` <span class="t-desc">较预计 ${job.delta_micros > 0 ? '+' : ''}${money(job.delta_micros, job.currency)}</span>` : '')) : ''}
        ${job.reconciled_at ? kv('核对人 / 时间',
          `${escapeHtml(job.reconciled_by_name || '—')} · ${escapeHtml(String(job.reconciled_at).slice(0, 16))}`) : ''}
        ${job.reconcile_note ? kv('核对备注', escapeHtml(job.reconcile_note)) : ''}
        ${kv('数量', `${job.quantity ?? '—'} ${unitLabel(job.unit)}`)}
        ${kv('提交时单价', price)}
        ${kv('币种', escapeHtml(job.currency || '—'))}
        ${kv('定价来源', escapeHtml(job.pricing_snapshot?.source || '—'))}
        ${kv('确认时间', escapeHtml(job.pricing_snapshot?.confirmed_at || '—'))}` : `
        ${kv('计费', '本地作业不计费（记 0 元）')}
        ${kv('机时', duration(res.wall_ms ?? job.elapsed_ms))}
        ${kv('worker CPU', res.cpu_ms == null ? '—' : duration(res.cpu_ms))}
        ${kv('模型子进程 CPU', res.child_cpu_ms == null ? '—' : duration(res.child_cpu_ms))}
        ${kv('worker 峰值内存', res.max_rss_kb == null ? '—' : `${Math.round(res.max_rss_kb / 1024)} MB`)}
        ${kv('子进程峰值内存', res.child_max_rss_kb == null ? '—' : `${Math.round(res.child_max_rss_kb / 1024)} MB`)}`;

      const err = job.error_code ? `<div class="drawer-err">
        <b>${escapeHtml(job.error_label || job.error_code)}</b>
        <span class="t-dim">${escapeHtml(job.error_code)}</span>
        <p>${escapeHtml(job.error_message || '没有更多说明')}</p></div>` : '';
      const attention = job.attention_reason ? `<div class="drawer-err warn">
        <b>待核对原因</b><p>${escapeHtml(job.attention_reason)}</p></div>` : '';
      const canMarkAttention = job.execution_mode === 'cloud'
        && ['succeeded', 'failed'].includes(job.status);
      // 四个动作按"这条接口到底要哪个权限点"显隐。打开抽屉只要 job:read，
      // 而取消要 job:cancel、重试／核对／标记要 job:submit（各自的服务端声明就是
      // 这一行的依据）。不看权限点渲染，等于给只读的人摆四个点了必吃 403 的钮，
      // 也等于让人误以为"我能取消别人的作业"。
      const canCancel = store.can('job:cancel');
      const canAct = store.can('job:submit');
      // 与列表同一口径：服务端说能不能，前端只说「你有没有这个权限」。
      const cancelable = typeof job.cancelable === 'boolean'
        ? job.cancelable : JOB_ACTIVE.includes(job.status);
      const retryable = typeof job.retryable === 'boolean'
        ? job.retryable : JOB_FINISHED_BAD.includes(job.status);
      const retryTitle = job.retry_hint ? ` title="${escapeHtml(job.retry_hint)}"` : '';
      const actions = [
        cancelable && canCancel
          ? `<button class="btn" data-cancel="${job.id}">取消作业</button>` : '',
        job.status === 'needs_attention' && canAct
          ? `<button class="btn primary" data-reconcile="${job.id}">核对账单</button>` : '',
        retryable && canAct
          ? `<button class="btn" data-retry="${job.id}"${retryTitle}>重试（新建一行）</button>` : '',
        canMarkAttention && canAct
          ? `<button class="btn" data-attention="${job.id}">标记待核对</button>` : '',
      ].filter(Boolean).join('');
      // 动作区空着有两种原因，不能拿同一句话糊过去：一种是"这条已经跑完了"，
      // 另一种是"你能看，但这个档位不让你动"。后者不写出来，人只会对着空白反复点。
      const blocked = !actions && (
        (cancelable && !canCancel)
        || ((retryable || job.status === 'needs_attention') && !canAct));
      const actionsHint = blocked
        ? '这里没有你能按的钮：取消要 job:cancel，重试／核对要 job:submit。'
        : '已结束，可关闭。';

      const artifacts = (arts || []).length ? arts.map((a, i) => {
        const name = a.path.split('/').pop() || a.path;
        const url = api.jobArtifactUrl(job.id, i);
        const ext = name.split('.').pop().toLowerCase();
        let preview = '';
        if (a.exists && /^(mp4|webm|mov|m4v)$/.test(ext)) {
          preview = `<video class="artifact-video" controls preload="metadata" src="${url}"></video>`;
        } else if (a.exists && /^(png|jpg|jpeg|webp|gif)$/.test(ext)) {
          preview = `<img class="artifact-image" src="${url}" alt="${escapeHtml(name)}" loading="lazy">`;
        } else if (a.exists && /^(mp3|wav|m4a|flac|ogg)$/.test(ext)) {
          preview = `<audio class="artifact-audio" controls preload="metadata" src="${url}"></audio>`;
        }
        return `<div class="artifact-item">
          <a class="artifact" href="${url}" target="_blank" rel="noopener">
            <span>${escapeHtml(name)}</span>
            <i>${a.exists ? `${(a.size / 1024).toFixed(1)} KB` : '文件已不在'}</i>
          </a>
          ${preview}
        </div>`;
      }).join('') : '<p class="hint">还没有产物。</p>';

      return `
        <div class="drawer-actions">${actions || `<span class="hint">${actionsHint}</span>`}</div>
        ${err}
        ${attention}
        ${diagnosisHtml(diag)}
        <div class="kv-grid">
          ${kv('状态', statusChip(job.status, job.status_label, true))}
          ${kv('项目', escapeHtml(projectName(job.project_id)))}
          ${kv('关联', [job.episode_id ? `集 ${job.episode_id}` : '',
            job.shot_id ? `镜 ${job.shot_id}` : ''].filter(Boolean).join(' · ') || '—')}
          ${kv('能力', escapeHtml(job.capability_label))}
          ${kv('执行', `${escapeHtml(job.execution_mode_label)} · ${escapeHtml(job.provider_code)} / ${escapeHtml(job.model_code)}`)}
          ${kv('入队', timeText(job.queued_at))}
          ${kv('开始', timeText(job.started_at))}
          ${kv('结束', timeText(job.finished_at))}
          ${kv('耗时 / 返回码', `${duration(job.elapsed_ms)} · ${job.return_code ?? '—'}`)}
          ${kv('第几次尝试', `第 ${job.attempt || 1} 次`)}
          ${kv('重试自', job.retry_of_job_id ? `#${job.retry_of_job_id}` : '—')}
          ${kv('已被重试', job.retry_count ? `${job.retry_count} 次${job.superseded_by_job_id ? `（最新 #${job.superseded_by_job_id}）` : ''}` : '—')}
          ${kv('adapter 版本', escapeHtml(job.adapter_version || '—'))}
        </div>
        <h3 class="drawer-h">成本</h3>
        <div class="kv-grid">${cloudCost}</div>
        ${providerHtml(job, res)}
        <h3 class="drawer-h">提示词</h3>
        <pre class="code-block">${escapeHtml(job.prompt_snapshot || '（无）')}</pre>
        <h3 class="drawer-h">参数快照</h3>
        <pre class="code-block">${escapeHtml(JSON.stringify(job.params || {}, null, 2))}</pre>
        ${metricsHtml(res)}
        ${commandHtml(res)}
        <h3 class="drawer-h">日志</h3>
        ${logMetaHtml(job, logs)}
        <pre class="code-block log-block">${escapeHtml(logs?.text || '还没有日志。')}</pre>
        <h3 class="drawer-h">产物</h3>
        <div class="artifact-list">${artifacts}</div>`;
    }

    async function loadDrawer(id, keepScroll = false) {
      if (drawerBusy) return;
      drawerBusy = true;
      const scroll = drawerBody.scrollTop;
      try {
        const [job, logs, arts, diag] = await Promise.all([
          api.job(id), api.jobLogs(id).catch(() => ({ text: '' })),
          api.jobArtifacts(id).catch(() => []),
          // 读不到诊断不影响看详情：这一栏是辅助信息，不能反过来把主信息挡掉
          api.jobDiagnosis(id).catch(() => null),
        ]);
        if (openJobId !== id) return;
        if (!alive()) return;
        root.querySelector('#gen-drawer-title').textContent = `作业 #${job.id}`;
        root.querySelector('#gen-drawer-sub').innerHTML =
          `${statusChip(job.status, job.status_label, true)}
           <span class="t-dim">${escapeHtml(job.capability_label)} · ${escapeHtml(job.provider_code)}/${escapeHtml(job.model_code)}</span>`;
        // **重画前记住哪些 `<details>` 是展开的，画完恢复**（2026-11-10 审计）。
        //
        // 抽屉每 3 秒跟着队列刷新一次（`paint()` 之后那句 `loadDrawer`），
        // 每次都是整块 `innerHTML = drawerHtml(...)`。而抽屉里的
        // 「实际命令（argv 数组…）」（`commandHtml` 的 `<details class="cmd-details">`）
        // 是**展开态住在 DOM 属性里**的——整块换掉，人就看着自己刚点开的那一节
        // 每 3 秒自动缩回去。作业跑得越久（本地视频要几分钟）越像界面抽风。
        //
        // 只恢复展开态、不做签名比对：抽屉里没有输入框（只有 `<pre>` 与
        // `<details>`），所以"重画会不会冲掉用户正在输入的东西"这个更麻烦的
        // 问题在这里不存在；而签名比对要保证不漏字段（`elapsed_ms`、
        // `running_seconds` 这类每拍都变的），漏一个就变成"该更新的时候不更新"，
        // 那是比多做一次重画更坏的 bug。用**下标**做键：详情面板每拍结构一致。
        const openDetails = [...drawerBody.querySelectorAll('details')]
          .map((d, i) => (d.open ? i : -1)).filter((i) => i >= 0);
        drawerBody.innerHTML = drawerHtml(job, logs, arts, diag);
        if (openDetails.length) {
          const again = drawerBody.querySelectorAll('details');
          for (const i of openDetails) {
            if (again[i]) again[i].open = true;
          }
        }
        if (keepScroll) drawerBody.scrollTop = scroll;
      } catch (err) {
        if (openJobId === id) drawerBody.innerHTML = errorBox('作业详情', err, false);
      } finally {
        drawerBusy = false;
      }
    }

    async function openDrawer(id) {
      openJobId = id;
      drawer.hidden = false;
      drawer.focus();
      drawerLoading(id);
      await loadDrawer(id);
    }

    function closeDrawer() {
      drawer.hidden = true;
      openJobId = null;
    }

    /* ---------- 取消失败重试 ---------- */
    async function askCancel(id) {
      const ok = await confirmDanger({
        title: '取消这条作业？',
        body: '排队中的现场取消；正在跑的会请求取消，worker 下一次轮询时停下。'
          + '云端作业如果已经发出请求，供应商仍可能计费——成本会留在账上，不会因为取消归零。',
        okText: '取消作业', danger: true,
      });
      if (!ok) return;
      if (await run('取消作业', () => api.cancelJob(id))) {
        await refresh();
        if (openJobId === id) openDrawer(id);
      }
    }

    async function askRetry(id) {
      // 先取预览：云端重试会多一笔预计计费，不弹这一屏，重试就只是"再点一次"
      let preview;
      try { preview = await api.retryPreview(id); } catch (err) {
        // 读不到预览就走页内错误条，不弹 alert：统一错误出口只在 util.js 一处
        if (err.status !== 401) { state.error = `重试预览读不到：${err.message}`; paint(); }
        return;
      }
      const chainRows = (preview.chain || []).map((c) =>
        `<tr><td>#${c.id}</td><td>${escapeHtml(c.status_label)}</td>
         <td>第 ${c.attempt} 次</td>
         <td>${c.cost_micros ? money(c.cost_micros, preview.currency) : '—'}</td></tr>`).join('');
      const body = `
        <p>${escapeHtml(preview.warning || '')}</p>
        <p class="t-dim">${escapeHtml(preview.provider_code)} / ${escapeHtml(preview.model_code)}
          · ${escapeHtml(preview.prompt_preview || '（无提示词）')}</p>
        ${chainRows ? `<div class="table-wrap"><table class="retry-chain"><thead><tr>
          <th>作业</th><th>状态</th><th>尝试</th><th>记账</th></tr></thead>
          <tbody>${chainRows}</tbody></table></div>
          <p class="t-dim">这一串合计 ${money(preview.chain_total_micros, preview.currency)}，
          其中 ${preview.chain_pending} 笔待核对。</p>` : ''}
        <p>重试会<b>新建一行</b>并继承参数快照，原行的日志、耗时、错误一个字段都不动。</p>`;
      const ok = await confirmDanger({
        title: preview.may_charge_again ? '重试会再走一次云端调用' : '重试这条作业？',
        body, okText: preview.may_charge_again ? '仍要重试（可能再计费）' : '重试',
        danger: !!preview.may_charge_again,
      });
      if (!ok) return;
      if (!(await run('重试', () => api.retryJob(id)))) return;
      await refresh();
      const latest = state.jobs.find((j) => j.retry_of_job_id === id);
      if (latest) openDrawer(latest.id);
    }

    /* ---------- 云端账单核对 ---------- */
    let reconcileId = null;

    function openReconcile(job) {
      reconcileId = job.id;
      reconcileModal.hidden = false;
      root.querySelector('#rc-title').textContent = `核对云端作业 #${job.id}`;
      root.querySelector('#rc-meta').innerHTML =
        `${escapeHtml(job.provider_code)} / ${escapeHtml(job.model_code)}
         · 预计 ${job.cost_micros == null ? '待补价' : money(job.cost_micros, job.currency)}
         · 请求 ID ${escapeHtml(job.provider_request_id || '—')}`;
      root.querySelector('#rc-decision').value = 'pending';
      // 实际金额默认填预计值方便核对，但人必须自己确认或改；不替人断定
      root.querySelector('#rc-amount').value =
        job.cost_micros == null ? '' : String(job.cost_micros / 1_000_000);
      root.querySelector('#rc-note').value = '';
      root.querySelector('#rc-error').textContent = '';
      root.querySelector('#rc-decision').focus();
    }

    function closeReconcile() { reconcileModal.hidden = true; reconcileId = null; }

    async function submitReconcile() {
      const errEl = root.querySelector('#rc-error');
      errEl.textContent = '';
      if (reconcileId == null) return;
      const decision = root.querySelector('#rc-decision').value;
      const rawAmount = root.querySelector('#rc-amount').value.trim();
      const body = { decision, note: root.querySelector('#rc-note').value.trim() };
      if (decision !== 'pending') {
        if (rawAmount === '' || !(Number(rawAmount) >= 0)) {
          errEl.textContent = '确认结果前必须填实际金额；不确定就选「继续待核对」。';
          return;
        }
        body.actual_cost_micros = Math.round(Number(rawAmount) * 1_000_000);
      }
      const btn = root.querySelector('#rc-submit');
      btn.disabled = true;
      try {
        await api.reconcileJob(reconcileId, body);
        const id = reconcileId;
        closeReconcile();
        await refresh();
        if (openJobId === id) openDrawer(id);
      } catch (err) {
        errEl.textContent = err.message || '核对失败';
      } finally {
        btn.disabled = false;
      }
    }

    async function askAttention(id) {
      // 用一个轻量输入收原因：原生 prompt 与两套主题脱节，这里复用确认弹窗，
      // 原因走一个内联输入框（confirmDanger 的 body 支持 HTML）。
      const ok = await confirmDanger({
        title: '把这条云端作业标记为待核对？',
        body: `<p>供应商说成功、产物却打不开，或用量与预计差很多时用。
          标记后账单挂起，<b>核对前不要重试</b>，可能重复计费。</p>
          <p><input id="att-reason" class="ctl" maxlength="500" style="width:100%"
            placeholder="为什么待核对，例如 产物打不开 / 用量与预计不符"></p>`,
        okText: '标记待核对', danger: false,
      });
      if (!ok) return;
      const reason = (document.querySelector('#att-reason')?.value || '').trim();
      // 原因为空时抛一句中文给 run()：统一错误出口只在 util.js，视图里不直接 alert
      if (await run('标记待核对', () => {
        if (!reason) throw new Error('请填一句待核对的原因。');
        return api.markAttention(id, reason);
      })) {
        await refresh();
        if (openJobId === id) openDrawer(id);
      }
    }

    /* ---------- 提交表单 ---------- */
    function optionsFor(cap, backend) {
      return state.options.filter((o) => o.capability === cap
        && (!backend || o.backend === backend));
    }

    function currentOf(cap) {
      return state.current.find((c) => c.capability === cap) || null;
    }

    function modeChoices(cap) {
      const out = [{ value: 'default', label: '默认轨（当前配置）' }];
      for (const mode of ['local', 'cloud']) {
        if (optionsFor(cap, mode).length) out.push({ value: mode, label: MODES[mode] });
      }
      return out;
    }

    function refreshModeSelect() {
      const cap = root.querySelector('#jm-capability').value;
      const sel = root.querySelector('#jm-mode');
      const keep = sel.value;
      const choices = modeChoices(cap);
      sel.innerHTML = choices.map((c) => `<option value="${c.value}">${c.label}</option>`).join('');
      sel.value = choices.some((c) => c.value === keep) ? keep : choices[0].value;
      refreshRouteSelect();
    }

    function refreshRouteSelect() {
      const cap = root.querySelector('#jm-capability').value;
      const mode = root.querySelector('#jm-mode').value;
      const route = root.querySelector('#jm-route');
      if (mode === 'default') {
        const cur = currentOf(cap);
        if (!cur) {
          route.innerHTML = '<option value="">没有配置默认轨</option>';
        } else {
          route.innerHTML = `<option value="">${escapeHtml(cur.backend_label)} · ${escapeHtml(cur.provider || '未填')} / ${escapeHtml(cur.model || '未填')}</option>`;
        }
      } else {
        const opts = optionsFor(cap, mode);
        route.innerHTML = opts.length
          ? opts.map((o, i) => `<option value="${i}">${escapeHtml(o.provider)} / ${escapeHtml(o.model)}（${HEALTH_LABEL[o.health_state] || o.health_state}）</option>`).join('')
          : '<option value="">这一轨没有已启用的槽位</option>';
      }
      renderParams();
      renderCostSection();
      refreshTemplateSelect();
    }

    function selectedOption() {
      const cap = root.querySelector('#jm-capability').value;
      const mode = root.querySelector('#jm-mode').value;
      if (mode === 'default') {
        const cur = currentOf(cap);
        if (!cur) return null;
        return optionsFor(cap, cur.backend).find((o) => o.provider === cur.provider
          && o.model === cur.model) || null;
      }
      const opts = optionsFor(cap, mode);
      const idx = Number(root.querySelector('#jm-route').value);
      return Number.isInteger(idx) ? opts[idx] || null : null;
    }

    function isCloud() {
      const mode = root.querySelector('#jm-mode').value;
      if (mode === 'cloud') return true;
      if (mode === 'local') return false;
      return currentOf(root.querySelector('#jm-capability').value)?.backend === 'cloud';
    }

    function paramFields(spec, values = {}, option = null) {
      const params = spec?.params || [];
      if (!params.length) {
        return `<p class="hint">${option
          ? '该 provider 的参数尚未核验，不接受额外参数。'
          : '还没有选中可用的槽位，参数暂不可填。'}</p>`;
      }
      return `<div class="param-grid">${params.map((p) => {
        const value = Object.prototype.hasOwnProperty.call(values, p.name)
          ? values[p.name] : (p.default ?? '');
        if (p.type === 'bool') {
          return `<label class="param"><span>${escapeHtml(p.name)}</span>
            <input type="checkbox" data-param="${escapeHtml(p.name)}" data-type="bool"
                   ${value ? 'checked' : ''}></label>`;
        }
        if (p.choices?.length) {
          // 枚举参数不给自由文本框：清单里写死的那几个值就是供应商接受的集合
          return `<label class="param"><span>${escapeHtml(p.name)}</span>
            <select class="ctl" data-param="${escapeHtml(p.name)}" data-type="${escapeHtml(p.type)}">
              ${p.choices.map((c) => `<option value="${escapeHtml(c)}"
                ${String(c) === String(value) ? 'selected' : ''}>${escapeHtml(c)}</option>`).join('')}
            </select>
            ${p.note ? `<i>${escapeHtml(p.note)}</i>` : ''}</label>`;
        }
        return `<label class="param"><span>${escapeHtml(p.name)}</span>
          <input class="ctl" data-param="${escapeHtml(p.name)}"
                 data-type="${escapeHtml(p.type)}"
                 ${p.type === 'image' ? 'placeholder="输入引用路径"' : ''}
                 value="${escapeHtml(String(value ?? ''))}">
          ${p.note ? `<i>${escapeHtml(p.note)}</i>` : ''}</label>`;
      }).join('')}</div>`;
    }

    function renderParams(values) {
      // 默认轨与显式轨都按"当前选中的 provider/model"去找模型档
      const option = selectedOption();
      const modelCode = option?.model;
      const model = option?.models?.find((m) => m.code === modelCode)
        || option?.models?.[0] || null;
      root.querySelector('#jm-params').innerHTML = paramFields(model, values || {}, option);
    }

    /** 清单里登记的官方价；当分辨率/时长等条件命中时优先用那一条。 */
    function priceFor(model, params) {
      const list = model?.prices || [];
      return list.find((p) => Object.entries(p.when || {})
        .every(([key, value]) => String(params[key] ?? '') === String(value)))
        || list[0] || null;
    }

    function renderCostSection() {
      const cloud = isCloud();
      const costEl = root.querySelector('#jm-cost');
      const label = root.querySelector('#jm-cost-label');
      costEl.hidden = !cloud;
      label.hidden = !cloud;
      if (!cloud) return;
      const cap = root.querySelector('#jm-capability').value;
      const defaultUnit = cap === 'video' || cap === 'music' ? 'second'
        : cap === 'image' ? 'request' : cap === 'tts' ? 'character'
          : cap === 'text' ? 'million_token' : 'token';
      costEl.innerHTML = `
        <div class="cost-inputs">
          <label><span>数量</span><input id="jm-qty" class="ctl" type="number" min="0" step="0.001" value="1"></label>
          <label><span>单位</span><select id="jm-unit" class="ctl">${Object.entries(UNITS)
            .map(([v, l]) => `<option value="${v}" ${v === defaultUnit ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
          <label><span>单价（元）</span><input id="jm-price" class="ctl" type="number" min="0" step="0.000001" placeholder="0.000000"></label>
          <label><span>币种</span><select id="jm-currency" class="ctl">
            <option value="CNY">CNY</option><option value="USD">USD</option></select></label>
          <label class="wide"><span>价格来源</span>
            <input id="jm-source" class="ctl" maxlength="200" value="手工估算（提交人填写）"></label>
        </div>
        <div class="cost-preview"><span>预计</span><b id="jm-estimate">—</b></div>
        <p class="hint cost-source" id="jm-price-note"></p>
        <label class="pf-check cost-confirm"><input type="checkbox" id="jm-confirm">
          我确认这次按供应商计费，预计金额以上方为准</label>
        <p class="hint">金额由服务端按 数量 × 单价 计算；失败不默认归零。重试会新建一行、继承这份计价，原行不动。</p>`;
      const update = () => {
        const q = Number(root.querySelector('#jm-qty')?.value || 0);
        const p = Number(root.querySelector('#jm-price')?.value || 0);
        const cur = root.querySelector('#jm-currency')?.value || 'CNY';
        root.querySelector('#jm-estimate').textContent =
          q > 0 && p > 0 ? money(Math.round(q * p * 1_000_000), cur) : '—';
      };
      applyPricing(update);
      ['#jm-qty', '#jm-price', '#jm-currency'].forEach((sel) => {
        root.querySelector(sel)?.addEventListener('input', update);
        root.querySelector(sel)?.addEventListener('change', update);
      });
    }

    /** 按清单预填计价，不替人勾确认；找不到官方价时说得清楚。 */
    function applyPricing(update) {
      const option = selectedOption();
      const model = option?.models?.find((m) => m.code === option?.model)
        || option?.models?.[0] || null;
      const params = readParams();
      const price = priceFor(model, params);
      const noteEl = root.querySelector('#jm-price-note');
      if (!price) {
        if (noteEl) {
          noteEl.textContent = option
            ? '这个档位没有登记官方价格；数量与单价请手工填写。'
            : '还没有选中可用槽位，无法预填价格。';
        }
        update?.();
        return;
      }
      const qty = price.unit === 'second' && params.duration
        ? Number(params.duration) : Number(price.default_quantity || 1);
      root.querySelector('#jm-qty').value = String(qty);
      root.querySelector('#jm-unit').value = price.unit;
      root.querySelector('#jm-price').value = String(price.amount_micros / 1_000_000);
      root.querySelector('#jm-currency').value = price.currency || 'CNY';
      root.querySelector('#jm-source').value = price.source.slice(0, 200);
      if (noteEl) {
        noteEl.textContent = `官方价档：${price.label}｜${price.source}${price.note ? '。' + price.note : ''}`;
      }
      update?.();
    }

    function refreshTemplateSelect() {
      const cap = root.querySelector('#jm-capability').value;
      const sel = root.querySelector('#jm-template');
      const keep = sel.value;
      const list = state.profiles.filter((p) => p.capability === cap);
      sel.innerHTML = '<option value="">不使用模板</option>' + list.map((p) =>
        `<option value="${p.id}">${escapeHtml(p.name)}${p.project_id === null ? '（全局）' : ''}</option>`).join('');
      if (list.some((p) => String(p.id) === keep)) sel.value = keep;
      else sel.value = '';
    }

    function applyProfile(p) {
      if (!p) return;
      const capSel = root.querySelector('#jm-capability');
      capSel.value = p.capability;
      refreshModeSelect();
      if (p.provider_code && p.model_code) {
        const option = state.options.find((o) => o.capability === p.capability
          && o.provider === p.provider_code && o.model === p.model_code);
        if (option) {
          root.querySelector('#jm-mode').value = option.backend;
          refreshRouteSelect();
          const opts = optionsFor(p.capability, option.backend);
          const idx = opts.findIndex((o) => o.provider === p.provider_code
            && o.model === p.model_code);
          if (idx >= 0) root.querySelector('#jm-route').value = String(idx);
        }
      }
      renderParams(p.params || {});
    }

    async function openJobModal(prefill) {
      jobModal.hidden = false;
      root.querySelector('#jm-error').textContent = '';
      linked = (prefill?.episodeId || prefill?.shotId)
        ? { episodeId: prefill.episodeId || null, shotId: prefill.shotId || null } : null;
      const linkEl = root.querySelector('#jm-link');
      linkEl.textContent = linked
        ? `关联：${linked.episodeId ? `集 ${linked.episodeId}` : ''}${linked.episodeId && linked.shotId ? ' · ' : ''}${linked.shotId ? `镜 ${linked.shotId}` : ''}（提交后不改镜头内容）`
        : '';
      const projectSel = root.querySelector('#jm-project');
      const preferred = prefill?.projectId || state.projectId || store.currentId || '';
      projectSel.innerHTML = store.projects.map((p) =>
        `<option value="${p.id}" ${String(p.id) === String(preferred) ? 'selected' : ''}>${escapeHtml(p.name)}${p.status === 'archived' ? '（已归档）' : ''}</option>`).join('');
      if (!store.projects.length) {
        projectSel.innerHTML = '<option value="">还没有项目</option>';
      }
      root.querySelector('#jm-capability').innerHTML = Object.entries(CAPS)
        .map(([v, l]) => `<option value="${v}" ${v === (prefill?.capability || 'video') ? 'selected' : ''}>${l}</option>`).join('');
      root.querySelector('#jm-prompt').value = '';
      root.querySelector('#jm-hint').textContent = '';
      refreshModeSelect();
      // 顺序有讲究：先让调用方的模板落进两个下拉，再挂场景面板。
      // 场景预填与模板预填撞车时**模板赢**（调用方给得更具体），
      // 所以带模板时直接关掉自动预填；不带模板时 allowAuto 生效，
      // 撤销的 before 也会落在模板之后的那一格上。
      if (prefill?.template) applyProfile(prefill.template);
      if (prefill?.prompt) root.querySelector('#jm-prompt').value = prefill.prompt;
      // 场景由调用方带过来（分镜点「生成」＝分镜生成），没有就停在"不按场景"。
      refreshSceneSelect(prefill?.sceneKey || '', !prefill?.template);
      try {
        state.profiles = await api.generationProfiles(projectSel.value || undefined);
        refreshTemplateSelect();
      } catch { /* 模板读不到不影响提交 */ }
      root.querySelector('#jm-project').focus();
    }

    /* ---------- 任务场景：候选、挡路原因，以及"按场景预填" ---------- */
    /**
     * 为什么要有这一块：`/model-policies/resolve` 早就回答得了"这个场景谁可能行、
     * 别的为什么不行"，但它只长在模型中心·调用策略那一页。于是人在提交框前面
     * 根本不知道自己要的那一档在不在候选里——只能凭记忆翻另一页，
     * 或者提交之后吃一句"连接失败"再回头查。
     *
     * 第二步（2026-11-07）加了**预填**：选场景后把第一档候选摆进「执行路线／槽位」。
     * 但它与裁定 MC1 的关系必须说准——MC1 禁的是**平台自己换路**
     * （`resolve_provider()` 不看 `model_policy`、备用轨不自动顶上），
     * 不是禁"人点了一下、界面把人选的东西填好"。这里的预填全部发生在
     * **人显式选了场景**（或从分镜／镜头工作台带着场景进来）之后：
     *   ① 填的是**界面上的两个下拉**，不是 `provider_config` 的默认轨——
     *      关掉弹窗、换个人提交，走的路一点没变；
     *   ② 每一次预填都**留痕**（谁改的、从哪条改成哪条）并给一键撤销；
     *   ③ **绝不勾「确认按供应商计费」**：云端候选可以预填路线，
     *      但计费确认仍必须人自己勾（`user_confirmed_cloud` 那道闸门原样不动）；
     *   ④ 服务端 `resolve_provider()` 一个字节没改。
     * 所以准确说法是：**人点的场景，平台替人把格子填好，并且把"我填过"写在脸上。**
     */
    let sceneSeq = 0;
    /** 最近一次预填的撤销信息；人手动改过任一相关下拉就作废（他接管了）。 */
    let scenePrefill = null;
    /** 当前面板上这批候选（「用它」按钮按行号回查它）。 */
    let sceneCandidates = [];

    /** 场景下拉按当前能力过滤：场景有能力的归属，选错能力再选场景只会得到 0 候选。 */
    function refreshSceneSelect(keepKey, allowAuto = true) {
      const cap = root.querySelector('#jm-capability').value;
      const sel = root.querySelector('#jm-scene');
      if (!sel) return;
      const pool = state.scenes.filter((s) => s.capability === cap);
      const keep = keepKey !== undefined ? keepKey : sel.value;
      // "不按场景"永远是第一项：不用场景的人不该被逼着选一个。
      sel.innerHTML = '<option value="">（不按场景）</option>' + pool.map((s) =>
        `<option value="${escapeHtml(s.scene_key)}">${escapeHtml(s.scene_label)}`
        + `${s.required_producible ? '' : '（清单缺能力）'}</option>`).join('');
      sel.value = pool.some((s) => s.scene_key === keep) ? keep : '';
      renderScenePanel(allowAuto);
    }

    /** 撤销/接管时收起预填留痕那一行。 */
    function clearScenePrefillTrace() {
      scenePrefill = null;
      const el = root.querySelector('#jm-scene-prefill');
      if (el) { el.hidden = true; el.innerHTML = ''; }
    }

    /**
     * 把一条候选摆进「执行路线／槽位」两个下拉，并留撤销信息。
     * 只动界面上的两个下拉——`provider_config` 的默认轨、服务端
     * `resolve_provider()` 都不经过这里。云端候选**不碰计费勾选**：
     * `#jm-confirm` 保持人自己勾，提交路径上 `user_confirmed_cloud`
     * 那道闸门原样不动。
     * 返回 false＝这一档在提交槽位池里找不到（未启用或没填 provider），
     * 调用方要把话说清而不是假装成功。
     */
    function scenePrefillTo(cand, sceneLabel) {
      const cap = root.querySelector('#jm-capability').value;
      const modeSel = root.querySelector('#jm-mode');
      const routeSel = root.querySelector('#jm-route');
      // 已有留痕时保留**最初**的 before：连续换场景预填两次，撤销要回到
      // 人打开弹窗时的那一格，而不是上一次的预填结果。
      const before = scenePrefill ? scenePrefill.before
        : { mode: modeSel.value, route: routeSel.value };
      if (!modeChoices(cap).some((c) => c.value === cand.backend)) return false;
      modeSel.value = cand.backend;
      refreshRouteSelect();
      const opts = optionsFor(cap, cand.backend);
      const idx = opts.findIndex((o) => o.provider === cand.provider
        && o.model === cand.model_code);
      if (idx < 0) {
        modeSel.value = before.mode;
        refreshRouteSelect();
        return false;
      }
      routeSel.value = String(idx);
      renderParams();
      renderCostSection();
      scenePrefill = {
        before, cap, sceneLabel, cloud: cand.backend === 'cloud',
        candLabel: `${cand.provider} / ${cand.model_code}`,
      };
      renderScenePrefillTrace();
      return true;
    }

    function renderScenePrefillTrace() {
      const el = root.querySelector('#jm-scene-prefill');
      if (!el || !scenePrefill) return;
      const modeLabel = (root.querySelector('#jm-mode').selectedOptions[0] || {}).textContent || '';
      const routeLabel = (root.querySelector('#jm-route').selectedOptions[0] || {}).textContent || '';
      el.hidden = false;
      el.innerHTML = `
        <p class="hint">已按场景「${escapeHtml(scenePrefill.sceneLabel)}」预填：
          <b>${escapeHtml(modeLabel.trim())}</b> ·
          <b class="mono">${escapeHtml(routeLabel.trim())}</b>。
          <button type="button" class="btn ghost sm" id="jm-scene-undo">撤销预填</button></p>
        <p class="hint">这只改了这个弹窗里的两个下拉——能力轨的默认配置一个字没动，
          关掉弹窗或换人提交都不受影响。${scenePrefill.cloud
            ? '<b class="t-bad">这一档是云端：预填不会替你勾「确认按供应商计费」，提交前仍要你自己勾。</b>'
            : ''}</p>`;
    }

    function undoScenePrefill() {
      if (!scenePrefill) return;
      const modeSel = root.querySelector('#jm-mode');
      const routeSel = root.querySelector('#jm-route');
      modeSel.value = scenePrefill.before.mode;
      // 这一句不能省：撤销把路线改回上一档时，槽位里的**选项**也得跟着重挂，
      // 否则 mode 回到默认轨、route 还停在预填那一档（断言由
      // `tools/generate_scene_check.py` 的"一键撤销"那条钉住，
      // 去掉这一句它当场红）。
      refreshRouteSelect();
      if ([...routeSel.options].some((o) => o.value === scenePrefill.before.route)) {
        routeSel.value = scenePrefill.before.route;
        renderParams();
        renderCostSection();
      }
      clearScenePrefillTrace();
    }

    /** 读一次这个场景的候选，画出来。`allowAuto`＝允许单候选自动预填。 */
    async function renderScenePanel(allowAuto = true) {
      const panel = root.querySelector('#jm-scene-panel');
      if (!panel) return;
      const key = root.querySelector('#jm-scene')?.value || '';
      if (!key) { panel.hidden = true; panel.innerHTML = ''; sceneCandidates = []; clearScenePrefillTrace(); return; }
      const scene = state.scenes.find((s) => s.scene_key === key);
      panel.hidden = false;
      // 请求序号：连着换几个场景时，后到的那条未必是后点的。
      const wanted = ++sceneSeq;
      panel.innerHTML = '<p class="hint">正在按能力匹配……</p>';
      let out;
      try {
        out = await api.resolveScene(key);
      } catch (err) {
        if (wanted !== sceneSeq) return;
        // 读不到不拦人：这一段只是提示，提交路径上还有同一套服务端判据兜底。
        panel.innerHTML = `<p class="hint">候选读不到（${escapeHtml(err.message || '未知错误')}）；
          仍可直接在下两格显式选执行路线与槽位。</p>`;
        return;
      }
      if (wanted !== sceneSeq) return;
      sceneCandidates = out.candidates || [];
      const cand = sceneCandidates.map((c, i) => `<tr>
        <td><b>${escapeHtml(c.label)}</b>
          <div class="hint mono">${escapeHtml(c.provider)}/${escapeHtml(c.model_code)}</div></td>
        <td><span class="chip ${c.status === 'available' ? 'green' : 'amber'}">${
          escapeHtml(c.status_label || c.status)}</span></td>
        <td>${c.needs_cloud_confirm
          ? '<span class="chip amber">要逐次确认计费</span>'
          : '<span class="hint">本地</span>'}</td>
        <td><button type="button" class="btn ghost sm" data-scene-use="${i}">用它</button></td>
      </tr>`).join('');
      const blockers = (out.blockers || [])
        .map((b) => `<li>${escapeHtml(b)}</li>`).join('');
      const impossible = (out.impossible_tags || []);
      panel.innerHTML = `
        <p class="hint gen-scene-lede">
          场景「${escapeHtml(scene?.scene_label || key)}」要的能力：
          ${(out.required || []).map(escapeHtml).join('、') || '（没写）'}。
          只有一档候选时会自动预填到「执行路线／槽位」并留痕可撤销；
          好几档时点那一行的「用它」——平台不替你挑。
        </p>
        ${impossible.length
          ? `<p class="t-bad gen-scene-warn">这几项必需能力在清单里没有任何来源：
             ${impossible.map(escapeHtml).join('、')}。要让它有解得先在 <code>adapters.py</code>
             补这一档规格，或者在「调用策略」里把这一项从「必需」降下来——点这里没用。</p>`
          : ''}
        <h3 class="drawer-h">满足必需能力的候选（${sceneCandidates.length}）</h3>
        ${sceneCandidates.length
          ? `<div class="table-wrap"><table class="gen-scene-table">
               <thead><tr><th>模型</th><th>状态</th><th>计费</th><th></th></tr></thead>
               <tbody>${cand}</tbody></table></div>`
          : '<p class="hint">一条都没有。</p>'}
        ${blockers
          ? `<details class="gen-scene-more"><summary>为什么别的不行（${
              (out.blockers || []).length}）</summary><ul class="hint">${blockers}</ul></details>`
          : ''}`;
      // 自动预填只在"恰好一档"时发生：好几档时替你挑第一档就是平台在选路，
      // 那不是预填，是越权。template 预填优先（调用方给得更具体）。
      if (allowAuto && sceneCandidates.length === 1) {
        scenePrefillTo(sceneCandidates[0], scene?.scene_label || key);
      }
    }


    function closeJobModal() {
      jobModal.hidden = true;
      linked = null;
      root.querySelector('#jm-link').textContent = '';
    }

    function readParams() {
      const out = {};
      root.querySelectorAll('#jm-params [data-param]').forEach((el) => {
        const name = el.dataset.param;
        const type = el.dataset.type;
        if (type === 'bool') { out[name] = el.checked; return; }
        const raw = el.value.trim();
        if (raw === '' && type !== 'str' && type !== 'image') return;
        if (type === 'int') out[name] = parseInt(raw, 10);
        else if (type === 'float') out[name] = parseFloat(raw);
        else out[name] = raw;
      });
      return out;
    }

    async function submitJob() {
      // **入口就置 busy，不是等预检回来才 disabled**（2026-11-10 审计修的真 bug）。
      //
      // 原来 `btn.disabled = true` 在下面 `await api.precheckShot(...)` **之后**，
      // 也就是从点击到按钮变灰之间隔着一整趟网络往返。窗口期内连点两下，
      // 两趟都跑到底：两次 `api.submitJob`，云端就是**两条计费作业**——
      // 而这里恰恰是"云端要勾选确认按供应商计费"的那条路径，重复提交＝重复花钱。
      // 本地轨重复提交只是多一条没用的作业，云端轨是钱。
      //
      // `submitBusy` 是模块变量而不是按钮属性：`paint()` 每 3 秒重画队列，
      // 按钮本身不会重建（它在弹窗里，弹窗不在重画范围内），但用变量更稳——
      // 也顺手挡住"按钮还没禁用但事件已经排在队列里"的第二下。
      // 形状照 `writing.js` 的 `applyAi`（入口 `if (ai.busy) return; ai.busy = true;`）。
      if (submitBusy) return;
      submitBusy = true;
      const submitBtn = root.querySelector('#jm-submit');
      if (submitBtn) submitBtn.disabled = true;
      try {
        const errEl = root.querySelector('#jm-error');
        errEl.textContent = '';
        const projectId = Number(root.querySelector('#jm-project').value);
        const capability = root.querySelector('#jm-capability').value;
        const mode = root.querySelector('#jm-mode').value;
        const prompt = root.querySelector('#jm-prompt').value.trim();
        if (!projectId) { errEl.textContent = '先选一个项目。'; return; }
        if (!prompt) { errEl.textContent = '提示词不能为空。'; return; }

        const body = {
          project_id: projectId, capability, prompt,
          params: readParams(), input_refs: [],
        };
        if (linked?.episodeId) body.episode_id = linked.episodeId;
        if (linked?.shotId) body.shot_id = linked.shotId;
        if (mode !== 'default') {
          const option = selectedOption();
          if (!option) { errEl.textContent = '这一轨没有可用槽位。'; return; }
          body.execution_mode = mode;
          body.provider_code = option.provider;
          body.model_code = option.model;
          // 健康不是门禁：本地权重没装也能提交，worker 会以 adapter_not_ready 失败，
          // 这条失败比"界面上不让点"更容易查——但要把话说在前面。
          if (option.health_state !== 'ok') {
            root.querySelector('#jm-hint').textContent =
              `提醒：这个槽位当前是「${HEALTH_LABEL[option.health_state] || option.health_state}」——${option.health_detail}`;
          }
        }

        if (isCloud()) {
          const rawPrice = root.querySelector('#jm-price').value.trim();
          const qty = Number(root.querySelector('#jm-qty').value);
          const price = Number(rawPrice);
          const source = root.querySelector('#jm-source').value.trim();
          if (!(qty > 0) || rawPrice === '' || !(price >= 0) || !source) {
            errEl.textContent = '云端作业要填数量、单价和价格来源。';
            return;
          }
          if (!root.querySelector('#jm-confirm').checked) {
            errEl.textContent = '请先勾选确认按供应商计费。';
            return;
          }
          body.estimate = {
            quantity: qty, unit: root.querySelector('#jm-unit').value,
            unit_price_micros: Math.round(price * 1_000_000),
            currency: root.querySelector('#jm-currency').value,
            source,
          };
          body.user_confirmed_cloud = true;
        }

        // 0037：提交前跑一次统一预检（有镜头才跑——纯文本/资产生成没有"这一镜"可查）。
        // 规则只有一份：`precheck.py` 用的就是 `resolve_provider`／`validate_params`／
        // `health` 这些服务端原判据，所以这里拦下的，提交也一定拦；这里放行的，
        // 提交不会再因为同一类原因失败。警告不拦人，只把话说在前面。
        if (linked?.shotId) {
          try {
            const q = { capability };
            if (mode === 'local' || mode === 'cloud') q.execution_mode = mode;
            if (isCloud()) q.confirmed_cloud = 'true';
            const check = await api.precheckShot(linked.shotId, q);
            const blockers = (check.items || []).filter((i) => i.level === '阻塞');
            if (blockers.length) {
              errEl.textContent = '预检阻塞：' + blockers.map((b) => b.text).join('；');
              return;
            }
            const warns = (check.items || []).filter((i) => i.level === '警告');
            if (warns.length) {
              root.querySelector('#jm-hint').textContent =
                '预检提醒：' + warns.map((w) => w.text).join('；');
            }
          } catch (err) {
            // 预检自己读不到时不拦提交：提交路径上还有同一套服务端判据兜底，
            // 为了"检查工具坏了"就把人锁在门外是另一种不公平
            root.querySelector('#jm-hint').textContent =
              `预检没跑成（${err.message || '未知错误'}），仍会由服务端在提交时把关。`;
          }
        }

        // 按钮在这里**已经**灰了（入口处），不必再 disabled 一次。
        try {
          const job = await api.submitJob(body);
          closeJobModal();
          await refresh();
          openDrawer(job.id);
        } catch (err) {
          errEl.textContent = err.outcomeUnknown
            ? '提交结果尚未确认，服务端可能已创建作业。请先关闭弹窗，在作业列表核对本次任务，避免重复提交。'
            : (err.message || '提交失败');
        }
      } finally {
        // **必须在这里复位**：上面有 6 处提前 return（没选项目、没写提示词、
        // 没有可用槽位、云端数量/单价/来源没填、没勾确认、预检阻塞），
        // 任何一条把 busy 留在 true，这个弹窗的提交按钮就**永久变灰**，
        // 人只能刷新整页才能再提交一次。外层 try/finally 就是为这个兜的。
        submitBusy = false;
        if (submitBtn) submitBtn.disabled = false;
      }
    }

    /* ---------- 参数模板 ---------- */
    function profileScope() {
      const id = root.querySelector('#pf-scope').value;
      if (id === 'global') return null;
      return Number(id) || null;
    }

    function profileRouteOptions() {
      const cap = root.querySelector('#pf-capability').value;
      return '<option value="">不绑定（参数留空）</option>' + optionsFor(cap, null).map((o, i) =>
        `<option value="${i}">${escapeHtml(o.backend_label)} · ${escapeHtml(o.provider)} / ${escapeHtml(o.model)}</option>`).join('');
    }

    function refreshProfileRoute() {
      const cap = root.querySelector('#pf-capability').value;
      const sel = root.querySelector('#pf-route');
      const opts = optionsFor(cap, null);
      sel.innerHTML = '<option value="">不绑定（参数留空）</option>' + opts.map((o, i) =>
        `<option value="${i}">${escapeHtml(o.backend_label)} · ${escapeHtml(o.provider)} / ${escapeHtml(o.model)}</option>`).join('');
    }

    function resetProfileForm() {
      editingProfile = null;
      root.querySelector('#pf-form-title').textContent = '新建模板';
      root.querySelector('#pf-name').value = '';
      root.querySelector('#pf-params').value = '{}';
      root.querySelector('#pf-default').checked = false;
      root.querySelector('#pf-error').textContent = '';
      root.querySelector('#pf-reset').hidden = true;
      root.querySelector('#pf-scope').disabled = false;
      root.querySelector('#pf-capability').disabled = false;
      root.querySelector('#pf-route').disabled = false;
      refreshProfileRoute();
    }

    function profileListHtml() {
      if (!state.profiles.length) {
        return '<p class="hint">还没有模板。下面建一个：下次提交同一类镜头就不用重填参数。</p>';
      }
      return state.profiles.map((p) => `
        <div class="pf-row" data-profile="${p.id}">
          <div class="pf-main">
            <b>${escapeHtml(p.name)}</b>
            <div class="pf-meta">
              <span class="chip plain">${escapeHtml(p.capability_label)}</span>
              <span class="chip ${p.project_id === null ? 'blue' : 'gray'}">${p.project_id === null
                ? '全局' : escapeHtml(p.project_name || '本项目')}</span>
              ${p.is_default ? '<span class="chip green">默认</span>' : ''}
              <span class="t-dim">${p.provider_code
                ? `${escapeHtml(p.provider_code)} / ${escapeHtml(p.model_code)}`
                : '不绑定槽位'}</span>
            </div>
            <code class="pf-params">${escapeHtml(JSON.stringify(p.params))}</code>
          </div>
          <div class="pf-actions">
            <button class="btn xs" data-pf-apply="${p.id}">应用</button>
            <button class="btn xs" data-pf-edit="${p.id}">编辑</button>
            <button class="btn xs danger" data-pf-del="${p.id}">删除</button>
          </div>
        </div>`).join('');
    }

    async function reloadProfiles() {
      state.profiles = await api.generationProfiles(profileProjectId || undefined);
      root.querySelector('#pf-list').innerHTML = profileListHtml();
      refreshTemplateSelect();
    }

    async function openProfiles() {
      profileModal.hidden = false;
      resetProfileForm();
      // 列表跟着这一轮打开时的项目走；保存/删除后也回到同一个项目，
      // 不拿"当前顶栏项目"去重拉——那会把刚建的模板从列表里洗掉。
      profileProjectId = state.projectId || store.currentId || store.projects[0]?.id || null;
      const pid = profileProjectId || '';
      root.querySelector('#pf-capability').innerHTML = Object.entries(CAPS)
        .map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
      const scopes = store.projects.map((p) =>
        `<option value="${p.id}" ${String(p.id) === String(pid) ? 'selected' : ''}>${escapeHtml(p.name)}</option>`);
      if (store.isManager) scopes.unshift('<option value="global">全局（所有项目可见）</option>');
      root.querySelector('#pf-scope').innerHTML = scopes.join('');
      root.querySelector('#pf-list').innerHTML = skeleton(3, 'card');
      refreshProfileRoute();
      try {
        await reloadProfiles();
      } catch (err) {
        root.querySelector('#pf-list').innerHTML = errorBox('参数模板', err, false);
      }
    }

    function closeProfiles() {
      profileModal.hidden = true;
    }

    function editProfile(p) {
      editingProfile = p;
      root.querySelector('#pf-form-title').textContent = `编辑：${p.name}`;
      root.querySelector('#pf-name').value = p.name;
      root.querySelector('#pf-capability').value = p.capability;
      root.querySelector('#pf-scope').value = p.project_id === null ? 'global' : String(p.project_id);
      root.querySelector('#pf-scope').disabled = true;
      root.querySelector('#pf-capability').disabled = true;
      root.querySelector('#pf-route').disabled = true;
      refreshProfileRoute();
      if (p.provider_code) {
        const opts = optionsFor(p.capability, null);
        const idx = opts.findIndex((o) => o.provider === p.provider_code
          && o.model === p.model_code);
        if (idx >= 0) root.querySelector('#pf-route').value = String(idx);
      }
      root.querySelector('#pf-params').value = JSON.stringify(p.params, null, 2);
      root.querySelector('#pf-default').checked = p.is_default;
      root.querySelector('#pf-reset').hidden = false;
      root.querySelector('#pf-name').focus();
    }

    async function saveProfile() {
      const errEl = root.querySelector('#pf-error');
      errEl.textContent = '';
      const name = root.querySelector('#pf-name').value.trim();
      if (!name) { errEl.textContent = '模板名不能为空。'; return; }
      let params;
      try {
        params = JSON.parse(root.querySelector('#pf-params').value || '{}');
      } catch {
        errEl.textContent = '参数不是合法 JSON。'; return;
      }
      if (!params || typeof params !== 'object' || Array.isArray(params)) {
        errEl.textContent = '参数必须是一个对象。'; return;
      }
      const isDefault = root.querySelector('#pf-default').checked;

      try {
        if (editingProfile) {
          await api.updateGenerationProfile(editingProfile.id, { name, params, is_default: isDefault });
        } else {
          const capability = root.querySelector('#pf-capability').value;
          const idx = Number(root.querySelector('#pf-route').value);
          const option = Number.isInteger(idx) ? optionsFor(capability, null)[idx] : null;
          if (root.querySelector('#pf-route').value !== '' && !option) {
            errEl.textContent = '绑定的槽位已经不在了。'; return;
          }
          const scope = profileScope();
          // 模板建在哪个项目，列表就切到哪个项目；建全局模板则留在当前项目
          if (scope !== null) profileProjectId = scope;
          await api.createGenerationProfile({
            project_id: scope, name, capability,
            provider_code: option?.provider || '',
            model_code: option?.model || '',
            params, is_default: isDefault,
          });
        }
        resetProfileForm();
        await reloadProfiles();
      } catch (err) {
        errEl.textContent = err.message || '保存失败';
      }
    }

    /* ---------- 翻页 ---------- */
    /** 取更早的一页并**追加**到列表后面。
     *
     * 为什么追加而不是整块替换：轮询每 3 秒重画一次，替换式翻页会让人刚翻到
     * 第 3 页就被打回第 1 页。追加则只往后长，第一页由轮询负责保鲜。
     *
     * 游标取「当前列表最后一条的 id」——列表本来就是 id 倒序，所以它正是
     * 「已经看到哪儿」的边界。用 id 而不是 offset：这段过程中还有人不断提交
     * 新作业，offset 会让新行把整页往下挤，第二页会重复第一页末尾那几条。
     */
    async function loadMore() {
      if (state.paging || state.ended) return;
      const last = state.jobs[state.jobs.length - 1];
      if (!last) return;
      state.paging = true;
      if (moreHost) moreHost.innerHTML = pagerHtml();   // 立刻显示「正在取」
      try {
        const page = await api.jobs({
          project_id: state.projectId === '__orphan__' ? '' : state.projectId,
          status: state.status,
          capability: state.capability, execution_mode: state.mode,
          q: state.q, limit: PAGE_SIZE, before_id: last.id,
        });
        // 去重：翻页期间可能有作业被删（或游标边界正好落在并发写入上），
        // 列表里出现两行同一个 id 会让人以为重复入库了。
        const seen = new Set(state.jobs.map((j) => j.id));
        const fresh = page.items.filter((j) => !seen.has(j.id));
        state.jobs = state.jobs.concat(fresh);
        state.total = page.total || state.total;
        if (page.items.length < PAGE_SIZE) state.ended = true;
        state.paging = false;
        if (!alive()) return;
        paint();
      } catch (err) {
        state.paging = false;
        state.error = err.message || '未知错误';
        paint();
      }
    }

    /* ---------- 事件绑定 ---------- */
    root.querySelector('#gen-refresh')?.addEventListener('click', () => refresh());
    root.querySelector('#gen-project')?.addEventListener('change', (e) => {
      resetPaging();
      state.projectId = e.target.value;
      // 切到「无主」时自动展开那批；从「无主」切回别的就收起。
      // 否则人点了筛选却还看到折叠栏，会以为筛错了。
      showOrphanJobs = state.projectId === '__orphan__';
      refresh();
    });
    // 无主作业的展开/收起。按钮内容是重画出来的，委托绑在容器上，
    // 免得每轮轮询重绘都要重新绑一次。
    orphanBar?.addEventListener('click', (e) => {
      if (!e.target.closest('#gen-orphan-toggle')) return;
      showOrphanJobs = !showOrphanJobs;
      paint();
    });
    root.querySelector('#gen-status')?.addEventListener('change', (e) => {
      resetPaging();
      state.status = e.target.value;
      refresh();
    });
    root.querySelector('#gen-capability')?.addEventListener('change', (e) => {
      resetPaging();
      state.capability = e.target.value;
      refresh();
    });
    root.querySelector('#gen-mode')?.addEventListener('change', (e) => {
      resetPaging();
      state.mode = e.target.value;
      refresh();
    });
    root.querySelector('#gen-q')?.addEventListener('input', (e) => {
      resetPaging();
      state.q = e.target.value;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => refresh(), 280);
    });
    errorHost.addEventListener('click', (e) => {
      if (e.target.closest('[data-retry-load]')) refresh();
    });

    // 委托绑在容器上：翻页条每次都是重画出来的，直接绑按钮点一次就失效。
    moreHost?.addEventListener('click', (e) => {
      if (e.target.closest('[data-more]')) loadMore();
    });

    rows.addEventListener('click', (e) => {
      const cancel = e.target.closest('[data-cancel]');
      if (cancel) { e.stopPropagation(); askCancel(Number(cancel.dataset.cancel)); return; }
      const reconcile = e.target.closest('[data-reconcile]');
      if (reconcile) {
        e.stopPropagation();
        const j = state.jobs.find((x) => x.id === Number(reconcile.dataset.reconcile));
        if (j) openReconcile(j);
        return;
      }
      const retry = e.target.closest('[data-retry]');
      if (retry) { e.stopPropagation(); askRetry(Number(retry.dataset.retry)); return; }
      const open = e.target.closest('[data-open]');
      if (open) { openDrawer(Number(open.dataset.open)); return; }
      const tr = e.target.closest('tr[data-job]');
      if (tr) openDrawer(Number(tr.dataset.job));
    });

    empty.addEventListener('click', async (e) => {
      if (e.target.closest('[data-new-job]')) openJobModal();
      if (e.target.closest('[data-retry]')) refresh();
    });
    cost.addEventListener('click', (e) => {
      if (e.target.closest('[data-new-job]')) openJobModal();
    });

    root.querySelector('#gen-drawer-close').addEventListener('click', closeDrawer);
    drawer.addEventListener('click', (e) => {
      if (e.target === drawer) closeDrawer();
    });
    drawerBody.addEventListener('click', (e) => {
      const cancel = e.target.closest('[data-cancel]');
      if (cancel) { askCancel(Number(cancel.dataset.cancel)); return; }
      const reconcile = e.target.closest('[data-reconcile]');
      if (reconcile) {
        const j = state.jobs.find((x) => x.id === Number(reconcile.dataset.reconcile));
        if (j) openReconcile(j);
        return;
      }
      const attention = e.target.closest('[data-attention]');
      if (attention) { askAttention(Number(attention.dataset.attention)); return; }
      const retry = e.target.closest('[data-retry]');
      if (retry) askRetry(Number(retry.dataset.retry));
    });
    drawer.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && jobModal.hidden && profileModal.hidden
          && reconcileModal.hidden) closeDrawer();
    });

    root.querySelector('#gen-new')?.addEventListener('click', () => openJobModal());
    root.querySelector('#jm-cancel').addEventListener('click', closeJobModal);
    root.querySelector('#jm-submit').addEventListener('click', submitJob);
    root.querySelector('#jm-capability').addEventListener('change', () => {
      refreshModeSelect();
      // 能力一换，场景池跟着换：场景有能力的归属，留着上一个能力的场景
      // 只会得到 0 候选，人还以为是模型没配好。
      // 能力换格＝人接管了路线，上一笔预填留痕作废。
      clearScenePrefillTrace();
      refreshSceneSelect('');
    });
    // 注意别把 change 事件本身传进去——renderScenePanel 的头一个参数
    // 是 allowAuto（布尔），事件对象是 truthy 会把"允许自动"变成巧合成立。
    root.querySelector('#jm-scene').addEventListener('change', () => renderScenePanel(true));
    // 「用它」与「撤销预填」都是动态渲染出来的，走委托。
    //
    // **委托不能挂在 root（#view-root）上**：那个 div 由 index.html 提供，
    // router.js:146 每次重绘只换 innerHTML，元素本身一直在，于是挂在它身上的
    // 监听器不随 DOM 消失。而 mount() 每次重绘都会跑（切页、保存、轮询、
    // app:refresh 都走 router 的同一个渲染点），刷新 N 次就叠 N 个历史闭包，
    // 一次点击会把 scenePrefillTo／撤销执行 N 遍——它们各自都读-改-写
    // #jm-mode 与 scenePrefill，叠起来就是"点一下弹窗里的下拉跳了两次"。
    // 挂到 .modal-box 上：它在每次 render 里都是新节点，监听器随它一起回收。
    // （#jm-scene-panel 与 #jm-scene-prefill 都在这个 .modal-box 内。）
    const sceneHost = root.querySelector('#job-modal .modal-box') || root;
    sceneHost.addEventListener('click', (e) => {
      const use = e.target.closest('[data-scene-use]');
      if (use) {
        const c = sceneCandidates[Number(use.dataset.sceneUse)];
        if (!c) return;
        const key = root.querySelector('#jm-scene').value;
        const label = (state.scenes.find((s) => s.scene_key === key) || {}).scene_label || key;
        if (!scenePrefillTo(c, label)) {
          root.querySelector('#jm-hint').textContent =
            `这一档（${c.provider}/${c.model_code}）不在提交槽位池里——`
            + '它可能没启用或没填 provider，请到模型仓库看这一行。';
        }
        return;
      }
      if (e.target.closest('#jm-scene-undo')) undoScenePrefill();
    });
    root.querySelector('#jm-mode').addEventListener('change', () => {
      // 人自己动了这两个下拉＝他接管了，撤销信息作废（下一笔预填重新记 before）。
      clearScenePrefillTrace();
      refreshRouteSelect();
    });
    root.querySelector('#jm-route').addEventListener('change', () => {
      clearScenePrefillTrace();
      renderParams();
      renderCostSection();
    });
    // 改分辨率/时长时，官方价档跟着变（720P 与 1080P 是两条价）
    root.querySelector('#jm-params').addEventListener('change', () => {
      if (!jobModal.hidden && isCloud()) renderCostSection();
    });
    root.querySelector('#jm-template').addEventListener('change', (e) => {
      const p = state.profiles.find((x) => String(x.id) === e.target.value);
      if (p) applyProfile(p);
    });
    root.querySelector('#jm-project').addEventListener('change', async (e) => {
      try {
        state.profiles = await api.generationProfiles(e.target.value || undefined);
        refreshTemplateSelect();
      } catch { /* 模板读不到不影响提交 */ }
    });

    root.querySelector('#gen-profiles').addEventListener('click', openProfiles);
    root.querySelector('#pf-close').addEventListener('click', closeProfiles);
    root.querySelector('#pf-reset').addEventListener('click', resetProfileForm);
    root.querySelector('#pf-save').addEventListener('click', saveProfile);
    root.querySelector('#pf-capability').addEventListener('change', refreshProfileRoute);
    root.querySelector('#pf-list').addEventListener('click', async (e) => {
      const apply = e.target.closest('[data-pf-apply]');
      if (apply) {
        const p = state.profiles.find((x) => String(x.id) === apply.dataset.pfApply);
        if (!p) return;
        closeProfiles();
        if (jobModal.hidden) await openJobModal({ template: p, capability: p.capability });
        else applyProfile(p);
        return;
      }
      const edit = e.target.closest('[data-pf-edit]');
      if (edit) {
        const p = state.profiles.find((x) => String(x.id) === edit.dataset.pfEdit);
        if (p) editProfile(p);
        return;
      }
      const del = e.target.closest('[data-pf-del]');
      if (del) {
        const p = state.profiles.find((x) => String(x.id) === del.dataset.pfDel);
        if (!p) return;
        const ok = await confirmDanger({
          title: '删除模板？', body: `「${escapeHtml(p.name)}」会被删掉；已经提交的作业不受影响。`,
          okText: '删除', danger: true,
        });
        if (!ok) return;
        if (await run('删除模板', () => api.deleteGenerationProfile(p.id))) {
          await reloadProfiles();
        }
      }
    });

    bindModalDismiss(jobModal, closeJobModal);
    bindModalDismiss(profileModal, closeProfiles);
    root.querySelector('#rc-cancel').addEventListener('click', closeReconcile);
    root.querySelector('#rc-submit').addEventListener('click', submitReconcile);
    reconcileModal.addEventListener('click', (e) => { if (e.target === reconcileModal) closeReconcile(); });
    bindModalDismiss(reconcileModal, closeReconcile);

    /* ---------- 首次加载与轮询 ---------- */
    (async () => {
      try {
        const [options, current, scenes] = await Promise.all([
          api.providerOptions(), api.currentProviders(),
          // 场景骨架读不到不该让整页读失败：它只是提交框里多一格提示。
          api.modelScenes().catch(() => ({ items: [] })),
        ]);
        state.options = options;
        state.current = current;
        state.scenes = scenes.items || [];
      } catch (err) {
        state.optionsError = err.message;
      }
      await refresh({ quiet: true });
      // 从分镜点「生成」过来：打开提交框并预填。只消费一次，
      // 刷新页面不会再弹；参数仍走服务端白名单与默认轨。
      const prefill = takeJobPrefill();
      if (prefill) await openJobModal(prefill);
    })();

    pollTimer = setInterval(() => {
      if (!alive()) { clearInterval(pollTimer); pollTimer = null; return; }
      // 弹窗开着时用户正在填，不重画列表；抽屉开着时只更新抬头
      if (!jobModal.hidden || !profileModal.hidden || !reconcileModal.hidden) return;
      // 标签页在后台时不发请求（2026-11-10 审计补的纪律，与 `monitor.js:642`
      // 和 `app.js:797` 同一套）。这里原来是**无条件**每 3 秒一发，一轮三请求
      // （队列＋成本账＋worker 状态）；人切到别的标签页干活时，这些请求
      // 照跑不误，而屏幕上没有"让人知道"的对象。
      //
      // 注意浏览器对后台定时器的节流是**另一个**变量（Chrome 对隐藏标签页
      // 最低 1 次/分钟），所以这条改动的收益不是"省掉全部后台请求"，
      // 而是"隐藏期间不再有请求"，二者实测频率见设计稿修订行。
      if (document.hidden) return;
      refresh({ quiet: true });
    }, 3000);

    // 回到前台补一拍：光跳过不够——人切回来时看到的是**离开那一刻**的队列，
    // 而作业可能已经跑完了。补这一下才让"回来看到的就是现在"。
    // 只在标签页重新可见时触发，不额外加轮询。
    // `visibilitychange` 只在 `document` 上派发，**不冒泡到普通元素**，
    // 所以只能挂 document。但 `router.js:147` 每次切页都重跑 `mount()`，
    // 挂 document 的监听不随视图回收——切十个来回就叠十份，一次切标签页
    // 触发十轮 `refresh()`。这里**成对解绑**：模块级变量记住这一次挂的那个
    // 闭包，下次 mount 开头先解掉上一份（`test_视图里的_window_监听必须成对解绑`
    // 这条门禁就是为这种情形立的，它抓到了这一处，按它的要求改成解绑而不是
    // 靠 `isConnected` 兜——`root` 是 `#view-root`，换页时它仍在 body 里，
    // `isConnected` 永远为真，挡不住）。
    if (visHandler) document.removeEventListener('visibilitychange', visHandler);
    visHandler = () => {
      if (!alive() || document.hidden) return;
      if (!jobModal.hidden || !profileModal.hidden || !reconcileModal.hidden) return;
      refresh({ quiet: true });
    };
    document.addEventListener('visibilitychange', visHandler);
  },
};
