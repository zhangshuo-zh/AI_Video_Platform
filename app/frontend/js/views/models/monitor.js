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
// 回前台补一拍的那个闭包。visibilitychange 只在 document 上派发、不冒泡，
// 挂上去就不随 #mc-live 回收，所以必须记住引用、和计时器一起收。
let visHandler = null;
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
  const d = cache.workerDaemon || {};
  const pids = p.pids || [];
  const count = p.count != null ? Number(p.count) : (pids.length || (p.running ? 1 : 0));
  const stale = p.stale_pids || [];
  const msg = p.last_action || '';

  const status = count
    ? `<span class="chip green">运行中 ${count} 个</span>
       <span class="hint mono">PID ${escapeHtml(pids.join('、'))}</span>`
    : (stale.length
      ? `<span class="chip amber">未运行（残留 ${escapeHtml(stale.join('、'))}）</span>`
      : '<span class="chip gray">未运行</span>');

  // 守护那块：**exposed 为真就要红着说**。守护配置里 enabled=true 但线程没在跑，
  // 或连续失败已放弃——这三种情况下"有守护"这件事都不成立，
  // 界面若只显示 enabled 就是在骗人。
  const daemonOn = !!d.enabled;
  const exposed = d.exposed !== false;
  const daemonChip = !daemonOn
    ? '<span class="chip gray">守护未开</span>'
    : (exposed
      ? '<span class="chip red">守护失效</span>'
      : `<span class="chip green">守护中 · 期望 ${d.desired} 个</span>`);

  const daemonLine = !daemonOn
    ? '守护没开：worker 掉了要人手动点启动。'
    : (exposed
      ? `<b>守护配着但没在守</b>${d.thread_alive ? '' : '（看门狗线程没在跑）'}`
        + (d.failures >= 3 ? `；连续 ${d.failures} 次没起来，已停止重试，等人工处理`
                           : '') + '。'
      : `守护每 ${d.check_interval_seconds || 15} 秒检查一次；在线 ${d.online}
         不足 ${d.desired} 就自动补起。累计已补 ${d.recovered || 0} 次。`);

  const actions = (d.actions || []).slice(0, 4).map((a) =>
    `<li class="hint"><code>${escapeHtml(a.at)}</code> ${escapeHtml(a.action)}：${escapeHtml(a.detail)}</li>`
  ).join('');

  return `
  <div class="card mb-card">
    <div class="card-head"><h2>worker 进程</h2>
      <span class="sub">从这里启停后台干活的进程；与 <code>./run.sh worker</code> 是同一个入口</span>
      <span class="right">${status}</span>
    </div>
    <div class="card-body">
      <p class="note flush">
        <b>API 重启不会带走它们</b>（每个都是独立会话）。但 worker 自己崩了
        <strong>只有开了守护</strong>才会自动拉起。
      </p>

      <!-- 启停：标签在左、控件在右，用全站同一套 form-grid。
           原来这几行用的是 row-actions 那个 class——它在 app.css 里根本
           没有定义（全站 4 处用它，一行 CSS 都没有），全靠行内 style 撑，
           于是"启动 [1] 个"和两个按钮挤成一行、宽度随内容乱跳。
           注意：模板字符串里的注释**不能出现反引号**，会提前闭合模板
           字面量、把后面的 HTML 当代码执行（这里撞上了同名的 actions）。 -->
      <div class="form-grid wp-grid">
        <label>数量</label>
        <div class="wp-inline">
          <input class="inp" type="number" id="mc-wp-count" min="1" max="8" value="1">
          <span class="hint">个（上限 8）</span>
        </div>
        <label>操作</label>
        <div class="wp-inline">
          <button class="btn primary" id="mc-wp-start">启动</button>
          <button class="btn danger" id="mc-wp-stop">全部停止</button>
          <span class="hint" id="mc-wp-msg">${hintOf('mc-wp-msg') || escapeHtml(msg)}</span>
        </div>
        ${count ? `<label>逐个停止</label>
        <div class="wp-inline">
          ${pids.map((pid) => `<button class="btn xs" data-stop-pid="${pid}">
            停止 PID ${escapeHtml(String(pid))}</button>`).join('')}
        </div>` : ''}
      </div>

      <!-- 守护：**单独一块**，不和启停混排。它是"要不要让平台自己管进程"的
           配置，上面是"我现在就要起/停"——两个等级不同的动作挤在同一操作行里，
           人会不知道该先按哪个。 -->
      <section class="wp-sub">
        <div class="wp-sub-head">
          <h3>守护（自动拉起）</h3>
          <span class="right">${daemonChip}</span>
        </div>
        <p class="note flush">${daemonLine}</p>
        <div class="form-grid wp-grid">
          <label>开启</label>
          <div class="wp-inline">
            <label class="wp-check">
              <input type="checkbox" id="mc-daemon-on" ${daemonOn ? 'checked' : ''}>
              <span>worker 掉了自动补起</span>
            </label>
          </div>
          <label>期望在线</label>
          <div class="wp-inline">
            <input class="inp" type="number" id="mc-daemon-desired" min="0" max="8"
                   value="${d.desired != null ? d.desired : 1}">
            <span class="hint">个（0 = 不自动补）</span>
          </div>
          <label></label>
          <div class="wp-inline">
            <button class="btn" id="mc-daemon-save">保存守护设置</button>
            <span class="hint" id="mc-daemon-msg">${hintOf('mc-daemon-msg') || ''}</span>
          </div>
        </div>
        <p class="hint wp-boundary">
          <b>边界说清楚</b>：守护住在 API 进程里——<strong>API 自己挂了，守护也跟着停</strong>。
          它能挡的是"worker 崩了"，不是"整台机器挂了"。
        </p>
        ${actions ? `<ul class="wp-actions hint">${actions}</ul>` : ''}
      </section>

      <p class="hint wp-log">
        日志：<code>${escapeHtml(p.log_path || 'data/logs/worker.log')}</code>
        （追加写，重启不丢上一次的输出）。
      </p>
    </div>
  </div>`;
}

