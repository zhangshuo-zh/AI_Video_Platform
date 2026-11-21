/**
 * 模型中心 · 3 运行监控（队列、worker、成本、最近失败）。
 *
 * 本文件由 models.js（2277 行）按子页拆出。共享底座见 ./shared.js：
 * 缓存、常量、跨子页的纯函数都在那边，这里只管本子页的渲染与交互。
 */
import { api } from '../../api.js';
import { store } from '../../store.js';
import {
  agoText, announce, bindModalDismiss, bindSubChips, confirmDanger, escapeHtml, run, subChipRow,
} from '../../util.js';
import { getSub, setSub } from '../../navstate.js';
import { gotoEntry } from '../../goto.js';
import { HEALTH, JOB_STATUS, VAL_BADGE, VAL_KIND_LABEL, alertLabel } from '../../validation.js';
import { REG_SOURCES, unitLabel } from '../../enums.js';
import {
  SUBS, cache, stateChip, healthChip, manifestAxis, adapterOf, manifestProviders,
  sourceLabels, sourceLabel, overviewCard, alertNotify, HISTORY_PAGE,
  showWorkerHistory, toggleWorkerHistory,
} from './shared.js';

/* ================= 工作区 3 · 运行监控 ================= */
/**
 * 这一页复用队列、进程、守护、模型统计与验证记录接口。
 * 为什么不去新建一套"模型运行记录"表：
 * Job 才是执行事实的唯一出口（系统设计 §1 不变量 2），
 * 这里再攒一份就必然长出两套状态。
 */

/* 作用域轮询的计时器。与生成中心同一套守卫（generate.js:1879）：
   模块级持有、mount 先清旧、alive() 判锚点还在不在——切走子页时路由替换
   #view-root，锚点 isConnected 变 false，计时器自己清掉，不会叠出第二条。
   为什么不用全站那条 20s 轮询：它只刷顶栏 chip（app.js paintWorker），
   监控页的队列表与告警它一概不动——名为"运行监控"却是切页那一刻的快照。 */
let pollTimer = null;
let daemonDebounceTimer = null;
// 回前台补一拍的那个闭包。visibilitychange 只在 document 上派发、不冒泡，
// 挂上去就不随 #mc-live 回收，所以必须记住引用、和计时器一起收。
let visHandler = null;
const POLL_MS = 5000;
let pollGeneration = 0;
let operationBusy = false;
let mutationVersion = 0;
let monitorFreshness = { at: '', error: '', failed: [] };
const drafts = new WeakMap();

/**
 * 这页的"数据是活的"那一行。
 *
 * v1.52 改过三件事，理由都在实测里（2026-11-17）：
 *
 * 1. **删掉「立即刷新」按钮**。它不是新功能：点击走的是 `root.__monitorTick()`，
 *    与下面 `setInterval(tick, POLL_MS)` 是**同一个函数**。实测点一下 63ms 见效、
 *    不点最多等 2.95s——为了省不到 3 秒，在页头常驻一颗按钮，不划算。
 *    按钮删了，但**"会自己刷新"这句话必须留着**：不留，新人会怀疑这是一屏快照，
 *    停在这里盯半天不敢动。所以保留文案、去掉手动入口。
 *
 * 2. **正常态不再带 `role="status"`**。原来整行是活区，而这行每拍都换一次时间戳，
 *    实测 11 秒内文字变 2 次——用读屏的人停在这页就是每 5 秒被念一次
 *    "最后完整更新 21时10分06秒"，连续不断。正常节拍不该念。
 *    异常态（读取失败）才挂活区：那才是"需要立刻知道"的事。
 *
 * 3. **时间戳不再逐秒显示**。它原来是 `toLocaleTimeString()`，精确到秒，
 *    于是每 5 秒这一行就变一次。改成"数据是活的"这句静态话，
 *    外加一个异常才出现的来源说明——人要看最近一次成功的时刻，
 *    异常文案里仍然带着（`保留上次数据（21:10:06）`），够用。
 */
function freshnessHtml() {
  if (monitorFreshness.error) {
    return `<p id="mc-freshness" class="hint mc-fresh-err" role="status">${escapeHtml(
      `连接异常：${monitorFreshness.error}；保留上次数据`
      + `（${monitorFreshness.at || '尚未成功刷新'}），恢复后自动清除。`)}</p>`;
  }
  return `<p id="mc-freshness" class="hint mc-fresh-ok"></p>`;
}

function restoreDrafts(root) {
  for (const [id, draft] of Object.entries(drafts.get(root) || {})) {
    const el = root.querySelector(`#${id}`);
    if (el) {
      if (el.type === 'checkbox') el.checked = draft.value;
      else el.value = draft.value;
    }
  }
}

function clearSubmittedDrafts(root, submitted) {
  const current = drafts.get(root) || {};
  for (const [id, draft] of Object.entries(submitted)) {
    if (current[id] === draft) delete current[id];
  }
}

function syncOperationButtons(root) {
  root.querySelectorAll('#mc-wp-start, #mc-wp-stop, [data-stop-pid], [data-quick-start-worker], #mc-daemon-on, #mc-daemon-sync, #mc-conc-save, #mc-conc-force, #mc-wp-clean')
    .forEach((b) => { b.disabled = operationBusy || (!overviewReady && b.matches('#mc-wp-stop, [data-stop-pid], #mc-wp-clean')); });
  const restartBtn = root.querySelector('#mc-wp-restart');
  if (restartBtn) restartBtn.disabled = operationBusy || !overviewReady || !(Number(cache.workerProcess?.count) > 0);
}

/** 资源类的界面名。队列分桶的键是后端的英文类名，界面一律念中文
 *  （并发设置那张表早就用中文，这里原来直接印 text/image，同页两套叫法）。 */
const RES_LABEL = { text: '文本', image: '图像', video: '视频', audio: '音频', compose: '成片' };

/** 验证留痕的筛选条件：切出再进重置；切换时向服务端请求匹配的全库记录。 */
let valFilterState = '';
let valFilterKind = '';
let valSeq = 0;
let overviewReady = false;
let overviewRows = [];
let logFetched = false;
let optionStates = new Map();
let valExtra = { provider: '', model: '', dateFrom: '', dateTo: '' };
let appliedFilters = {};
let logFollowing = true;
let logSnapshot = null;
let logLimit = 12;

function valFilterParams() {
  return {
    kind: valFilterKind || '',
    onlyBad: valFilterState === 'bad',
    ...valExtra,
  };
}

function jobJumpHtml(jobId) {
  if (!jobId) return '—';
  const id = Number(jobId);
  return `<button type="button" class="btn-link mono" data-goto-job="${id}" title="前往生成中心查看作业 #${id}">#${id}</button>`;
}

/** 验证历史的一条行。首屏渲染与「更早」追加共用这一份——原来两处各抄了一遍
 *  同一段行 HTML，改一处忘另一处，翻到第二页列就错位。 */
function valRow(v) {
  const when = escapeHtml(String(v.created_at || '').slice(0, 16));
  const model = escapeHtml(v.model || '未记录模型');
  const detail = String(v.detail || '');
  const rowCls = v.state && v.state !== 'ok' ? ' class="mc-val-bad"' : '';
  return `<tr${rowCls}>
    <td class="mc-val-kind"><span class="chip plain">${escapeHtml(v.kind_label || VAL_KIND_LABEL[v.kind] || v.kind)}</span></td>
    <td class="mc-val-result">${stateChip(v.state)}</td>
    <td class="hint mc-val-slot"><strong class="mc-val-mobile-model">${model}</strong>${escapeHtml(v.capability_label || v.capability)} · ${escapeHtml(v.backend)} · ${escapeHtml(v.provider || '—')}<br class="mc-val-desktop-model"><code class="mc-val-desktop-model">${model}</code></td>
    <td class="mc-val-job">${v.job_id ? jobJumpHtml(v.job_id) : '—'}</td>
    <td class="mc-val-detail-cell"><span class="t-desc"${detail ? ` title="${escapeHtml(detail)}"` : ''}>${escapeHtml(detail || '—')}</span></td>
    <td class="t-dim mc-val-time">${when}</td>
  </tr>`;
}

export function validationPager() {
  const total = cache.validations?.total || 0;
  const loaded = (cache.validations?.items || []).length;
  const page = cache.validationPage || { before: null, pages: 0 };
  const older = loaded > 0 && loaded < total && !page.exhausted;
  return `<span class="hint">共 ${total} 条 · 已加载 ${loaded} 条`
    + `${page.pages ? ` · 已翻 ${page.pages} 页` : ''}</span> `
    + `${older ? '<button class="btn xs" id="mc-val-older">更早的</button>' : ''}`;
}

/**
 * 队列与 worker ＋ 告警两张卡，数据来自 worker/status。
 * 验证留痕不跟着每 5 秒重画——
 * 前者只在登记/验证时才变，后者一重画就把人刚翻到的页码 reset 回第一页。
 * 每次调用都现读 cache.worker，所以轮询更新缓存后重画即是新数。
 */
function ageLabel(seconds) {
  if (seconds == null) return '—';
  return seconds < 60 ? `${seconds} 秒` : agoText(seconds).replace(/前$/, '');
}

function staleLabel(source) {
  return monitorFreshness.failed.includes(source) ? '<span class="chip amber">读取失败</span>' : '';
}

