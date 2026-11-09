/**
 * 模型中心 · 3 运行监控（队列、worker、成本、最近失败）。
 *
 * 本文件由 models.js（2277 行）按子页拆出。共享底座见 ./shared.js：
 * 缓存、常量、跨子页的纯函数都在那边，这里只管本子页的渲染与交互。
 */
import { api } from '../../api.js';
import { store } from '../../store.js';
import {
  agoText, bindModalDismiss, bindSubChips, confirmDanger, escapeHtml, run, subChipRow,
} from '../../util.js';
import { getSub, setSub } from '../../navstate.js';
import { HEALTH, VAL_BADGE, VAL_KIND_LABEL, alertLabel } from '../../validation.js';
import { REG_SOURCES, unitLabel } from '../../enums.js';
import {
  SUBS, cache, stateChip, healthChip, manifestAxis, adapterOf, manifestProviders,
  sourceLabels, sourceLabel, overviewCard, alertNotify, HISTORY_PAGE,
  showWorkerHistory, toggleWorkerHistory,
} from './shared.js';

/* ================= 工作区 3 · 运行监控 ================= */
/**
 * 这一页只读三样现成的东西：`/api/worker/status`、`/api/models/stats`、
 * `/api/providers/validations`。为什么不去新建一套"模型运行记录"表：
 * Job 才是执行事实的唯一出口（系统设计 §1 不变量 2），
 * 这里再攒一份就必然长出两套状态。
 */

/* 作用域轮询的计时器。与生成中心同一套守卫（generate.js:1879）：
   模块级持有、mount 先清旧、alive() 判锚点还在不在——切走子页时路由替换
   #view-root，锚点 isConnected 变 false，计时器自己清掉，不会叠出第二条。
   为什么不用全站那条 20s 轮询：它只刷顶栏 chip（app.js paintWorker），
   监控页的队列表与告警它一概不动——名为"运行监控"却是切页那一刻的快照。 */
let pollTimer = null;
const POLL_MS = 5000;

/** 验证历史的一条行。首屏渲染与「更早」追加共用这一份——原来两处各抄了一遍
 *  同一段行 HTML，改一处忘另一处，翻到第二页列就错位。 */
function valRow(v) {
  return `<tr>
    <td><span class="chip plain">${escapeHtml(v.kind_label || VAL_KIND_LABEL[v.kind] || v.kind)}</span></td>
    <td>${stateChip(v.state)}</td>
    <td class="hint">${escapeHtml(v.capability)} · ${escapeHtml(v.backend)} · ${escapeHtml(v.provider || '—')}</td>
    <td>${v.job_id ? '#' + v.job_id : '—'}</td>
    <td><span class="t-desc">${escapeHtml(v.detail || '')}</span></td>
    <td class="t-dim">${escapeHtml(String(v.created_at || '').slice(0, 16))}</td>
  </tr>`;
}

/**
 * 验证历史的翻页条。
 *
 * 为什么要它：接口从 0035 起就支持 `limit`，而仓储层把上限夹在 200——
 * 于是"第 201 条之后读不到"这件事在界面上完全不可见，翻到底的人会以为
 * 历史只有这么多条。这一版后端补了 `before_id` 与 `X-Total-Count`，
 * 界面必须把两件事都说出来，否则补了接口也等于没补：
 *   ① 一共多少条（来自响应头），② 还有没有更早的（有就给「更早」按钮）。
 */
export function validationPager() {
  const total = cache.validations?.total || 0;
  const shown = (cache.validations?.items || []).length;
  const page = cache.validationPage || { before: null, pages: 0 };
  // "还有没有更早的"＝已经搬上来的条数还不到总数。不要用 `page.before` 判：
  // 第一页的游标是空的，那样写会让"一共有 500 条、这一页 50 条"的时候不出现翻页按钮。
  const older = shown > 0 && shown < total;
  return `<span class="hint">共 ${total} 条 · 这一页 ${shown} 条`
    + `${page.pages ? ` · 已翻 ${page.pages} 页` : ''}</span> `
    + `${older ? '<button class="btn xs" id="mc-val-older">更早的</button>' : ''}`;
}

