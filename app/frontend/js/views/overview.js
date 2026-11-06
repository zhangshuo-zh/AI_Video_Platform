import { api } from '../api.js';
import { MODULES } from '../modules.js';
import { store } from '../store.js';
import { escapeHtml, formatMoney } from '../util.js';
import { alertLabel } from '../validation.js';

function alertsBlock(w) {
  const alerts = w?.alerts || [];
  if (!alerts.length) return '';
  return `<div class="gen-alerts">${alerts.map((a) => `
    <div class="gen-alert ${a.level === 'bad' ? 'bad' : 'warn'}">
      <span class="chip ${a.level === 'bad' ? 'red' : 'amber'}">${escapeHtml(alertLabel(a.code))}</span>
      <span>${escapeHtml(a.text)}</span>
    </div>`).join('')}</div>`;
}

/**
 * 首页待办条：进来第一眼先看到"谁在等我"，而不是十张模块说明卡。
 *
 * 数字全部来自 `/api/workbench`——跨模块加总在服务端做，前端只读不拼口径。
 * 一条待办都没有时整条不显示：四张 0 只会占掉第一屏。
 * 用真 `<button>` 而不是 div：键盘能 Tab 到、Enter 能开，和模块卡不一样。
 */
function todoStrip(data) {
  const c = data?.counts;
  if (!c) return '';
  const review = (c.review_documents || 0) + (c.review_artifacts || 0);
  const items = [
    { n: review, label: '待你签字', go: 'review',
      sub: `文档 ${c.review_documents || 0} ／ 产物 ${c.review_artifacts || 0}` },
    { n: c.jobs_failed || 0, label: '失败作业', go: 'generate', tone: 'bad',
      sub: '点进去看是哪一条' },
    { n: c.needs_attention || 0, label: '待核对账单', go: 'generate', tone: 'warn',
      sub: '云端结果等人判' },
    { n: c.runs_active || 0, label: '进行中的运行', go: 'pipeline',
      sub: '还没收尾的产线' },
  ];
  // worker 离线不是"有几条待办"，是"待办不会自己往前走"。
  // 它没有数量，塞进数字卡会变成一句"1"；单独一条提示，且即使零待办也要出现。
  const down = store.worker ? !store.worker.online : false;
  const has = items.some((it) => it.n > 0);
  if (!has && !down) return '';
  const downBar = down ? `<div class="gen-alerts"><div class="gen-alert warn">
      <span class="chip amber">worker 未接入</span>
      <span>队列里的活不会自己往前走；先到生成中心看启动方式。</span>
    </div></div>` : '';
  const cards = has ? `<div class="wb-grid">${items.map((it) => `
    <button type="button" class="wb-card ${it.tone || ''}" data-go="${it.go}">
      <b>${it.n}</b><span>${it.label}</span><em>${it.sub}</em>
    </button>`).join('')}</div>` : '';
  return downBar + cards;
}

/**
 * 首页 = 模块地图 + 九实体说明 + 本机状态。
 * 本机状态里只放后端真报回来的数字；worker / 队列还没接，就老实写"未接入"。
 */