function alertAction(a) {
  if (a.job_id) return `<button class="btn xs" data-goto-job="${Number(a.job_id)}">查看作业 #${Number(a.job_id)}</button>`;
  if (a.code === 'worker_offline') return '<button class="btn xs" data-monitor-target="mc-process-card">查看 Worker 进程</button>';
  if (a.code?.startsWith('concurrency_')) return '<button class="btn xs" data-monitor-target="mc-concurrency-card">查看并发设置</button>';
  if (a.code === 'needs_attention') return '<button class="btn xs" data-job-status="needs_attention">查看待核对作业</button>';
  return '<button class="btn xs" data-goto-gen>前往生成中心</button>';
}

function liveCards() {
  const w = cache.worker || {};
  const q = w.queue || {};
  const byRes = q.by_resource || {};

  const resChips = (n) => {
    const r = n.resources || {};
    const bits = [];
    if (r.disk_free_gb != null) bits.push(`盘余 ${r.disk_free_gb} GB`);
    if (r.load1 != null) bits.push(`负载 ${Number(r.load1).toFixed(2)}/${r.cores ?? '?'}`);
    if (r.rss_peak_mb != null) bits.push(`峰值内存 ${r.rss_peak_mb} MB`);
    return bits.length ? bits.join(' · ') : '—';
  };
  const p = cache.workerProcess || {};
  const nodeRow = (n) => {
    const pid = Number(n.pid);
    const isManaged = n.ownership === 'managed';
    const isExternal = n.ownership === 'local_external';
    const stateBadge = n.heartbeat_state === 'missing'
      ? '<span class="chip amber">等待心跳 · 受管</span>'
      : !n.online
        ? '<span class="chip gray">离线</span>'
        : isManaged
          ? '<span class="chip green">在线 · 受管</span>'
          : isExternal
            ? '<span class="chip blue" title="本机外部在线心跳，不授予停止权限">在线 · 外部</span>'
            : n.ownership === 'remote' ? '<span class="chip plain">在线 · 远程</span>'
              : '<span class="chip green">在线</span>';
    const op = n.can_stop && overviewReady && (p.pids || []).includes(pid)
      ? `<button type="button" class="btn xs" data-stop-pid="${pid}" aria-label="停止 PID ${pid}">停止</button>`
      : isExternal
        ? '<span class="hint">请在原终端关闭</span>'
        : '<span class="hint">—</span>';
    return `<tr>
      <td class="mono">${escapeHtml(n.worker_id || `PID ${pid}`)}</td>
      <td>${stateBadge}</td>
      <td>${n.current_job_id ? jobJumpHtml(n.current_job_id) : '<span class="hint">空闲</span>'}</td>
      <td class="hint">${n.heartbeat_at ? `${escapeHtml(String(n.heartbeat_at).slice(0, 16))} ${agoText(n.age_seconds)}` : n.heartbeat_state === 'missing' ? '等待心跳' : '—'}</td>
      <td class="hint">${escapeHtml(resChips(n))}</td>
      <td class="wp-op">${op}</td>
    </tr>`;
  };

  // 一行＝一个进程，这是租约归属的要求，不该改；要改的是"默认给你看几行"。
  // 连续开发攒下的死行既不影响队列、也不该每天占着面板让人分辨哪条是真的在干活。
  const allNodes = overviewRows.length ? overviewRows : (w.workers || []);
  const liveNodes = allNodes.filter((n) => n.online || n.heartbeat_state === 'missing');
  const historyCount = w.history_count != null ? Number(w.history_count) : allNodes.filter((n) => !n.online).length;
  const shownNodes = showWorkerHistory ? allNodes : liveNodes;
  const workers = shownNodes.map(nodeRow).join('');
  const historyLine = historyCount ? `
    <div class="card-body mc-offline-bar">
      <button class="btn xs" id="mc-worker-history">
        ${showWorkerHistory ? '收起离线进程'
          : `另有 ${historyCount} 个进程在 7 天内登记过 · 现已离线（点开看）`}</button>
      <span class="hint">离线进程不领活，7 天后自动清理</span>
    </div>` : '';

  const queueChips = Object.entries(byRes).map(([k, v]) =>
    `<span class="chip ${v.running ? 'blue' : (v.queued ? 'amber' : 'gray')}">
      ${escapeHtml(RES_LABEL[k] || k)} 等 ${v.queued} · 跑 ${v.running}</span>`).join('')
    || '<span class="t-dim">队列里没有任何活</span>';

  // by_status 是各状态作业的**累计数**（库里全部作业按状态分组），不是"此刻在跑"。
  // 它挂在「执行中」那个大数字下面，原来又印着 failed／succeeded 的英文键，
  // 一眼看过去像是"执行中 0，但有 2 个 failed 正在跑"。念成中文并点明是累计。
  const statusChips = Object.entries(w.by_status || {}).map(([k, v]) => {
    const label = (JOB_STATUS[k] || [])[1] || k;
    const statusParam = ['queued', 'starting', 'running', 'cancel_requested'].includes(k)
      ? 'queued,starting,running,cancel_requested' : k;
    return `<button type="button" class="chip plain" data-job-status="${escapeHtml(statusParam)}"
      title="前往生成中心查看「${escapeHtml(label)}」作业">${escapeHtml(label)} ${v} ↗</button>`;
  }).join('') || '<span class="t-dim">—</span>';

  const alertList = [...(w.alerts || [])].sort((a, b) => Number(b.level === 'bad') - Number(a.level === 'bad'));
  const activeJobs = (w.running_jobs || []).map((j) => `<li>
    ${jobJumpHtml(j.id)} <span>${escapeHtml(j.model || RES_LABEL[j.capability] || j.capability || '—')}</span>
    <span>已运行 ${escapeHtml(ageLabel(j.started_seconds))}</span>
    <span>最近日志 ${escapeHtml(j.last_log_at || '未取得')}</span>
    <span class="mono" title="${escapeHtml(j.worker_id || '')}">${escapeHtml(j.worker_id || '未分配 Worker')}</span>
  </li>`).join('');
  const alerts = alertList.map((a) => `<li class="mc-alert-item">
      <span class="chip ${a.level === 'bad' ? 'red' : 'amber'}">${escapeHtml(alertLabel(a.code))}</span>
      <span>${escapeHtml(a.text)}</span>
      ${alertAction(a)}
    </li>`).join('');

  const alertCard = alertList.length ? `
  <div class="card mb-card mc-alert-card">
    <div class="card-head">
      <div style="display:flex;align-items:center">
        <h2>告警（${alertList.length}）</h2>
        <span class="help-tip">
          <span class="help-icon">?</span>
          <span class="tip-pop tip-right">
            <span class="tip-title">告警观测与处置原则</span>
            <div class="tip-desc">判定与处置严格分离：这里只展示异动告警，取消与重试仍回生成中心做。</div>
            <ul class="tip-list">
              <li><b>判定与处置分离</b>：模型中心仅陈述异常指标（队列积压、探活失败、并发冲突）。</li>
              <li><b>处置主权</b>：任务重试、作业撤销等业务操作请前往生成中心发起。</li>
            </ul>
          </span>
        </span>
      </div>
      <span class="right">
        <button type="button" id="mc-alert-generate" class="btn xs" data-goto-gen>前往生成中心</button>
      </span>
    </div>
    <div class="card-body">
      <ul class="hint flush">${alerts}</ul>
    </div>
  </div>` : '';

  return `${alertCard}
  <div class="card mb-card" >
    <div class="card-head">
      <div style="display:flex;align-items:center">
        <h2>队列与 Worker</h2>
        ${staleLabel('队列 / 心跳')}
        <span class="help-tip">
          <span class="help-icon">?</span>
          <span class="tip-pop tip-right">
            <span class="tip-title">队列与 Worker 运行事实</span>
            <div class="tip-desc">实时读取 <code>/api/worker/status</code>，与生成中心保持完全同一口径。</div>
            <ul class="tip-list">
              <li><b>不开第二套队列</b>：模型中心只做真相观测，排队与调度权威归生成中心。</li>
              <li><b>单进程按行展示</b>：并发上限与认领租约记在各自进程上，不合并，离线行默认折叠。</li>
              <li><b>双通道实时探活</b>：显示当前作业与等待数，离线或挂死时及时预警。</li>
            </ul>
          </span>
        </span>
      </div>
      <span class="right">
        ${monitorFreshness.error ? '' : '<span class="chip plain">5s 自动刷新</span>'}
        <span class="chip ${alertList.length ? 'red' : 'green'}">${alertList.length ? `${alertList.length} 条告警` : '无异常告警'}</span>
        <span class="chip ${w.online ? 'green' : 'gray'}">${w.online ? 'worker 在线' : 'worker 未接入'}</span>
      </span>
    </div>
    <div class="card-body dash-grid mc-dash-grid">
      <div><span class="grp">排队</span><b>${q.queued ?? '—'}</b><span class="hint">最长等待 ${escapeHtml(ageLabel(w.oldest_queued_seconds))}</span></div>
      <div><span class="grp">执行中</span><b>${q.running ?? '—'}</b><span class="hint">租约过期 ${w.expired_leases ?? '—'}</span></div>
      <div><span class="grp">在线 Worker</span><b>${w.live_count ?? liveNodes.length}</b></div>
      <div><span class="grp">待核对作业</span><b>${w.needs_attention ?? '—'}</b></div>
    </div>
    <div class="card-body mc-resource-strip">
      <div class="mc-resource-chips">${queueChips}</div>
      <div id="mc-job-history" class="mc-job-history">
        <span class="hint">历史累计</span>
        <div class="dash-chips">${statusChips}</div>
      </div>
    </div>
    ${activeJobs ? `<details id="mc-running-jobs" class="card-body mc-monitor-detail"><summary id="mc-running-summary">执行中作业 · ${w.running_jobs.length}</summary><ul>${activeJobs}</ul></details>` : ''}
    <div class="table-wrap"><table class="mc-worker-table">
      <thead><tr><th>Worker</th><th>状态</th><th>当前作业</th><th>最近心跳</th><th>机器实况</th><th class="wp-op">操作</th></tr></thead>
      <tbody>${workers || `<tr><td colspan="6" class="hint"><div class="mc-worker-empty"><span>${
        allNodes.length ? '当前没有在线 Worker——队列将暂停推进，可在下方「Worker 进程」启动'
                         : '还没有 Worker 登记过心跳'}</span><button type="button" class="btn xs" data-quick-start-worker>立即启动 1 个</button></div></td></tr>`}</tbody>
    </table></div>
    ${historyLine}
  </div>`;
}