async function refreshWorkerProcess(root, action, payload) {
  setMsg(root, 'mc-wp-msg', action === 'start'
    ? '正在启动（逐个等它写第一份心跳）…'
    : '正在停止（先 SIGTERM 让它收尾）…');
  root.querySelectorAll('#mc-wp-start, #mc-wp-stop, [data-stop-pid]')
    .forEach((b) => { b.disabled = true; });
  try {
    let out;
    if (action === 'start') {
      const raw = root.querySelector('#mc-wp-count')?.value;
      const count = Math.max(1, Math.min(8, Number(raw) || 1));
      out = await api.startWorkers({ count });
    } else if (action === 'stopOne') {
      out = await api.stopOneWorker(payload.pid);
    } else {
      out = await api.stopWorker();
    }
    cache.workerProcess = out;
    repaintLive(root);
  } catch (err) {
    // 409 = 已经在跑；400 = 起不来（带原因）。都如实显示，
    // 不把失败伪装成成功——界面说"成功"而队列不动是最坏的情况。
    setMsg(root, 'mc-wp-msg', escapeHtml(String(err?.message || err || '操作没成功。')));
  }
}

async function saveDaemon(root) {
  const enabled = !!root.querySelector('#mc-daemon-on')?.checked;
  const desired = Math.max(0, Math.min(8, Number(
    root.querySelector('#mc-daemon-desired')?.value) || 0));
  setMsg(root, 'mc-daemon-msg', '正在保存…');
  try {
    cache.workerDaemon = await api.setWorkerDaemon({ enabled, desired });
    repaintLive(root);
  } catch (err) {
    setMsg(root, 'mc-daemon-msg', escapeHtml(String(err?.message || err || '保存失败。')));
  }
}

/** 重画 live 两张卡（进程、并发、守护都在这两张里），保留验证留痕不动。 */
function repaintLive(root) {
  const live = root.querySelector('#mc-live');
  if (!live) return;
  live.innerHTML = liveCards();
  bindWorkerHistory(root);
  bindWorkerProcess(root);
  bindConcurrency(root);
}