/**
 * 队列与 worker ＋ 告警两张卡。轮询只重画这两张（数据来自 worker/status，
 * 是真会随时间变的那部分）；模型维度与验证留痕不跟着每 5 秒重画——
 * 前者只在登记/验证时才变，后者一重画就把人刚翻到的页码 reset 回第一页。
 * 每次调用都现读 cache.worker，所以轮询更新缓存后重画即是新数。
 */
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
  const nodeRow = (n) => `<tr>
      <td class="mono">${escapeHtml(n.worker_id || '—')}</td>
      <td>${n.online ? '<span class="chip green">在线</span>' : '<span class="chip gray">离线</span>'}</td>
      <td>${n.current_job_id ? '#' + n.current_job_id : '<span class="hint">空闲</span>'}</td>
      <td class="hint">${escapeHtml(String(n.heartbeat_at || '').slice(0, 16))}
        ${agoText(n.age_seconds)}</td>
      <td class="hint">${escapeHtml(resChips(n))}</td>
    </tr>`;

  // 一行＝一个进程，这是租约归属的要求，不该改；要改的是"默认给你看几行"。
  // 连续开发攒下的死行既不影响队列、也不该每天占着面板让人分辨哪条是真的在干活。
  const allNodes = w.workers || [];
  const liveNodes = allNodes.filter((n) => n.online);
  const historyCount = w.history_count != null ? Number(w.history_count) : allNodes.filter((n) => !n.online).length;
  const shownNodes = showWorkerHistory ? allNodes : liveNodes;
  const workers = shownNodes.map(nodeRow).join('');
  const historyLine = historyCount ? `
    <div class="card-body" style="border-top:1px solid var(--line);padding-top:10px">
      <button class="btn xs" id="mc-worker-history">
        ${showWorkerHistory ? '收起离线进程'
          : `另有 ${historyCount} 个进程在 7 天内登记过 · 现已离线（点开看）`}</button>
      <span class="t-desc">离线行不领活、不影响队列；保留 7 天后自动清。
        一行代表一个进程：它的并发上限与"正在跑哪条作业"都记在自己这行上，
        两个进程共用一行会把租约算到别人头上，所以这里不合并、只折叠。</span>
    </div>` : '';

  const queueChips = Object.entries(byRes).map(([k, v]) =>
    `<span class="chip ${v.running ? 'blue' : (v.queued ? 'amber' : 'gray')}">
      ${escapeHtml(k)} 等 ${v.queued} · 跑 ${v.running}</span>`).join('')
    || '<span class="t-dim">队列里没有任何活</span>';

  const statusChips = Object.entries(w.by_status || {}).map(([k, v]) =>
    `<span class="chip plain">${escapeHtml(k)} ${v}</span>`).join('') || '<span class="t-dim">—</span>';

  const alerts = (w.alerts || []).map((a) => `<li>
      <span class="chip ${a.level === 'bad' ? 'red' : 'amber'}">${escapeHtml(alertLabel(a.code))}</span>
      ${escapeHtml(a.text)}</li>`).join('');

  return `
  <div class="card mb-card" >
    <div class="card-head"><h2>队列与 worker</h2>
      <span class="sub">现读 <code>/api/worker/status</code>，与生成中心同一个口径；这一页不开第二条队列</span>
      <span class="right">
        <span class="chip ${w.online ? 'green' : 'gray'}">${w.online ? 'worker 在线' : 'worker 未接入'}</span>
      </span>
    </div>
    <div class="card-body dash-grid">
      <div><span class="grp">排队</span><b>${q.queued ?? 0}</b>
        <div class="dash-chips">${queueChips}</div></div>
      <div><span class="grp">执行中</span><b>${q.running ?? 0}</b>
        <div class="dash-chips">${statusChips}</div></div>
      <div><span class="grp">需要人看一眼</span><b>${w.needs_attention ?? 0}</b>
        <div class="dash-chips"><span class="chip plain">租约过期 ${w.expired_leases ?? 0}</span>
          <span class="chip plain">最长排队 ${w.oldest_queued_seconds ?? 0}s</span></div></div>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>Worker</th><th>状态</th><th>当前作业</th><th>最近心跳</th><th>机器实况</th></tr></thead>
      <tbody>${workers || `<tr><td colspan="5" class="hint">${
        allNodes.length ? '现在没有在线的 worker——队列不会前进。启动：./run.sh worker'
                         : '还没有 worker 登记过心跳'}</td></tr>`}</tbody>
    </table></div>
    ${historyLine}
  </div>

  <div class="card mb-card" >
    <div class="card-head"><h2>告警</h2>
      <span class="sub">判定与处置分开：这里只说哪里不对，取消与重试仍回生成中心做</span></div>
    <div class="card-body">
      ${alerts ? `<ul class="hint flush" >${alerts}</ul>`
              : '<p class="note flush" >现在没有告警。</p>'}
    </div>
  </div>

  ${workerProcessCard()}${concurrencyCard()}`;
}