function opsCards() {
  return `${workerProcessCard()}${concurrencyCard()}`;
}

function optionKey(field, provider = '') {
  return JSON.stringify([field, field === 'model' ? provider : '']);
}

function validationSelectOptions(field, emptyLabel, currentValue = '', provider = '') {
  const choices = new Set(optionStates.get(optionKey(field, provider))?.items || []);
  for (const entry of [...(cache.providers || []), ...(cache.validations?.items || [])]) {
    if (field === 'model' && provider && entry.provider !== provider) continue;
    if (entry[field]) choices.add(entry[field]);
  }
  if (currentValue) choices.add(currentValue);
  return [`<option value="">${escapeHtml(emptyLabel)}</option>`,
    ...[...choices].sort((a, b) => a.localeCompare(b)).map((v) =>
      `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`)].join('');
}

async function loadValidationOptions(root, field, more = false) {
  const host = root.querySelector('#mc-val-extra');
  if (!host) return;
  const provider = field === 'model' ? root.querySelector('#mc-val-provider').value : '';
  const key = optionKey(field, provider);
  const previous = optionStates.get(key);
  if (previous?.pending || (more ? !previous?.next_after : previous && !previous.error)) return;
  const state = { ...previous, items: previous?.items || [], pending: true, error: false };
  const page = optionStates;
  page.set(key, state);
  syncValidationSelectOptions(root);
  try {
    const result = await api.validationOptions(field, more ? previous.next_after : '', provider);
    Object.assign(state, { items: [...state.items, ...result.items], next_after: result.next_after });
  } catch {
    state.error = true;
  } finally {
    state.pending = false;
    if (host.isConnected && page === optionStates) syncValidationSelectOptions(root);
  }
}

export function pageMonitor() {
  const s = cache.stats;
  monitorFreshness = { at: '', error: '', failed: [] };
  valFilterState = '';
  valFilterKind = '';
  valExtra = { provider: '', model: '', dateFrom: '', dateTo: '' };
  appliedFilters = { ...valFilterParams() };
  valSeq += 1;
  optionStates = new Map();
  overviewReady = false;
  overviewRows = [];
  logFetched = false;
  const kindOptions = Object.entries(VAL_KIND_LABEL).map(([k, label]) =>
    `<option value="${escapeHtml(k)}">${escapeHtml(label)}</option>`).join('');
  return `
  <div class="mc-monitor-status">${freshnessHtml()}</div>
  <div id="mc-live">${liveCards()}</div>
  <div id="mc-ops" class="mc-ops-grid">${opsCards()}</div>

  <div class="card">
    <div class="card-head">
      <div style="display:flex;align-items:center">
        <h2>最近验证留痕</h2>
        <span class="help-tip">
          <span class="help-icon">?</span>
          <span class="tip-pop tip-right">
            <span class="tip-title">验证记录与模型统计边界</span>
            <div class="tip-desc">登记 ${s.registered_total} · 可用 ${s.available_total} · 需要看一眼 ${s.needs_attention}。仅支持真实调用或产物验收记录追加，只增不改。</div>
            <ul class="tip-list">
              <li><b>只增不改</b>：验证记录属于防篡改证据链，密钥不进记录，每页最多 ${HISTORY_PAGE} 条。</li>
              <li><b>按 provider/model 聚合成本：不支持</b>（cost_summary 按币种×能力×状态聚合）。</li>
              <li><b>验证历史</b>支持按模型、供应商和日期筛选；成本与耗时统计尚无时间窗口。</li>
              <li><b>分档执行耗时：不支持</b>（仅记录单条作业耗时，无分位数统计）。</li>
            </ul>
          </span>
        </span>
      </div>
      <div class="right">
        <span class="mc-val-pager">${validationPager()}</span>
      </div>
    </div>
    <div class="mc-val-tools mc-val-filterbar">
      <select class="ctl sm" id="mc-val-filter-state" aria-label="按结果筛选">
        <option value="">全部结果</option>
        <option value="bad">仅异常 / 未通过</option>
      </select>
      <select class="ctl sm" id="mc-val-filter-kind" aria-label="按出处筛选">
        <option value="">全部出处</option>
        ${kindOptions}
      </select>
      <details class="mc-val-extra" id="mc-val-extra"><summary>更多筛选</summary><div class="mc-val-extra-fields">
      <select class="ctl sm" id="mc-val-provider" aria-label="供应商筛选">${validationSelectOptions('provider', '全部供应商')}</select>
      <button type="button" class="btn xs" id="mc-val-more-provider" hidden>更多供应商</button>
      <select class="ctl sm" id="mc-val-model" aria-label="模型筛选">${validationSelectOptions('model', '全部模型')}</select>
      <button type="button" class="btn xs" id="mc-val-more-model" hidden>更多模型</button>
      <span class="hint" id="mc-val-options-status"></span>
      <span class="hint">日期（含起止日）</span>
      <input class="ctl sm" type="date" id="mc-val-from" aria-label="开始日期">
      <input class="ctl sm" type="date" id="mc-val-to" aria-label="结束日期">
      <button class="btn xs" id="mc-val-apply">应用筛选</button>
      </div></details>
      <button class="btn xs" id="mc-val-reset">重置</button>
      <span class="mc-val-applied"></span>
    </div>
    <div class="table-wrap mc-val-scroll"><table>
      <thead><tr><th>出处</th><th>结果</th><th>槽位</th><th>作业</th><th>说明</th><th>时间</th></tr></thead>
      <tbody id="mc-val-rows">${(cache.validations?.items || []).map(valRow).join('')
        || '<tr><td colspan="6" class="hint">还没有验证记录</td></tr>'}</tbody>
    </table></div>
  </div>`;
}

/* ================= 工作区 3 交互 ================= */
/** 离线进程折叠按钮：轮询重画 #mc-live 后也要重绑，所以单独抽出来。
 *  展开状态存在 shared 的模块变量里，重画不会把人刚点开的那段折回去。 */
function bindWorkerHistory(root) {
  const button = root.querySelector('#mc-worker-history');
  if (!button || button.dataset.bound) return;
  button.dataset.bound = 'true';
  button.addEventListener('click', () => {
    toggleWorkerHistory();
    // 只重画 live 两张卡，不动验证留痕（保住人翻到的页码）。
    const live = root.querySelector('#mc-live');
    if (refreshMarkup(live, liveCards())) bindWorkerHistory(root);
  });
}


/* ================= worker 进程启停（0048） ================= */
/**
 * 这张卡把"开终端敲 ./run.sh worker"收进界面，但**它不是守护进程**。
 *
 * 为什么这句话必须写在界面上：按钮存在会让人以为"崩了会自动拉起"，
 * 而事实是没有 supervise——worker 挂了就得再点一次。这个错觉比没有
 * 按钮更危险：没有按钮时人会去查进程，有按钮时人只会刷新页面等它。
 *
 * 状态一律**现读 /api/worker/process**（后端以 PID 文件 + kill -0 判），
 * 不在前端记"我刚起过"——API 重启后内存没了而 worker 还活着，
 * 记内存状态会让按钮显示"未启动"，人一按就起了第二个去抢显存。
 */
function workerProcessStatusHtml() {
  const p = cache.workerProcess;
  if (!p || p.count == null) return '<span class="chip gray">进程状态未取得</span>';
  const parts = [];
  // 右上角只给数量；具体 PID 在下面的进程表里逐行列出，进程多了也不会把标题栏挤成一长串。
  if (p.count) parts.push(`<span class="chip green">受管运行中 ${Number(p.count)} 个</span>`);
  if (p.external_online_count) parts.push(`<span class="chip blue" title="本机外部在线心跳，不授予停止权限">外部运行中 ${Number(p.external_online_count)} 个</span>`);
  if (p.stale_pids?.length) parts.push(`<span class="chip amber">残留登记 ${p.stale_pids.length} 条 · ${escapeHtml(p.stale_pids.join('、'))}</span>`);
  return parts.join(' ') || '<span class="chip gray">未运行</span>';
}

/** 日志行按级别着色。textContent 必须与原始行逐字一致（复制日志与验收都读它），
 *  所以只在原文外面包 span，不改、不补任何字符。
 *  没有时间戳的续行（traceback 的栈帧、异常名）沿用上一行的级别：
 *  崩溃栈整段红，而不是只有第一行红。 */
const LOG_HEAD = /^(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[.,]\d+)?\s+)(\[(\w+)\])(.*)$/s;
const LOG_LEVEL = { CRITICAL: 'error', ERROR: 'error', WARNING: 'warn', WARN: 'warn', INFO: 'info', DEBUG: 'debug' };