function bindWorkerProcess(root) {
  root.querySelector('#mc-wp-start')?.addEventListener('click', () => {
    refreshWorkerProcess(root, 'start');
  });
  // **不弹确认框**（2026-11-10 自查改的）。两条理由：
  //  1. 全仓 `window.confirm` 用量是 0——项目自己的确认一律走自绘 `.modal`
  //     （projects.js / asset-detail.js 那几处）。孤零零一个原生弹窗风格完全不搭。
  //  2. 停止 worker 不是"不可撤销"的破坏性操作：正在跑的作业靠租约回收、
  //     重启 worker 就能继续。项目里"取消作业""退出登录"也都不确认。
  // 危险感交给按钮本身（`btn danger`）与回执那句话，不靠一个多余的弹窗。
  root.querySelector('#mc-wp-stop')?.addEventListener('click', () => {
    refreshWorkerProcess(root, 'stop');
  });
  root.querySelectorAll('[data-stop-pid]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const pid = Number(btn.dataset.stopPid);
      refreshWorkerProcess(root, 'stopOne', { pid });
    });
  });
  root.querySelector('#mc-daemon-save')?.addEventListener('click', () => {
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
        <span class="hint" id="mc-conc-msg">${hintOf('mc-conc-msg')}</span>
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
  const btn = root.querySelector('#mc-conc-save');
  if (btn) btn.disabled = true;
  try {
    const body = { ...payload, acknowledge_risk: !!acknowledgeRisk };
    const out = await api.setWorkerConcurrency(body);
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

export function mountMonitor(root) {
  bindWorkerHistory(root);
  bindWorkerProcess(root);
  bindConcurrency(root);

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

  // 作用域轮询：只刷队列与告警（liveCards），不碰验证留痕的页码。
  // 锚点取 #mc-live：切走子页时路由替换 #view-root，它 isConnected 变 false，
  // 下一拍自己清掉。mount 先清旧计时器，避免同屏重 mount 叠出多条。
  stopPoll();
  const anchor = root.querySelector('#mc-live');
  const alive = () => anchor.isConnected && (getSub('models') === 3);
  // 一拍做的事抽成函数：计时器与"回前台补一拍"共用同一份判活/写法，
  // 免得两条路径慢慢长歪（2026-11-11 审计 F-04）。
  const tick = async () => {
    if (!alive()) { stopPoll(); return; }
    try {
      cache.worker = await api.workerStatus();
      const live = root.querySelector('#mc-live');
      if (live && alive()) { live.innerHTML = liveCards(); bindWorkerHistory(root); }
    } catch { /* 轮询读不到就保持上一帧，下一拍再试；不打断人 */ }
  };
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
}

/** 计时器与 document 监听一起收。只清计时器会留下一个拿着旧 alive 闭包的监听。 */
function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  if (visHandler) { document.removeEventListener('visibilitychange', visHandler); visHandler = null; }
}

/** 取更早一页验证记录并**追加**到表后。委托与首次绑定共用这一段。 */
async function loadOlderValidations(root, btn) {
  const items = cache.validations?.items || [];
  const last = items[items.length - 1];
  if (!last) return;
  btn.disabled = true;
  try {
    const page = await api.providerValidations(HISTORY_PAGE, last.id);
    const fresh = page.items.filter((v) => !items.some((old) => old.id === v.id));
    cache.validations = { items: items.concat(fresh),
                          total: page.total || cache.validations.total };
    cache.validationPage = { before: last.id, pages: (cache.validationPage?.pages || 0) + 1 };
    const tbody = root.querySelector('#mc-val-rows');
    if (!tbody) { window.dispatchEvent(new CustomEvent('app:refresh')); return; }
    const empty = tbody.querySelector('td.hint')?.closest('tr');
    if (empty) empty.remove();
    tbody.insertAdjacentHTML('beforeend', fresh.map(valRow).join(''));
    const pager = btn.closest('.right');
    if (pager) {   // 换掉整条翻页条后，按钮是新节点——委托仍在 root 上，所以还能点
      pager.outerHTML = `<span class="right">${validationPager()}</span>`;
    }
  } catch (err) {
    btn.disabled = false;
    alertNotify(`更早的验证记录读不到：${err.message}`);
  }
}