export function pageMonitor() {
  const s = cache.stats;
  return `
  <div id="mc-live">${liveCards()}</div>

  <div class="card mb-card" >
    <div class="card-head"><h2>模型维度</h2>
      <span class="sub">登记 ${s.registered_total} · 可用 ${s.available_total} · 需要看一眼 ${s.needs_attention}</span></div>
    <div class="card-body">
      <p class="note" style="margin:0 0 8px"><b>下面三件事本轮做不到，如实标出来，不拿估算填</b>：</p>
      <div class="param-chips">
        <span class="chip amber" title="cost_summary 现在只按 currency × capability × 状态聚合">按 provider/model 聚合成本：不支持</span>
        <span class="chip amber" title="列表接口只有 limit，全仓没有 since/date_from">按时间范围筛选：不支持</span>
        <span class="chip amber" title="只有单条 job.elapsed_ms，没有 AVG/分位">分档执行耗时：不支持</span>
      </div>
      <p class="hint" style="margin-top:10px">这三项各要改一处后端（成本聚合维度、列表参数、耗时统计），
        属于 P1。云端调用次数与金额的权威出口仍是生成中心的成本账（§1 不变量：成本只有一个出口），
        这一页不另算一遍。</p>
    </div>
  </div>

  <div class="card">
    <div class="card-head"><h2>最近验证留痕</h2>
      <span class="sub">只追加不改；密钥值不进记录</span>
      <span class="right">${validationPager()}</span></div>
    <div class="table-wrap"><table>
      <thead><tr><th>出处</th><th>结果</th><th>槽位</th><th>作业</th><th>说明</th><th>时间</th></tr></thead>
      <tbody id="mc-val-rows">${(cache.validations?.items || []).map(valRow).join('')
        || '<tr><td colspan="6" class="hint">还没有验证记录</td></tr>'}</tbody>
    </table></div>
    <div class="card-body" style="border-top:1px solid var(--line)">
      <p class="note flush" >这一页是**一页**：接口一次最多回 ${HISTORY_PAGE} 条，
        翻旧账走「更早」那颗按钮——用上一页最后一条的 id 往前切，不跳号也不重复。
        更早的记录不是被删了，只是没往这一屏搬。</p>
    </div>
  </div>`;
}

/* ================= 工作区 3 交互 ================= */
/** 离线进程折叠按钮：轮询重画 #mc-live 后也要重绑，所以单独抽出来。
 *  展开状态存在 shared 的模块变量里，重画不会把人刚点开的那段折回去。 */