function logListHtml(lines) {
  let carry = '';
  return lines.map((line) => {
    const text = String(line);
    const m = LOG_HEAD.exec(text);
    if (m) {
      carry = LOG_LEVEL[m[3].toUpperCase()] || '';
      return `<li class="mono lv-${carry || 'none'}"><span class="lg-t">${escapeHtml(m[1])}</span>`
        + `<span class="lg-lv">${escapeHtml(m[2])}</span>${escapeHtml(m[4])}</li>`;
    }
    if (/^Traceback|^\s+File "|^\w*(Error|Exception)\b/.test(text)) carry = 'error';
    const cls = carry === 'error' || carry === 'warn' ? carry : 'none';
    return `<li class="mono lv-${cls}">${escapeHtml(text)}</li>`;
  }).join('');
}

function workerProcessCard() {
  const p = cache.workerProcess || {};
  const d = cache.workerDaemon || {};
  const pids = p.pids || [];
  const count = p.count != null ? Number(p.count) : (pids.length || (p.running ? 1 : 0));
  const msg = p.last_action || '';

  const status = workerProcessStatusHtml();

  // 守护那块：**exposed 为真就要红着说**。守护配置里 enabled=true 但线程没在跑，
  // 或连续失败已放弃——这三种情况下"有守护"这件事都不成立，
  // 界面若只显示 enabled 就是在骗人。
  const daemonOn = !!d.enabled;
  const exposed = d.exposed !== false;
  const daemonKnown = typeof d.enabled === 'boolean';
  const daemonInterval = Number(d.check_interval_seconds || 15);
  const daemonChip = !daemonKnown
    ? '<span class="chip gray">守护状态未取得</span>'
    : !daemonOn
      ? `<span class="chip gray" title="开启后每 ${daemonInterval} 秒巡检一次">已暂停 · ${daemonInterval}s 巡检</span>`
      : (exposed
        ? '<span class="chip red">守护失效</span>'
        : `<span class="chip green" title="每 ${daemonInterval} 秒巡检一次">守护中 · 已补 ${Number(d.recovered || 0)} 次</span>`);

  const daemonLine = !daemonKnown ? '未取得守护状态，请刷新确认。' : !daemonOn
    ? ''
    : (exposed
      ? `<b>守护配着但没在守</b>${d.thread_alive ? '' : '（看门狗线程没在跑）'}`
        + (d.failures >= 3 ? `；连续 ${d.failures} 次没起来，已停止重试，等人工处理`
                           : '') + '。'
      : `守护中 · 每 ${d.check_interval_seconds || 15} 秒检查一次；在线 ${d.online} 不足 ${d.desired} 自动补起（累计已补 ${d.recovered || 0} 次）。`);

  const actions = (d.actions || []).slice(0, 4).map((a) =>
    `<li class="hint"><code>${escapeHtml(a.at)}</code> ${escapeHtml(a.action)}：${escapeHtml(a.detail)}</li>`
  ).join('');

  // 接口和页面均按时间正序，保留 traceback 的上下文。
  if (logFollowing || !logSnapshot) logSnapshot = { ...p, log_tail: [...(p.log_tail || [])] };
  const log = logFollowing ? p : logSnapshot;
  const logLines = (log.log_tail || []).slice(-logLimit);
  const logSize = Number(log.log_size || 0);
  const logPath = String(p.log_path || 'data/logs/worker.log')
    .replace(/^.*?(?=(?:app\/backend\/)?(?:data\/)?logs\/)/, '') || 'data/logs/worker.log';
  const logMeta = !logFetched ? '展开以读取' : `${logSize < 1024 ? `${logSize} 字节` : `${(logSize / 1024).toFixed(1)} KB`}`
    + `${log.log_mtime ? ` · 最后写入 ${escapeHtml(log.log_mtime)}` : ''}`;
  const logEmptyReason = {
    missing: !logFetched ? '展开后读取日志。' : '日志文件尚不存在；不能据此判断 Worker 是否曾经启动。',
    empty: '日志文件可读，当前没有非空日志行。',
    unreadable: '日志读取失败，请检查运行目录权限；保留进程状态供排查。',
  }[log.log_status] || '本次没有取得日志内容。';

  return `
  <div class="card mb-card">
    <div class="card-head">
      <div style="display:flex;align-items:center">
        <h2 id="mc-process-card" tabindex="-1">Worker 进程</h2>
        ${staleLabel('进程 / 日志')}
        <span class="help-tip">
          <span class="help-icon">?</span>
          <span class="tip-pop tip-right">
            <span class="tip-title">Worker 进程与守护规范</span>
            <div class="tip-desc">启停后台执行作业的 worker 进程；与 <code>./run.sh worker</code> 是同一个入口。</div>
            <ul class="tip-list">
              <li><b>进程独立性</b>：worker 为独立会话，API 重启不会带走运行中的进程；停止可能中断当前作业，需人工检查后重试。</li>
              <li><b>看门狗守护</b>：守护线程住在 API 进程里（API 挂了守护也会停），开启后 worker 崩溃将自动拉起补齐。</li>
              <li><b>外部进程</b>：命令行手动拉起的 worker 不授予网页停止权限，请在原终端关闭。</li>
            </ul>
          </span>
        </span>
      </div>
      <span class="right" id="mc-wp-status">${status}</span>
    </div>
    <div class="card-body">
      <div class="wp-toolbar">
        <div class="wp-inline wp-ctrl-group">
          <span class="wp-seg">
            <label for="mc-wp-count" class="wp-bar-label">新增</label>
            <input class="inp" type="number" id="mc-wp-count" min="1" max="8" value="1">
            <button class="btn primary" id="mc-wp-start">启动</button>
          </span>
          <button class="btn danger" id="mc-wp-stop">全部停止</button>
          <button class="btn" id="mc-wp-restart" ${count ? '' : 'disabled'} title="先停止全部受管进程，再按当前数量重新拉起">重启全部</button>
          ${p.stale_pids?.length ? '<button class="btn" id="mc-wp-clean">清理残留登记</button>' : ''}
          <span class="wp-cap" title="当前受管 ${count} / 上限 8">
            <span class="wp-slots${count >= 8 ? ' full' : ''}" aria-hidden="true">${Array.from({ length: 8 }, (_, i) => `<i${i < count ? ' class="on"' : ''}></i>`).join('')}</span>
            <span class="hint">当前 ${count} / 上限 8</span>
          </span>
        </div>

        <div class="wp-inline wp-daemon-group">
          <span class="wp-seg">
            <span class="wp-bar-label">守护</span>
            ${staleLabel('守护')}
            <label class="switch-ui switch-green" title="worker 掉了自动补起">
              <input type="checkbox" id="mc-daemon-on" ${daemonOn ? 'checked' : ''}>
              <span class="switch-slider"></span>
            </label>
          </span>
          <span class="wp-seg">
            <span class="hint">补至</span>
            <input class="inp" type="number" id="mc-daemon-desired" min="0" max="8"
                   value="${d.desired != null ? d.desired : 1}" aria-label="期望在线个数">
            <span class="hint">个</span>
            <button type="button" class="btn xs" id="mc-daemon-sync" title="将期望在线数同步为当前受管数量并保存">对齐当前</button>
          </span>
          ${daemonChip}
        </div>
      </div>
      <div class="wp-feedback">
        <span class="hint" id="mc-wp-msg">${hintOf('mc-wp-msg') || escapeHtml(msg)}</span>
        <span class="hint" id="mc-daemon-msg">${hintOf('mc-daemon-msg') || ''}</span>
      </div>
      ${daemonLine ? `<p class="note flush wp-daemon-note">${daemonLine}</p>` : ''}
      ${actions ? `<ul class="wp-actions hint">${actions}</ul>` : ''}

      <details class="wp-logbox" id="mc-log-panel">
        <summary id="mc-log-summary">
          <span>Worker 日志</span>
          <span class="hint wp-log">${logFollowing ? '' : '已暂停跟随 · '}${logMeta} · <code>${escapeHtml(logPath)}</code></span>
        </summary>
        <div class="wp-sub-head">
          <button class="btn xs" id="mc-log-follow">${logFollowing ? '暂停跟随' : '继续跟随'}</button>
          <button class="btn xs" id="mc-log-more">${logLimit === 12 ? '显示更多' : '收起'}</button>
          <button class="btn xs" id="mc-log-copy" ${logLines.length ? '' : 'disabled'}>复制当前日志</button>
        </div>
        ${logLines.length
          ? `<ol class="wp-loglist">${logListHtml(logLines)}</ol>`
          : `<p class="hint wp-logempty">${escapeHtml(logEmptyReason)}</p>`}
      </details>
    </div>
  </div>`;
}

async function refreshWorkerProcess(root, action, payload) {
  if (operationBusy || (!overviewReady && action !== 'start')) return;
  operationBusy = true;
  mutationVersion += 1;
  syncOperationButtons(root);
  let pauseDaemon = true;
  const restartCount = Math.max(1, Math.min(8, Number(cache.workerProcess?.count) || 1));
  try {
    if (action === 'restart') {
      const status = await api.workerStatus();
      const jobs = status.running_jobs || [];
      const affected = jobs.length
        ? `当前运行作业（可能受影响）：${jobs.map((j) => `#${Number(j.id || j.job_id)}`).join('、')}。`
        : '当前未观测到运行作业，但确认期间仍可能有新作业被领取。';
      if (!(await confirmDanger({ title: `重启全部受管 Worker（共 ${restartCount} 个）`,
        body: `${escapeHtml(affected)}<br>将先停止当前受管进程，再按原数量重新拉起；停止可能中断作业，需要检查后人工重试。`,
        okText: '确认重启' }))) return;
    } else if (action !== 'start' && action !== 'clean') {
      // 确认前读取最新作业，读取失败则取消，避免用旧快照承诺没有影响。
      const status = await api.workerStatus();
      const jobs = status.running_jobs || [];
      const affected = jobs.length
        ? `当前运行作业（可能受影响）：${jobs.map((j) => `#${Number(j.id || j.job_id)}`).join('、')}。`
        : '当前未观测到运行作业，但确认期间仍可能有新作业被领取。';
      if (!(await confirmDanger({ title: action === 'stopOne' ? `停止 Worker ${payload.pid}` : '停止全部受管 Worker',
        body: `${escapeHtml(affected)}<br>停止可能中断作业，需要检查后人工重试。
          <label class="mc-stop-choice"><input type="radio" name="mc-stop-mode" value="pause" checked> 暂停全局守护后停止</label>
          <label class="mc-stop-choice"><input type="radio" name="mc-stop-mode" value="once"> 仅停止本次，守护可能再次补起</label>`,
        okText: '确认停止' }))) return;
      pauseDaemon = document.querySelector('input[name="mc-stop-mode"]:checked')?.value !== 'once';

    }
    setMsg(root, 'mc-wp-msg', action === 'start'
      ? '正在启动（逐个等它写第一份心跳）…'
      : action === 'restart'
        ? `正在重启 ${restartCount} 个受管 Worker…`
        : action === 'clean' ? '正在复查并清理残留登记…' : '正在停止（可能中断当前作业）…');
    let out;
    if (action === 'start') {
      const raw = payload?.count ?? root.querySelector('#mc-wp-count')?.value;
      const count = Math.max(1, Math.min(8, Number(raw) || 1));
      out = await api.startWorkers({ count });
    } else if (action === 'restart') {
      const stopped = await api.stopWorker(false);
      out = stopped?.ok === false ? stopped : await api.startWorkers({ count: restartCount });
    } else if (action === 'clean') {
      out = await api.cleanStaleWorkers();
    } else if (action === 'stopOne') {
      out = await api.stopOneWorker(payload.pid, pauseDaemon);
    } else {
      out = await api.stopWorker(pauseDaemon);
    }
    cache.workerProcess = out;
    setMsg(root, 'mc-wp-msg', escapeHtml(out.last_action || (
      action === 'start' ? '启动请求已完成。'
      : action === 'restart' ? `已重启 ${restartCount} 个受管 Worker。`
      : action === 'clean' ? '残留登记清理已完成。' : '停止请求已完成。')));
    if (action !== 'start' && action !== 'clean' && action !== 'restart') {
      try { cache.workerDaemon = await api.workerDaemon(); }
      catch { monitorFreshness.error = '守护状态读取失败，请刷新确认'; }
    }
    repaintLive(root);
  } catch (err) {
    setMsg(root, 'mc-wp-msg', escapeHtml(String(err?.message || err || '操作没成功。')));
    // 部分启动也可能报错；后续完整刷新重新确认实际进程，不猜测回滚。
    monitorFreshness.error = '操作未确认，请刷新查看实际状态';
  } finally {
    operationBusy = false;
    mutationVersion += 1;
    syncOperationButtons(root);
    paintFreshness(root);
  }
}

async function saveDaemon(root) {
  clearTimeout(daemonDebounceTimer);
  daemonDebounceTimer = null;
  if (operationBusy) return;
  operationBusy = true;
  mutationVersion += 1;
  syncOperationButtons(root);
  const submitted = { ...(drafts.get(root) || {}) };
  const enabled = !!root.querySelector('#mc-daemon-on')?.checked;
  const desired = Math.max(0, Math.min(8, Number(
    root.querySelector('#mc-daemon-desired')?.value) || 0));
  setMsg(root, 'mc-daemon-msg', '正在保存…');
  try {
    cache.workerDaemon = await api.setWorkerDaemon({ enabled, desired });
    clearSubmittedDrafts(root, Object.fromEntries(Object.entries(submitted).filter(([id]) => id.startsWith('mc-daemon-'))));
    setMsg(root, 'mc-daemon-msg', '守护设置已保存。');
    repaintLive(root);
  } catch (err) {
    setMsg(root, 'mc-daemon-msg', escapeHtml(String(err?.message || err || '保存失败。')));
  } finally {
    operationBusy = false;
    mutationVersion += 1;
    syncOperationButtons(root);
  }
}

function paintFreshness(root) {
  const el = root.querySelector('#mc-freshness');
  if (!el) return;
  const next = freshnessHtml();
  if (el.dataset.rendered !== next) {
    el.outerHTML = next;
    root.querySelector('#mc-freshness').dataset.rendered = next;
  }
}

/**
 * 只在**状态翻转**时播报，不在每一拍播报。
 *
 * 这一行现在是正常态静默、异常态才有 `role="status"`。但"静默→异常"那一下
 * 如果只靠节点的出现来触发，读屏未必会念（节点是整块换进去的，
 * 有些读屏只认已存在活区里的文本变化）。所以显式用 `announce()` 说一次，
 * 走的是全站同一套 aria-live 通道（util.js:756）。
 *
 * **必须去重**：tick 每 5 秒一次。不去重就是把刚治好的"每 5 秒念一遍"
 * 原样搬到另一个通道上——异常持续期间会一直重复。所以记住上一次报过的错误文本，
 * 只在它变了（新错误、或从异常恢复正常）时说话。
 */
let lastAnnouncedFreshness = '';
function announceFreshness() {
  const key = monitorFreshness.error;
  if (key === lastAnnouncedFreshness) return;
  lastAnnouncedFreshness = key;
  announce(key ? `运行监控读取异常：${key}，已保留上次数据。`
               : '运行监控连接已恢复。');
}

const monitorMarkup = new WeakMap();

function refreshMarkup(host, html) {
  if (!host || monitorMarkup.get(host) === html) return false;
  const focused = host.contains(document.activeElement) ? document.activeElement : null;
  const state = [...host.querySelectorAll('details[id]')].map((el) => [el.id, el.open]);
  const next = document.createElement('template');
  next.innerHTML = html;
  for (const [id, open] of state) {
    const el = next.content.getElementById(id);
    if (el) el.open = open;
  }
  // 轮询保留内容未变的整块卡片和仍然存在的键盘目标。
  const stableAttrs = ['id', 'data-goto-job', 'data-monitor-target', 'data-job-status', 'data-stop-pid', 'data-goto-gen'];
  const attr = focused && stableAttrs.find((key) => focused.hasAttribute(key));
  const controls = attr ? [...next.content.querySelectorAll(`[${attr}]`)] : [];
  const replacement = controls.find((el) => el.getAttribute(attr) === focused.getAttribute(attr)
    && el.className === focused.className);
  const existing = [...host.children];
  const incoming = [...next.content.children];
  if (existing.length === incoming.length) {
    incoming.forEach((el, i) => {
      if (existing[i].outerHTML !== el.outerHTML) existing[i].replaceWith(el);
    });
  } else {
    host.replaceChildren(next.content);
  }
  monitorMarkup.set(host, html);
  if (focused && !focused.isConnected) {
    const target = replacement?.isConnected ? replacement : null;
    target?.focus({ preventScroll: true });
  }
  return true;
}

function repaintLive(root) {
  if (refreshMarkup(root.querySelector('#mc-live'), liveCards())) bindWorkerHistory(root);
  const ops = root.querySelector('#mc-ops');
  const logScroll = ops?.querySelector('.wp-loglist')?.scrollTop || 0;
  // 运维表单保留既有草稿模型。避免给未替换的控件再次绑定监听。
  const html = ops && opsCards();
  if (ops && monitorMarkup.get(ops) !== html) {
    const focusedId = ops.contains(document.activeElement) ? document.activeElement.id : null;
    const details = [...ops.querySelectorAll('details[id]')].map((el) => [el.id, el.open]);
    ops.innerHTML = html;
    monitorMarkup.set(ops, html);
    details.forEach(([id, open]) => { const el = document.getElementById(id); if (el) el.open = open; });
    bindWorkerProcess(root);
    bindConcurrency(root);
    restoreDrafts(root);
    syncOperationButtons(root);
    const logList = root.querySelector('.wp-loglist');
    if (logList) logList.scrollTop = logFollowing ? logList.scrollHeight : logScroll;
    if (focusedId) document.getElementById(focusedId)?.focus({ preventScroll: true });
  }
}

function bindWorkerProcess(root) {
  const panel = root.querySelector('#mc-log-panel');
  panel?.addEventListener('toggle', () => {
    if (panel.open && panel.isConnected && !logFetched) root.__monitorTick?.();
  });
  root.querySelector('#mc-wp-clean')?.addEventListener('click', () => refreshWorkerProcess(root, 'clean'));
  root.querySelector('#mc-log-follow')?.addEventListener('click', () => {
    logFollowing = !logFollowing;
    repaintLive(root);
  });
  root.querySelector('#mc-log-more')?.addEventListener('click', () => {
    logLimit = logLimit === 12 ? 100 : 12;
    repaintLive(root);
  });
  root.querySelector('#mc-log-copy')?.addEventListener('click', async () => {
    const lines = [...root.querySelectorAll('.wp-loglist li')].map((el) => el.textContent);
    try {
      await navigator.clipboard.writeText(lines.join('\n'));
      alertNotify('当前显示日志已复制。', 'ok');
    } catch { alertNotify('复制失败，请在日志框中选择文本复制。', 'err'); }
  });
  root.querySelector('#mc-wp-start')?.addEventListener('click', () => {
    refreshWorkerProcess(root, 'start');
  });
  root.querySelector('#mc-wp-stop')?.addEventListener('click', () => {
    refreshWorkerProcess(root, 'stop');
  });
  root.querySelector('#mc-wp-restart')?.addEventListener('click', () => {
    refreshWorkerProcess(root, 'restart');
  });
  root.querySelector('#mc-daemon-on')?.addEventListener('change', () => {
    saveDaemon(root);
  });
  const desiredEl = root.querySelector('#mc-daemon-desired');
  desiredEl?.addEventListener('input', () => {
    clearTimeout(daemonDebounceTimer);
    daemonDebounceTimer = null;
    const raw = String(desiredEl.value || '').trim();
    if (raw === '') return;
    const n = Number(raw);
    if (!Number.isFinite(n) || n < 0 || n > 8) return;
    daemonDebounceTimer = setTimeout(() => {
      daemonDebounceTimer = null;
      if (root.isConnected) saveDaemon(root);
    }, 600);
  });
  desiredEl?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      saveDaemon(root);
    }
  });
  desiredEl?.addEventListener('blur', () => {
    const raw = String(desiredEl.value || '').trim();
    if (raw === '') {
      clearTimeout(daemonDebounceTimer);
      daemonDebounceTimer = null;
      desiredEl.value = String(cache.workerDaemon?.desired ?? 1);
      return;
    }
    if (daemonDebounceTimer) saveDaemon(root);
  });
  root.querySelector('#mc-daemon-sync')?.addEventListener('click', () => {
    if (!desiredEl) return;
    const count = Math.max(0, Math.min(8, Number(cache.workerProcess?.count) || 0));
    desiredEl.value = String(count);
    desiredEl.dispatchEvent(new Event('input', { bubbles: true }));
    saveDaemon(root);
  });
}


