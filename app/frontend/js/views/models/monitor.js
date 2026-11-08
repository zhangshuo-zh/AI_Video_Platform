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
  sourceLabels, sourceLabel, overviewCard, alertNotify,
  showWorkerHistory, toggleWorkerHistory,
} from './shared.js';

/* ================= 工作区 3 · 运行监控 ================= */
/**
 * 这一页只读三样现成的东西：`/api/worker/status`、`/api/models/stats`、
 * `/api/providers/validations`。为什么不去新建一套"模型运行记录"表：
 * Job 才是执行事实的唯一出口（系统设计 §1 不变量 2），
 * 这里再攒一份就必然长出两套状态。
 */
/** 验证历史一页多少条。与后端 `GET /api/providers/validations` 的默认一页对齐，
 *  但**不要**把它当成"历史总数"：总量来自响应头 `X-Total-Count`。 */
const HISTORY_PAGE = 50;

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

export function pageMonitor() {
  const w = cache.worker || {};
  const q = w.queue || {};
  const byRes = q.by_resource || {};
  const s = cache.stats;

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
  const deadNodes = allNodes.filter((n) => !n.online);
  const historyCount = w.history_count != null ? Number(w.history_count) : deadNodes.length;
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
  <div class="card" style="margin-bottom:14px">
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

  <div class="card" style="margin-bottom:14px">
    <div class="card-head"><h2>告警</h2>
      <span class="sub">判定与处置分开：这里只说哪里不对，取消与重试仍回生成中心做</span></div>
    <div class="card-body">
      ${alerts ? `<ul class="hint" style="margin:0">${alerts}</ul>`
              : '<p class="note" style="margin:0">现在没有告警。</p>'}
    </div>
  </div>

  <div class="card" style="margin-bottom:14px">
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
      <tbody id="mc-val-rows">${(cache.validations?.items || []).map((v) => `<tr>
        <td><span class="chip plain">${escapeHtml(v.kind_label || VAL_KIND_LABEL[v.kind] || v.kind)}</span></td>
        <td>${stateChip(v.state)}</td>
        <td class="hint">${escapeHtml(v.capability)} · ${escapeHtml(v.backend)} · ${escapeHtml(v.provider || '—')}</td>
        <td>${v.job_id ? '#' + v.job_id : '—'}</td>
        <td><span class="t-desc">${escapeHtml(v.detail || '')}</span></td>
        <td class="t-dim">${escapeHtml(String(v.created_at || '').slice(0, 16))}</td>
      </tr>`).join('') || '<tr><td colspan="6" class="hint">还没有验证记录</td></tr>'}</tbody>
    </table></div>
    <div class="card-body" style="border-top:1px solid var(--line)">
      <p class="note" style="margin:0">这一页是**一页**：接口一次最多回 ${HISTORY_PAGE} 条，
        翻旧账走「更早」那颗按钮——用上一页最后一条的 id 往前切，不跳号也不重复。
        更早的记录不是被删了，只是没往这一屏搬。</p>
    </div>
  </div>`;
}

/* ================= 工作区 2 交互 ================= */
export function mountMonitor(root) {
  // 折叠/展开离线进程。走 app:refresh 重画整页（与引擎页展开同一条件：
  // 只有重画才能让页内切换器与表体同一轮更新），展开状态存在模块变量里，
  // 所以轮询不会把人刚点开的那段又折回去。
  root.querySelector('#mc-worker-history')?.addEventListener('click', () => {
    toggleWorkerHistory();
    window.dispatchEvent(new CustomEvent('app:refresh'));
  });

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
        tbody.insertAdjacentHTML('beforeend', fresh.map((v) => `<tr>
          <td><span class="chip plain">${escapeHtml(v.kind_label || VAL_KIND_LABEL[v.kind] || v.kind)}</span></td>
          <td>${stateChip(v.state)}</td>
          <td class="hint">${escapeHtml(v.capability)} · ${escapeHtml(v.backend)} · ${escapeHtml(v.provider || '—')}</td>
          <td>${v.job_id ? '#' + v.job_id : '—'}</td>
          <td><span class="t-desc">${escapeHtml(v.detail || '')}</span></td>
          <td class="t-dim">${escapeHtml(String(v.created_at || '').slice(0, 16))}</td>
        </tr>`).join(''));
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
}