export default {
  async render() {
    // 首页每次都现拉一次 worker 状态：这一页本来就是"本机现在怎么样"，
    // 用 20 秒前的缓存会让刚启动 worker 的人以为没起来。
    await store.loadWorker();
    let dash = null;
    let dashErr = null;
    // 待办与看板各拉各的：一个接口失败不该把首页整页弄没。
    // 待办失败就整条不显示——比摆一排 0 更诚实。
    let todo = null;
    const [dashResult, todoResult] = await Promise.allSettled([
      store.current ? api.dashboard(store.current.id) : Promise.resolve(null),
      api.workbench(),
    ]);
    if (dashResult.status === 'fulfilled') dash = dashResult.value;
    else dashErr = dashResult.reason;
    if (todoResult.status === 'fulfilled') todo = todoResult.value;
    const w = store.worker;
    const q = w?.queue || { queued: 0, running: 0 };
    const cards = MODULES.filter((m) => m.id !== 'overview' && !m.utility && !m.hidden).map((m) => `
        <div class="mod" data-go="${m.id}">
          <h3>${m.label}</h3>
          <p>${m.desc}</p>
          <div class="ents">${m.ents}</div>
        </div>`).join('');

    // 归档的项目不计进"在产"，但也不藏起来：单独一行摆出来，
    // 否则首页那个数字会让人以为项目被删了
    const archivedN = store.projects.length - store.activeProjects.length;

    return `
    <section class="view on">
      <div class="page-head">
        <h1>模块地图</h1>
        <p>平台只编排，不生成；生成全靠适配器调现有脚本</p>
        <div class="right"><span class="chip plain">已接后端：项目 / 类型</span></div>
      </div>

      ${todoStrip(todo)}

      <div class="mods">
        ${cards}
        <div class="mod static">
          <h3>技术栈</h3>
          <p>FastAPI + SQLite WAL + 静态前端（ES Module，无构建链）。适配器一律 CLI 子进程。</p>
          <div class="ents">/api 只返回 JSON</div>
        </div>
      </div>

      ${dashCard(dash, dashErr)}

      <div class="grid g-2" style="margin-top:14px">
        <div class="card">
          <div class="card-head"><h2>九实体怎么连</h2><span class="sub">主干管属于谁，支线管挂在哪</span></div>
          <div class="card-body">
            <div class="lbl">主干 · 从属关系（越往下越细）</div>
            <ul class="etree">
              <li><b>Project</b><span>项目，一切从这里开始</span>
                <ul><li><b>Episode</b><span>创作单元，一集或一卷</span>
                  <ul><li><b>Doc</b><span>文档，小说／剧本／分镜</span>
                    <ul><li><b>Shot</b><span>一条镜头，带景别与台词</span>
                      <ul><li><b>Job</b><span>一次生成，记成本和产物</span></li></ul>
                    </li></ul>
                  </li></ul>
                </li></ul>
              </li>
            </ul>

            <div class="rels">
              <div class="lbl">支线 · 引用与挂载</div>
              <div class="rel"><b>Shot</b><i>引用</i><b>Asset</b><span>角色／场景／道具</span></div>
              <div class="rel"><b>Review</b><i>挂在</i><b>Doc 或 Job</b><span>校验与人签</span></div>
              <div class="rel"><b>Knowledge</b><i>属于</i><b>Project</b><span>设定与证据快照</span></div>
              <div class="rel"><b>Task</b><i>可挂</i><b>任意实体</b><span>指派与人签</span></div>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head"><h2>本机状态</h2><span class="sub">M5 Ultra 96GB · 1.2TB/s</span></div>
          <div class="card-body stack" style="gap:10px">
            ${alertsBlock(w)}
            <div class="row" style="justify-content:space-between">
              <span class="note">在产项目</span>
              <span class="num" style="font-size:12px">${store.activeProjects.length}</span>
            </div>
            ${archivedN ? `<div class="row" style="justify-content:space-between">
              <span class="note">已归档项目</span>
              <span class="num" style="font-size:12px">${archivedN}</span>
            </div>` : ''}
            <div class="row" style="justify-content:space-between">
              <span class="note">生成 worker</span>
              <span class="chip ${w?.online ? 'green' : 'gray'}">${w?.online ? '在线' : '未接入'}</span>
            </div>
            <div class="row" style="justify-content:space-between">
              <span class="note">队列</span>
              <span class="chip ${(q.queued + q.running) ? 'blue' : 'gray'}">${
                (q.queued + q.running) ? `等待 ${q.queued} · 运行 ${q.running}` : '空闲'}</span>
            </div>
            <div class="row" style="justify-content:space-between">
              <span class="note">受限模型（H3 音画同生）</span>
              <span class="chip plain">未启用</span>
            </div>
            <hr class="sep">
            <p class="note">同时最多 1 个本地作业，其余排队。云端 API 可并发提交、串行落库。</p>
          </div>
        </div>
      </div>
    </section>`;
  },

  /** 卡片点击走事件，不直接 import router，避免循环依赖 */
  mount(root) {
    root.querySelectorAll('[data-go]').forEach((el) => {
      el.addEventListener('click', () => {
        window.dispatchEvent(new CustomEvent('app:go', { detail: el.dataset.go }));
      });
    });
  },
};


function dashCard(d, err) {
  if (!store.current) {
    return `<div class="card" style="margin-top:14px"><div class="empty">
      <div class="empty-t">还没有当前项目</div>
      <p>去项目中心选一个项目，这里会显示集、镜、作业、成本、审核与音频的实时汇总。</p>
    </div></div>`;
  }
  if (err) {
    return `<div class="card" style="margin-top:14px"><div class="bar bad">
      读不到项目看板：${escapeHtml(err.message || String(err))}</div></div>`;
  }
  if (!d) return '';
  const chips = (list, cls) => list.length
    ? list.filter((x) => x.count).map((x) => `<span class="chip ${cls}">${escapeHtml(x.label)} ${x.count}</span>`).join(' ')
    : '<span class="t-dim">—</span>';
  const costs = d.costs.length
    ? d.costs.map((c) => `<span class="chip green">${formatMoney(c.micros, c.currency)} · ${c.jobs} 条</span>`).join(' ')
    : '<span class="t-dim">还没有计入成本的作业</span>';
  return `<div class="card" style="margin-top:14px">
    <div class="card-head"><h2>项目看板</h2>
      <span class="sub">${escapeHtml(store.current.name)} · 全部数字从权威表只读聚合</span>
      <div class="right">
        <a class="btn sm" href="/api/projects/${store.current.id}/export/shots.csv" download>导出镜头 CSV</a>
        <a class="btn sm" href="/api/projects/${store.current.id}/export/shots.md" download>导出 Markdown</a>
      </div>
    </div>
    <div class="card-body dash-grid">
      <div><span class="grp">集</span><b>${d.episode_total}</b>
        <div class="dash-chips">${chips(d.episodes, 'gray')}</div></div>
      <div><span class="grp">镜头</span><b>${d.shots}</b>
        <div class="dash-chips"><span class="chip plain">文档 ${d.documents}</span>
          <span class="chip plain">资产 ${d.assets}</span></div></div>
      <div><span class="grp">作业</span><b>${d.job_total}</b>
        <div class="dash-chips">${chips(d.jobs, 'blue')}</div></div>
      <div><span class="grp">成本</span>
        <div class="dash-chips">${costs}</div></div>
      <div><span class="grp">审核</span>
        <div class="dash-chips">${chips(d.gates, 'amber')}</div>
        <div class="dash-chips">${d.signs.map((x) => `<span class="chip plain">${escapeHtml(x.label)} ${x.count}</span>`).join(' ') || '<span class="t-dim">还没有签字</span>'}</div></div>
      <div><span class="grp">音频</span>
        <div class="dash-chips">${chips(d.audio, 'green')}</div></div>
    </div>
  </div>`;
}