/* ================= 并发设置（0047） ================= */
/**
 * 这一段要解决的是一个具体矛盾：并发上限**本来就是安全的**（默认 1，见
 * `worker.py` 的模块头），但它只能从命令行改，于是想调的人不知道调多少才不炸；
 * 而想改到网页上，就会让这个安全阈值变成随手能点掉的东西。
 *
 * 平衡点：**允许改，但把代价摆在旁边**。所以这张卡不是三个输入框，
 * 而是"实测每类占多少 → 这套上限要多少 → 本机还剩多少"一条算式，
 * 结论为 `over` 时按钮变成危险色、并要求勾一个确认框。
 *
 * 算式归后端（`concurrency_plan`），这里只显示——两处各算一次迟早分家。
 */
const VERDICT_UI = {
  safe:    { chip: 'green', label: '放得下' },
  tight:   { chip: 'amber', label: '余量偏薄' },
  over:    { chip: 'red',   label: '会溢出' },
  unknown: { chip: 'gray',  label: '估不出' },
};

function concurrencyCard() {
  const c = (cache.worker || {}).concurrency || {};
  const a = c.assessment || {};
  const ui = VERDICT_UI[a.verdict] || VERDICT_UI.unknown;
  const classes = ['text', 'image', 'video', 'audio', 'compose'];
  const labels = { text: '文本', image: '图像', video: '视频', audio: '音频', compose: '成片' };
  const effective = c.effective || {};
  const per = effective.per || {};

  const num = (id, value) => `<input class="inp" type="number" min="0" max="64"
      id="${id}" value="${value == null ? '' : escapeHtml(String(value))}"
      ${value == null ? 'placeholder="默认"' : ''}>`;

  const rows = classes.map((cls) => {
    const peak = (a.peak_by_class_mb || {})[cls];
    const slot = per[cls];
    return `<tr>
      <td>${labels[cls] || cls}</td>
      <td class="hint">${peak == null
        ? '<span class="t-dim">没有样本</span>'
        : `实测峰值 ${peak} MB`}</td>
      <td>${num(`mc-conc-per-${cls}`, slot)}</td>
    </tr>`;
  }).join('');

  const detail = (a.detail || []).map((d) =>
    `${labels[d.class] || d.class} ${d.unit_peak_mb} MB × ${d.slots} = ${d.sum_mb} MB`).join('；');

  const basis = a.verdict === 'unknown'
    ? `<p class="note flush" style="margin-top:10px">${escapeHtml(a.sentence || '')}</p>`
    : `<p class="note flush" style="margin-top:10px">
         <b>${escapeHtml(ui.label)}</b>：${escapeHtml(a.sentence || '')}
       </p>`;

  return `
  <div class="card mb-card">
    <div class="card-head">
      <div style="display:flex;align-items:center">
        <h2 id="mc-concurrency-card" tabindex="-1">并发设置</h2>
        <span class="help-tip">
          <span class="help-icon">?</span>
          <span class="tip-pop tip-right">
            <span class="tip-title">Worker 并发控制规则</span>
            <div class="tip-desc">控制同时最多运行的作业条数。改完 worker 下一轮自动按新值领活。</div>
            <ul class="tip-list">
              <li><b>免重启生效</b>：保存后 worker 下一次领活时立即生效，无需重启进程。</li>
              <li><b>资源防爆</b>：依据本机实测峰值内存（乘安全余量系数 ${escapeHtml(String(a.safety_ratio ?? '—'))}）测算，当前明细：${escapeHtml(detail || '暂无样本')}。</li>
              <li><b>优先级</b>：留空＝跟随进程默认（<code>./run.sh worker --max-global N</code> 或 <code>FASTVIDEO_WORKER_MAX_GLOBAL</code>）；命令行参数优先于网页配置。</li>
            </ul>
          </span>
        </span>
      </div>
      <span class="right">
        <span class="chip ${ui.chip}">${escapeHtml(ui.label)}</span>
      </span>
    </div>
    <div class="card-body">
      <details id="mc-reported-limits" class="mc-monitor-detail">
        <summary id="mc-limits-summary">在线 Worker 上报的并发上限</summary>
        <ul>${((cache.worker || {}).workers || []).filter((w) => w.online).map((w) =>
          `<li><code>${escapeHtml(w.worker_id || '—')}</code> · 全局 ${escapeHtml(String(w.limits?.global ?? '未上报'))}
            ${Object.entries(w.limits?.per || {}).map(([key, value]) =>
              escapeHtml(` · ${RES_LABEL[key] || key} ${value}`)).join('')}</li>`).join('') || '<li>暂无在线 Worker</li>'}</ul>
      </details>
      <div class="table-wrap mc-conc-wrap"><table class="mc-conc-table">
        <thead><tr><th>资源类</th><th>本机实测占用</th><th>上限</th></tr></thead>
        <tbody>
          <tr><td>全局</td><td class="hint">这条作业总数上限</td>
              <td>${num('mc-conc-global', c.global_max)}</td></tr>
          ${rows}
        </tbody>
      </table></div>
      <div class="mc-conc-foot">
        <div class="mc-conc-notes">
          ${basis}
          <p class="hint" style="margin:4px 0 0">留空即跟随进程默认（命令行参数优先）。</p>
        </div>
        <div class="row-actions mc-conc-actions">
          <span class="hint" id="mc-conc-msg">${hintOf('mc-conc-msg')}</span>
          <button class="btn" id="mc-conc-save">保存并发设置</button>
        </div>
      </div>
    </div>
  </div>`;
}