function bindWorkerHistory(root) {
  root.querySelector('#mc-worker-history')?.addEventListener('click', () => {
    toggleWorkerHistory();
    // 只重画 live 两张卡，不动验证留痕（保住人翻到的页码）。
    const live = root.querySelector('#mc-live');
    if (live) { live.innerHTML = liveCards(); bindWorkerHistory(root); }
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
function workerProcessCard() {
  const p = cache.workerProcess || {};
  const running = !!p.running;
  const pid = p.pid;
  const stale = p.stale_pid;
  const msg = p.last_action || '';

  const status = running
    ? `<span class="chip green">运行中</span>
       <span class="hint">PID ${escapeHtml(String(pid))}</span>`
    : (stale
      ? `<span class="chip amber">未运行（残留 PID ${escapeHtml(String(stale))}）</span>`
      : '<span class="chip gray">未运行</span>');

  return `
  <div class="card mb-card">
    <div class="card-head"><h2>worker 进程</h2>
      <span class="sub">从这里启停后台干活的进程；与 <code>./run.sh worker</code> 是同一个入口</span>
      <span class="right">${status}</span>
    </div>
    <div class="card-body">
      <p class="note flush">
        <b>这是手动启停，不是守护进程。</b>API 重启不会带走它（它自己是一个独立会话），
        但 worker 自己崩了也<strong>不会</strong>自动拉起——发现队列不动时，回这里看一眼、
        再点一次启动。
      </p>
      <div class="row-actions" style="margin-top:10px">
        ${running
          ? '<button class="btn danger" id="mc-wp-stop">停止 worker</button>'
          : '<button class="btn primary" id="mc-wp-start">启动 worker</button>'}
        <span class="hint" id="mc-wp-msg">${escapeHtml(msg)}</span>
      </div>
      <p class="hint" style="margin-top:8px">
        日志：<code>${escapeHtml(p.log_path || 'data/logs/worker.log')}</code>
        （追加写，重启不丢上一次的输出）。
      </p>
    </div>
  </div>`;
}

async function refreshWorkerProcess(root, action) {
  const msg = root.querySelector('#mc-wp-msg');
  const btn = root.querySelector('#mc-wp-start') || root.querySelector('#mc-wp-stop');
  if (btn) btn.disabled = true;
  if (msg) msg.textContent = action === 'start' ? '正在启动（等它写第一份心跳）…'
                                                : '正在停止（先 SIGTERM 让它收尾）…';
  try {
    const out = action === 'start' ? await api.startWorker({})
                                   : await api.stopWorker();
    cache.workerProcess = out;
    // 重画这两张卡：进程状态与并发评估都可能随着启停变。
    const live = root.querySelector('#mc-live');
    if (live) {
      live.innerHTML = liveCards();
      bindWorkerHistory(root);
      bindWorkerProcess(root);
      bindConcurrency(root);
    }
  } catch (err) {
    // 409 = 已经在跑；400 = 起不来（会带原因）。两种都如实显示，
    // 不把失败伪装成成功——界面说"成功"而队列不动是最坏的情况。
    const text = String(err?.message || err || '');
    if (msg) msg.textContent = text || '操作没成功。';
    if (btn) btn.disabled = false;
  }
}

function bindWorkerProcess(root) {
  root.querySelector('#mc-wp-start')?.addEventListener('click', () => {
    refreshWorkerProcess(root, 'start');
  });
  root.querySelector('#mc-wp-stop')?.addEventListener('click', () => {
    refreshWorkerProcess(root, 'stop');
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
      style="width:74px" ${value == null ? 'placeholder="默认"' : ''}>`;

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
    ? `<p class="note flush" style="margin-top:8px">${escapeHtml(a.sentence || '')}</p>`
    : `<p class="note flush" style="margin-top:8px">
         <b>${escapeHtml(ui.label)}</b>：${escapeHtml(a.sentence || '')}
       </p>
       <p class="hint" style="margin:4px 0 0">
         依据：${escapeHtml(detail || '—')}（各类取峰值而不是平均，并把余量系数
         ${a.safety_ratio ?? '—'} 计入）。总内存 ${a.total_mb
           ? `${Math.round(a.total_mb / 1024)} GB` : '读不到'}，
         已扣系统常驻 ${a.reserved_mb ? `${Math.round(a.reserved_mb / 1024)} GB` : '—'}。
       </p>`;

  const sourceText = c.source === 'web'
    ? `当前生效：<b>网页设置</b>（全局 ${effective.global ?? '—'}）`
    : `当前生效：<b>进程默认</b>（全局 ${effective.global ?? '—'}，来自启动参数或环境变量）`;

  return `
  <div class="card mb-card">
    <div class="card-head"><h2>并发设置</h2>
      <span class="sub">同时最多跑几条作业。改完 worker 下一轮就按新值领活，不用重启</span>
      <span class="right">
        <span class="chip ${ui.chip}">${escapeHtml(ui.label)}</span>
      </span>
    </div>
    <div class="card-body">
      <p class="note flush">${sourceText}</p>
      <div class="table-wrap" style="margin-top:8px"><table>
        <thead><tr><th>资源类</th><th>本机实测占用</th><th>上限</th></tr></thead>
        <tbody>
          <tr><td>全局</td><td class="hint">这条作业总数上限</td>
              <td>${num('mc-conc-global', c.global_max)}</td></tr>
          ${rows}
        </tbody>
      </table></div>
      ${basis}
      <p class="hint" style="margin-top:10px">
        留空＝跟随进程默认（<code>./run.sh worker --max-global N</code> 或
        <code>FASTVIDEO_WORKER_MAX_GLOBAL</code>）。命令行显式给的参数优先于这里，
        不会被网页上的旧值盖掉。
      </p>
      <div class="row-actions" style="margin-top:10px">
        <button class="btn" id="mc-conc-save">保存并发设置</button>
        <span class="hint" id="mc-conc-msg"></span>
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

async function saveConcurrency(root, payload, acknowledgeRisk) {
  const msg = root.querySelector('#mc-conc-msg');
  const btn = root.querySelector('#mc-conc-save');
  if (btn) btn.disabled = true;
  try {
    const body = { ...payload, acknowledge_risk: !!acknowledgeRisk };
    const out = await api.setWorkerConcurrency(body);
    if (msg) {
      msg.textContent = out?.assessment?.verdict === 'safe'
        ? '已保存；worker 下一轮按新值领活。'
        : `已保存（${out?.assessment?.sentence || ''}）`;
    }
  } catch (err) {
    const text = String(err?.message || err || '');
    // 后端把"会溢出，要确认"做成了 400 + 那句话。这里**不猜**该不该继续：
    // 判断这句话是不是那道闸，用 confirm 让人再决定一次，而不是静默重试。
    if (/内存溢出风险/.test(text) && !acknowledgeRisk) {
      if (window.confirm(`${text}\n\n仍要保存吗？`)) {
        await saveConcurrency(root, payload, true);
        return;
      }
      if (msg) msg.textContent = '已取消，没有保存。';
      return;
    }
    if (msg) msg.textContent = text || '没保存成功。';
  } finally {
    if (btn) btn.disabled = false;
  }
}

function bindConcurrency(root) {
  root.querySelector('#mc-conc-save')?.addEventListener('click', () => {
    saveConcurrency(root, readConcurrencyForm(root), false);
  });
}

export function mountMonitor(root) {
  bindWorkerHistory(root);
  bindWorkerProcess(root);
  bindConcurrency(root);

  // 验证历史翻页：再取一页**追加**到后面，而不是重载整页。
  // 重载整页会把上面 worker 表、告警、模型维度一起重画，而人想看的就是这一张表；
  // 走 app:refresh 还会让刚展开的 worker 历史折回去（那是模块变量，会被覆盖）。
  // 追加只动两张东西：表体（追加行）与翻页条（条数与"还有没有更早的"）。
  root.querySelector('#mc-val-older')?.addEventListener('click', async (e) => {
    const items = cache.validations?.items || [];
    const last = items[items.length - 1];
    if (!last) return;
    const btn = e.target;
    btn.disabled = true;
    try {
      const page = await api.providerValidations(HISTORY_PAGE, last.id);
      const fresh = page.items.filter((v) => !items.some((old) => old.id === v.id));
      cache.validations = {
        // 按 id 去重后追加：翻页期间若有人刚写了一条新记录，游标法仍然不会
        // 把重复行带进来；这里再兜一次底，保证表格里不会出现两行同一个 id。
        items: items.concat(fresh),
        total: page.total || cache.validations.total,
      };
      cache.validationPage = { before: last.id, pages: (cache.validationPage?.pages || 0) + 1 };

      const tbody = root.querySelector('#mc-val-rows');
      if (tbody) {
        const empty = tbody.querySelector('td.hint')?.closest('tr');
        if (empty) empty.remove();          // 第一页为空的占位行，来了数据就撤
        tbody.insertAdjacentHTML('beforeend', fresh.map(valRow).join(''));
        const pager = btn.closest('.right');
        if (pager) pager.outerHTML = `<span class="right">${validationPager()}</span>`;
      } else {
        // 表不在了（人切了子页又切回来正好赶上响应）：退回整页重绘兜底
        window.dispatchEvent(new CustomEvent('app:refresh'));
      }
    } catch (err) {
      btn.disabled = false;
      alertNotify(`更早的验证记录读不到：${err.message}`);
    }
  });

  // 作用域轮询：只刷队列与告警（liveCards），不碰验证留痕的页码。
  // 锚点取 #mc-live：切走子页时路由替换 #view-root，它 isConnected 变 false，
  // 下一拍自己清掉。mount 先清旧计时器，避免同屏重 mount 叠出多条。
  if (pollTimer) clearInterval(pollTimer);
  const anchor = root.querySelector('#mc-live');
  const alive = () => anchor.isConnected && (getSub('models') === 3);
  pollTimer = setInterval(async () => {
    if (!alive()) { clearInterval(pollTimer); pollTimer = null; return; }
    // 页面不可见时不发：后台标签页没有"让人知道"的对象（与全站轮询同一纪律）。
    if (document.hidden) return;
    try {
      cache.worker = await api.workerStatus();
      const live = root.querySelector('#mc-live');
      if (live && alive()) { live.innerHTML = liveCards(); bindWorkerHistory(root); }
    } catch { /* 轮询读不到就保持上一帧，下一拍再试；不打断人 */ }
  }, POLL_MS);
}