function readConcurrencyForm(root) {
  const read = (id) => {
    const el = root.querySelector(`#${id}`);
    if (!el) return null;
    const raw = String(el.value || '').trim();
    if (raw === '') return null;
    const n = Number(raw);
    return Number.isFinite(n) ? Math.max(0, Math.trunc(n)) : null;
  };
  const per = {};
  for (const cls of ['text', 'image', 'video', 'audio', 'compose']) {
    const v = read(`mc-conc-per-${cls}`);
    if (v != null) per[cls] = v;
  }
  return { global_max: read('mc-conc-global'), per };
}

/** **每次写提示前重新查一次元素，不要缓存引用**（2026-11-10 实测踩到）。
 *
 *  根因：`app:refresh`（worker 状态每 20 秒一轮、启停/保存之后也会发）
 *  会让整个视图重绘，`#mc-conc-msg` 这个 <span> 被换成一个新节点。
 *  而 `await` 之前抓的旧引用此时已经脱离 DOM——往里写字符串，写的是一个
 *  游离节点，屏幕上什么都不会变。
 *  实测现象：点击保存后提示区始终为空、按钮又恢复成可点，看起来像"没反应"。
 *  两次查询之间几乎不可能再插入一次重绘（这一句是同步执行的），所以就地查最稳。
 */
/** 读回上一次写的提示（重绘后仍要显示）。空字符串＝没有提示。
 *  `cache.hints` 只存这几个短句，不做清空——它们本来就是"最近一次操作的结果"，
 *  下次操作会覆盖掉。 */
function hintOf(id) {
  const v = (cache.hints || {})[id];
  return v ? String(v) : '';
}


function setMsg(root, id, html) {
  // 同时写进 cache：`app:refresh`（worker 状态 20 秒一轮）会重绘整个 live 区，
  // 只写 DOM 的话那句话会在几百毫秒内被冲掉——实测就是这样（写进去了、然后就没了）。
  // 重绘时由卡片自己从 cache 读回来（见 workerProcessCard/concurrencyCard）。
  cache.hints = { ...(cache.hints || {}), [id]: html };
  const el = root.querySelector(`#${id}`);
  if (el) el.innerHTML = html;
  return el;
}

async function saveConcurrency(root, payload, acknowledgeRisk) {
  if (operationBusy) return;
  operationBusy = true;
  mutationVersion += 1;
  syncOperationButtons(root);
  const submitted = { ...(drafts.get(root) || {}) };
  const btn = root.querySelector('#mc-conc-save');
  if (btn) btn.disabled = true;
  try {
    const body = { ...payload, acknowledge_risk: !!acknowledgeRisk };
    const out = await api.setWorkerConcurrency(body);
    if (out && cache.worker) cache.worker.concurrency = out;
    clearSubmittedDrafts(root, Object.fromEntries(Object.entries(submitted).filter(([id]) => id.startsWith('mc-conc-'))));
    repaintLive(root);
    // await 之后必须重新查（见 setMsg 的注释）
    setMsg(root, 'mc-conc-msg', out?.assessment?.verdict === 'safe'
      ? '已保存；worker 下一轮按新值领活。'
      : `已保存（${out?.assessment?.sentence || ''}）`);
  } catch (err) {
    const text = String(err?.message || err || '');
    // 后端把"会溢出，要确认"做成了 400 + 那句话。这里**不猜**该不该继续：
    // 判断这句话是不是那道闸，用 confirm 让人再决定一次，而不是静默重试。
    // **危险档的二次确认不用 `window.confirm`**（2026-11-10 自查）。
    // 这里改成"把后端原话摆出来 + 给一个明确的再次保存按钮"：
    // 原生弹窗风格与全站不一致（全仓用量 0），而且它会把那段带数字的说明
    // 挤成一行小字。现在改成在按钮旁边就地显示，人能读完整句再决定。
    if (/内存溢出风险/.test(text) && !acknowledgeRisk) {
      // **不在这里给按钮绑事件**（2026-11-10 实测踩到）：这行 innerHTML 之后
      // 可能紧跟一次 `app:refresh` 重绘，刚绑的监听会跟着旧节点一起被丢掉，
      // 于是"点了没反应、后端也没存"。改成把待确认的 payload 挂到 cache，
      // 由 `bindConcurrency` 里那条**委托监听**统一处理（它绑在容器上，
      // 重绘不影响）。
      cache.pendingConcurrency = { payload, text };
      setMsg(root, 'mc-conc-msg',
        `${escapeHtml(text)} <button class="btn xs danger" `
        + `id="mc-conc-force">我知道风险，仍要保存</button>`);
      return;
    }
    setMsg(root, 'mc-conc-msg', escapeHtml(text || '没保存成功。'));
  } finally {
    // 按钮同理：重绘之后旧引用已脱离 DOM，`btn.disabled` 改的是死对象。
    // 重新查一次，否则失败后按钮会一直停用（除非正好又重绘过）。
    const live = root.querySelector('#mc-conc-save');
    if (live) live.disabled = false;
    operationBusy = false;
    mutationVersion += 1;
    syncOperationButtons(root);
  }
}

function bindConcurrency(root) {
  root.querySelector('#mc-conc-save')?.addEventListener('click', () => {
    saveConcurrency(root, readConcurrencyForm(root), false);
  });
  // **委托**处理"我知道风险，仍要保存"：这个按钮是 setMsg 动态生成的，
  // 而且它生成后随时可能被 `app:refresh` 重绘替换掉。直接绑在按钮上会随旧
  // 节点一起消失（实测：点了没反应、后端 global_max 仍是 None）。
  // 绑在不会重建的容器上（`#mc-live` 的外层 root）就没有这个问题。
  // 先解掉上一次绑的（repaintLive 会重复调本函数），避免同一次点击触发多遍。
  if (root.__concDelegated) return;
  root.__concDelegated = true;
  root.addEventListener('click', (e) => {
    if (!e.target.closest('#mc-conc-force')) return;
    const pending = cache.pendingConcurrency;
    if (!pending) return;
    cache.pendingConcurrency = null;
    saveConcurrency(root, pending.payload, true);
  });
}

function syncValidationSelectOptions(root) {
  for (const [id, field, emptyLabel] of [
    ['mc-val-provider', 'provider', '全部供应商'],
    ['mc-val-model', 'model', '全部模型'],
  ]) {
    const sel = root.querySelector(`#${id}`);
    if (!sel) continue;
    const current = sel.value || '';
    const provider = root.querySelector('#mc-val-provider')?.value || '';
    sel.innerHTML = validationSelectOptions(field, emptyLabel, current, provider);
    const state = optionStates.get(optionKey(field, provider));
    const more = root.querySelector(`#mc-val-more-${field}`);
    if (more) {
      more.hidden = !state?.next_after && !state?.error;
      more.disabled = !!state?.pending;
      more.textContent = state?.error ? '重试' : field === 'provider' ? '更多供应商' : '更多模型';
    }
    sel.value = current;
  }
  const provider = root.querySelector('#mc-val-provider')?.value || '';
  const active = [optionStates.get(optionKey('provider')), optionStates.get(optionKey('model', provider))];
  const message = root.querySelector('#mc-val-options-status');
  if (message) message.textContent = active.some((state) => state?.error) ? '历史选项读取失败，仍可使用已加载选项。'
    : active.some((state) => state?.pending) ? '正在读取历史选项…' : '';
}

function renderValidationTable(root) {
  syncValidationSelectOptions(root);
  const tbody = root.querySelector('#mc-val-rows');
  if (tbody) {
    const rows = cache.validations?.items || [];
    tbody.innerHTML = rows.map(valRow).join('')
      || `<tr><td colspan="6" class="hint">${(valFilterState || valFilterKind || Object.values(valExtra).some(Boolean))
        ? '当前筛选条件下没有验证记录' : '还没有验证记录'}</td></tr>`;
  }
  const pager = root.querySelector('.mc-val-pager');
  if (pager) pager.innerHTML = validationPager();
  const applied = root.querySelector('.mc-val-applied');
  if (applied) {
    const { provider, model, dateFrom, dateTo } = appliedFilters;
    applied.textContent = [provider && `供应商 ${provider}`, model && `模型 ${model}`,
      (dateFrom || dateTo) && `${dateFrom || '不限'} 至 ${dateTo || '不限'}`].filter(Boolean).join(' · ');
  }
}

function restoreFilterControls(root, filters) {
  root.querySelector('#mc-val-filter-kind').value = filters.kind || '';
  root.querySelector('#mc-val-filter-state').value = filters.onlyBad ? 'bad' : '';
  for (const [key, id] of Object.entries({ provider: 'provider', model: 'model', dateFrom: 'from', dateTo: 'to' })) {
    const input = root.querySelector(`#mc-val-${id}`);
    const value = filters[key] || '';
    if (input.tagName === 'SELECT' && value && ![...input.options].some((option) => option.value === value)) {
      input.add(new Option(value, value));
    }
    input.value = value;
  }
}

async function reloadValidations(root) {
  const wanted = ++valSeq;
  const requested = { ...valFilterParams() };
  const tbody = root.querySelector('#mc-val-rows');
  const alive = () => tbody?.isConnected && wanted === valSeq && getSub('models') === 3;
  if (tbody) tbody.innerHTML = '<tr><td colspan="6" class="hint">正在筛选……</td></tr>';
  try {
    const out = await api.providerValidations(HISTORY_PAGE, null, requested);
    if (!alive()) return;
    appliedFilters = requested;
    cache.validations = out;
    cache.validationPage = { before: null, pages: 0, exhausted: !out.items.length };
    renderValidationTable(root);
  } catch (err) {
    if (!alive()) return;
    valFilterKind = appliedFilters.kind || '';
    valFilterState = appliedFilters.onlyBad ? 'bad' : '';
    valExtra = { provider: appliedFilters.provider || '', model: appliedFilters.model || '',
      dateFrom: appliedFilters.dateFrom || '', dateTo: appliedFilters.dateTo || '' };
    restoreFilterControls(root, appliedFilters);
    alertNotify(`验证记录筛选失败：${err.message}`, 'err');
    renderValidationTable(root);
  }
}

export function mountMonitor(root) {
  if (!root.__monitorDraftBound) {
    root.__monitorDraftBound = true;
    const remember = (e) => {
      const el = e.target;
      if (!el.closest('#mc-ops') || !el.matches('input[id], select[id]')) return;
      const current = drafts.get(root) || {};
      current[el.id] = { value: el.type === 'checkbox' ? el.checked : el.value };
      drafts.set(root, current);
    };
    root.addEventListener('input', remember);
    root.addEventListener('change', remember);
  }
  restoreDrafts(root);
  syncOperationButtons(root);
  bindWorkerHistory(root);
  bindWorkerProcess(root);
  bindConcurrency(root);

  root.querySelector('#mc-val-filter-state')?.addEventListener('change', (e) => {
    valFilterState = e.target.value || '';
    applyFilters();
  });
  root.querySelector('#mc-val-filter-kind')?.addEventListener('change', (e) => {
    valFilterKind = e.target.value || '';
    applyFilters();
  });

  const applyFilters = () => {
    valExtra = { provider: root.querySelector('#mc-val-provider').value,
      model: root.querySelector('#mc-val-model').value,
      dateFrom: root.querySelector('#mc-val-from').value,
      dateTo: root.querySelector('#mc-val-to').value };
    reloadValidations(root);
  };
  root.querySelector('#mc-val-apply')?.addEventListener('click', applyFilters);
  const extras = root.querySelector('#mc-val-extra');
  extras?.addEventListener('toggle', () => {
    if (!extras.open) return;
    loadValidationOptions(root, 'provider');
    loadValidationOptions(root, 'model');
  });
  root.querySelector('#mc-val-provider')?.addEventListener('change', () => {
    root.querySelector('#mc-val-model').value = '';
    syncValidationSelectOptions(root);
    loadValidationOptions(root, 'model');
  });
  for (const field of ['provider', 'model']) {
    root.querySelector(`#mc-val-more-${field}`)?.addEventListener('click', () => {
      const provider = root.querySelector('#mc-val-provider').value;
      loadValidationOptions(root, field, !!optionStates.get(optionKey(field, provider))?.next_after);
    });
  }
  for (const id of ['provider', 'model', 'from', 'to']) {
    root.querySelector(`#mc-val-${id}`)?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') applyFilters();
    });
  }
  root.querySelector('#mc-val-reset')?.addEventListener('click', () => {
    valFilterKind = ''; valFilterState = '';
    valExtra = { provider: '', model: '', dateFrom: '', dateTo: '' };
    restoreFilterControls(root, valFilterParams());
    if (extras?.open) loadValidationOptions(root, 'model');
    reloadValidations(root);
  });

  if (!root.__monitorNavDelegated) {
    root.__monitorNavDelegated = true;
    root.addEventListener('click', (e) => {
      const target = e.target.closest('[data-monitor-target]');
      if (target) {
        const heading = document.getElementById(target.dataset.monitorTarget);
        heading?.focus();
        heading?.scrollIntoView({ block: 'center' });
        return;
      }
      const status = e.target.closest('[data-job-status]');
      if (status) {
        gotoEntry({ module: 'generate', job_status: status.dataset.jobStatus });
        return;
      }
      const jobBtn = e.target.closest('[data-goto-job]');
      if (jobBtn) {
        gotoEntry({ module: 'generate', job_id: Number(jobBtn.dataset.gotoJob) });
        return;
      }
      if (e.target.closest('[data-quick-start-worker]')) {
        refreshWorkerProcess(root, 'start', { count: 1 });
        return;
      }
      const stopBtn = e.target.closest('[data-stop-pid]');
      if (stopBtn) {
        refreshWorkerProcess(root, 'stopOne', { pid: Number(stopBtn.dataset.stopPid) });
        return;
      }
      if (e.target.closest('[data-goto-gen]')) {
        gotoEntry({ module: 'generate' });
      }
    });
  }

  // 验证历史翻页：再取一页**追加**到后面，而不是重载整页。
  // 重载整页会把上面 worker 表、告警、模型维度一起重画，而人想看的就是这一张表；
  // 走 app:refresh 还会让刚展开的 worker 历史折回去（那是模块变量，会被覆盖）。
  // 追加只动两张东西：表体（追加行）与翻页条（条数与"还有没有更早的"）。
  // 具体的点击处理见下面 root 上的委托与 `loadOlderValidations()`。
  // **翻页按钮必须用委托**（2026-11-10 自查修的真 bug）。
  //
  // 上面那个 `pager.outerHTML = …` 把整条 `<span class="right">` 换成了新节点，
  // 新生成的 `#mc-val-older` 是**另一个元素**，原来绑在旧元素上的监听随它一起
  // 消失。于是第一次翻页成功之后，第二次点「更早的」完全没有反应——
  // 不发请求、不 disabled、控制台也不报错，验证记录永远只能看两页，
  // 而"第 201 条之后读不到"正是这个翻页条存在的理由（见 validationPager 注释）。
  //
  // 与 `bindConcurrency` 同一处理：绑在不会因翻页而重建的容器上，并用
  // `__valOlderDelegated` 挡住重复 mount 叠出的多份监听（root 是持久的
  // `#view-root`，`mountMonitor` 每次进这一页都会再调一遍）。
  // 注意这里**不能用 `return` 早退**：`mountMonitor` 后面还有轮询初始化，
  // 早退会让重进这一页时整块轮询不再挂上（那是更严重的问题）。
  if (!root.__valOlderDelegated) {
    root.__valOlderDelegated = true;
    root.addEventListener('click', async (e) => {
      const btn = e.target.closest('#mc-val-older');
      if (!btn || btn.disabled) return;
      if (!(cache.validations?.items || []).length) return;
      await loadOlderValidations(root, btn);
    });
  }

  // 作用域轮询：完整刷新运行快照，保留操作草稿与验证留痕页码。
  // 锚点取 #mc-live：切走子页时路由替换 #view-root，它 isConnected 变 false，
  // 下一拍自己清掉。mount 先清旧计时器，避免同屏重 mount 叠出多条。
  stopPoll();
  const anchor = root.querySelector('#mc-live');
  const alive = () => anchor.isConnected && (getSub('models') === 3);
  // 一拍做的事抽成函数：计时器与"回前台补一拍"共用同一份判活/写法，
  // 免得两条路径慢慢长歪（2026-11-11 审计 F-04）。
  const generation = pollGeneration;
  let inFlight = false;
  const tick = async () => {
    if (!alive()) { if (generation === pollGeneration) stopPoll(); return; }
    if (inFlight || operationBusy || generation !== pollGeneration) return;
    inFlight = true;
    const version = mutationVersion;
    try {
      const includeLog = !!root.querySelector('#mc-log-panel')?.open;
      const failed = [];
      try {
        const result = await api.workerOverview(includeLog);
        if (!alive() || generation !== pollGeneration || version !== mutationVersion) return;
        overviewRows = result.worker_rows;
        cache.worker = { ...result.status, workers: overviewRows.filter((row) => row.heartbeat_state !== 'missing') };
        overviewReady = !!result.process;
        if (result.process) {
          const { log_tail, log_size, log_mtime, log_status } = cache.workerProcess || {};
          cache.workerProcess = result.log_included ? result.process : {
            ...result.process, log_tail, log_size, log_mtime, log_status,
          };
          if (result.log_included) logFetched = true;
        }
        if (result.daemon) cache.workerDaemon = result.daemon;
        if (result.errors?.includes('process')) failed.push('进程 / 日志');
        if (result.errors?.includes('daemon')) failed.push('守护');
      } catch (err) {
        if (!alive() || generation !== pollGeneration || version !== mutationVersion) return;
        // 旧后端或非运维账号仍可读取公开给登录用户的状态。不开启进程操作。
        if (err.status === 404 || err.status === 403) {
          const status = await api.workerStatus().catch(() => null);
          if (!alive() || generation !== pollGeneration || version !== mutationVersion) return;
          if (status) {
            cache.worker = status;
            overviewRows = [];
          } else failed.push('队列 / 心跳');
          if (err.status === 403) {
            cache.workerProcess = {};
            cache.workerDaemon = {};
          }
        } else failed.push('队列 / 心跳');
        overviewReady = false;
        failed.push('进程 / 日志', '守护');
      }
      monitorFreshness.failed = failed;
      monitorFreshness.error = failed.length ? `${failed.join('、')}读取失败` : '';
      if (!failed.length) monitorFreshness.at = new Date().toLocaleTimeString();
      repaintLive(root);
      paintFreshness(root);
      // 正常拍不发声，只在"刚出问题 / 刚恢复"那一下说一句（见 announceFreshness）。
      announceFreshness();
    } finally { inFlight = false; }
  };
  // `root.__monitorTick` 仍然挂着，但**不再有按钮去点它**（v1.52 删了「立即刷新」）。
  // 留着这个引用是给浏览器验收用的：tools/monitor_interaction_browser_check.py
  // 需要一个"立刻触发一拍"的入口来验单飞、写屏障、切页丢弃这些时序，
  // 原来靠点按钮，现在直接调它——比"为此保留一个没人点的按钮"干净。
  root.__monitorTick = tick;
  tick();
  pollTimer = setInterval(() => {
    if (!alive()) { stopPoll(); return; }
    // 页面不可见时不发：后台标签页没有"让人知道"的对象（与全站轮询同一纪律）。
    if (document.hidden) return;
    tick();
  }, POLL_MS);
  // 只跳过不够：人切回来时看到的是**离开那一刻**的队列，而作业可能早跑完了。
  // 这里补一拍，不额外加轮询。判活与 hidden 两个条件都要——document 的监听
  // 不随视图回收，切走之后这一下照样会派发过来。
  visHandler = () => {
    if (!alive() || document.hidden) return;
    tick();
  };
  document.addEventListener('visibilitychange', visHandler);
  return stopPoll;
}

/** 计时器与 document 监听一起收。只清计时器会留下一个拿着旧 alive 闭包的监听。 */
function stopPoll() {
  pollGeneration += 1;
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (visHandler) { document.removeEventListener('visibilitychange', visHandler); visHandler = null; }
}

/** 取更早一页验证记录并**追加**到表后。委托与首次绑定共用这一段。 */
async function loadOlderValidations(root, btn) {
  const items = cache.validations?.items || [];
  const last = items[items.length - 1];
  if (!last) return;
  btn.disabled = true;
  const wanted = valSeq;
  const tbodyAnchor = root.querySelector('#mc-val-rows');
  try {
    const page = await api.providerValidations(HISTORY_PAGE, last.id, appliedFilters);
    if (wanted !== valSeq || !tbodyAnchor?.isConnected || getSub('models') !== 3) return;
    const fresh = page.items.filter((v) => !items.some((old) => old.id === v.id));
    cache.validations = { items: items.concat(fresh),
                          total: page.total ?? cache.validations.total };
    cache.validationPage = { before: last.id, pages: (cache.validationPage?.pages || 0) + 1, exhausted: !page.items.length };
    const tbody = root.querySelector('#mc-val-rows');
    if (!tbody) { window.dispatchEvent(new CustomEvent('app:refresh')); return; }
    renderValidationTable(root);
  } catch (err) {
    btn.disabled = false;
    alertNotify(`更早的验证记录读不到：${err.message}`, 'err');
  }
}
