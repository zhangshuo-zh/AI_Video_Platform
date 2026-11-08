import { api } from '../api.js';
import { store } from '../store.js';
import { confirmDanger, escapeHtml, run } from '../util.js';
import { setSub } from '../navstate.js';
import { renderView } from '../router.js';
import { gotoEntry } from '../goto.js';
import { CAPS } from '../enums.js';
import { DECISION_LABEL, DECISION_TONE } from '../validation.js';

/**
 * 项目生产工作台（0037，任务书 §4）。
 *
 * 这一页存在的唯一理由：让人**不用先懂模块之间的关系**就知道下一步做什么。
 * 所以它只读 `GET /api/projects/{p}/production` 一份数据，九阶段的数字全部
 * 来自服务端现算（`production.py`），界面一个数都不自己加——
 * 一旦这里出现"前端也算一遍"，就会出现工作台与看板两个进度。
 *
 * 三条界面纪律：
 * 1. **阶段不是锁**。九阶段按推荐顺序排，但每一行都能直接点进去，
 *    上游没做完不灰下游——资产与剧本本来就允许并行（任务书 §4.2）。
 * 2. **点一行＝去真正做那件事的页面**，这一页不内嵌任何写操作。
 *    提交、签字、合成各归各的模块，这里只负责把人送到地方。
 * 3. **没接线的不给按钮**。音频阶段照实显示"执行引擎尚未接入"，
 *    不给一个点了必失败的入口。
 */

const TONE = {
  完成: 'green', 进行中: 'blue', 需要处理: 'amber', 失败: 'red', 待处理: 'gray',
};

function stageChip(status) {
  return `<span class="chip ${TONE[status] || 'gray'}">${escapeHtml(status)}</span>`;
}

function headCard(ov, board) {
  const cur = ov.current;
  return `
  <div class="card" style="margin-bottom:12px">
    <div class="mc-strip">
      <span class="st">总集数 <b>${ov.episodes}</b></span>
      <span class="st" title="交付＝成片合成成功且那条合成作业签过通过">已交付
        <b>${ov.delivered_episodes}</b>
        <span class="hint">${ov.progress}%</span></span>
      <span class="st">当前集
        <b>${cur ? `第 ${cur.seq} 集` : '未选'}</b>
        <span class="hint">${cur ? `${cur.shots} 镜 · 有片 ${cur.videos_succeeded}` : '在顶栏选一集'}</span></span>
      <span class="st">这一集镜头 <b>${board.shots || 0}</b></span>
      <span class="st-caliber">数字全部现算：镜头数取分镜表、有片数取每镜最新成功作业、
        签字数取审核记录、成片数取版本表。这一页不写任何东西。</span>
    </div>
  </div>`;
}

function stageTable(board) {
  if (!board.stages || !board.stages.length) {
    return '<div class="empty"><div class="empty-t">还没有集</div>'
      + '<p>先到创作中心建一集，这里才有进度可算。</p></div>';
  }
  const rows = board.stages.map((s) => `
    <tr>
      <td class="t-strong">${escapeHtml(s.label)}</td>
      <td>${stageChip(s.status)}</td>
      <td class="c">${s.done}／${s.total}</td>
      <td class="c">${s.pending || '—'}</td>
      <td class="c">${s.failed || '—'}</td>
      <td class="c">${s.blocked ? `<span class="chip red">${s.blocked}</span>` : '—'}</td>
      <td><span class="t-desc">${escapeHtml(s.note || '')}</span></td>
      <td><button class="btn xs" data-goto="${escapeHtml(JSON.stringify(s.entry))}">进入</button></td>
    </tr>`).join('');
  return `
  <div class="table-wrap"><table>
    <thead><tr><th>阶段</th><th>状态</th><th class="c">完成</th><th class="c">待处理</th>
      <th class="c">失败</th><th class="c">阻塞</th><th>说明</th><th>操作</th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

function taskList(board) {
  const tasks = board.next_tasks || [];
  if (!tasks.length) {
    return `<div class="card-body"><p class="note flush" >
      <b>这一集现在没有待办</b>：九阶段里没有阻塞、失败或未处理的数量。
      要往下做就直接点上面任一阶段。</p></div>`;
  }
  return `<div class="card-body"><ul class="hint flush" >
    ${tasks.map((t) => `<li>
      <span class="chip ${TONE[t.status] || 'gray'}">${escapeHtml(t.status)}</span>
      <b>${escapeHtml(t.label)}</b>：${escapeHtml(t.note || '')}
      <button class="btn xs" data-goto="${escapeHtml(JSON.stringify(t.entry))}">去做</button>
    </li>`).join('')}</ul></div>`;
}

function episodeTable(ov) {
  const rows = (ov.per_episode || []).map((e) => `
    <tr>
      <td class="t-strong">第 ${e.seq} 集${e.title ? ` · ${escapeHtml(e.title)}` : ''}</td>
      <td>${escapeHtml(e.status === 'done' ? '定稿' : e.status === 'active' ? '在产' : e.status)}</td>
      <td class="c">${e.shots}</td>
      <td class="c">${e.videos_succeeded}</td>
      <td class="c">${e.composition_versions}</td>
      <td>${e.delivered ? '<span class="chip green">已交付</span>' : '<span class="chip gray">在产</span>'}</td>
      <td><button class="btn xs" data-ep="${e.episode_id}">设为当前集</button></td>
    </tr>`).join('');
  if (!rows) return '<div class="card-body"><p class="hint flush" >这个项目还没有集。</p></div>';
  return `<div class="table-wrap"><table>
    <thead><tr><th>集</th><th>状态</th><th class="c">镜头</th><th class="c">有片</th>
      <th class="c">成片版本</th><th>交付</th><th>操作</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}


/** 生产统计与质量读数：任务书 §15 的 P2「生产统计」「质量分析」里**能算的那半**。
 *
 *  一屏只说三件事实：跑了多少条、钱与时间花在哪、卡住的原因分布。
 *  数字全部来自服务端同一份聚合（`production.project_stats`）——界面不 SUM、
 *  不算通过率，也不「推荐该重做哪一镜」：那需要质量标准，库里只有人勾过的结论。
 *  推算值没有样本时服务端给 None，这里就老实写「不给推算」。
 */
/** 这一档的单条内存峰值；没有采样读数就写「没读数」。
 *
 *  为什么宁可留一行「没读数」也不显示 `child_max_rss_kb`：那一栏是 worker 生命周期的最大值
 *  （本机实测：跑完一条 8.75GB 的视频作业后，随后一条 1024 图像作业也被记成 8.75GB，
 *  而它自己那棵树采到的是 3.13GB）。把上一条的峰值挂在这一条头上就是假数据。
 */
function peakGb(stats, cap) {
  const item = ((stats.memory || {}).by_capability || {})[cap];
  return item ? item.peak_gb + ' GB' : '没读数';
}

function statsCard(stats) {
  if (!stats || !stats.jobs_total) {
    return `<div class="card mb-card" >
      <div class="card-head"><h2>生产统计</h2>
        <span class="sub">作业、成本、签字与原因分布都来自服务端，界面不另算一本账</span></div>
      <div class="card-body"><p class="hint flush" >这个项目还没有作业记录。</p></div>
    </div>`;
  }
  const capRows = Object.entries(stats.by_capability || {}).map(([cap, c]) => `
    <tr><td>${escapeHtml(CAPS[cap] || cap)}</td>
      <td class="c">${c.total}</td><td class="c">${c.succeeded}</td>
      <td class="c">${c.failed}</td><td class="c">${c.needs_attention}</td>
      <td class="c">${c.other}</td>
      <td class="c">${c.timed ? Math.round(c.avg_ms / 1000) + 's' : '—'}</td>
      <td class="c">${c.max_ms ? Math.round(c.max_ms / 1000) + 's' : '—'}</td>
      <td class="c">${peakGb(stats, cap)}</td></tr>`).join('');
  const money = stats.money || {};
  const cash = Object.entries(money.by_currency || {})
    .map(([cur, v]) => `${v} ${cur}`).join(' ｜ ');
  const errs = (stats.top_error_codes || []).map((e) => `<li>${escapeHtml(e.label)}`
    + ` <span class="t-dim mono">${escapeHtml(e.error_code)}</span> × ${e.n}</li>`).join('');
  const signs = Object.entries(stats.signs || {}).map(([kind, by]) => {
    const name = kind === 'job' ? '产物' : '文档';
    const parts = Object.entries(by).map(([d, n]) =>
      `<span class="chip ${DECISION_TONE[d] || 'gray'}">${escapeHtml(DECISION_LABEL[d] || d)} ${n}</span>`
    ).join(' ');
    return `<div>${name}：${parts || '<span class="t-dim">还没签过</span>'}</div>`;
  }).join('');
  const est = stats.estimate || {};
  const warm = est.minutes != null ? `约 ${est.minutes} 分钟` : '不给推算（还没有可参照的作业）';
  const cold = est.minutes_if_all_new_prompts != null
    ? `；若每条提示词都是新的，约 ${est.minutes_if_all_new_prompts} 分钟`
      + `（每条新提示词多花 ${Math.round((est.cold_extra_ms || 0) / 1000)}s 跑文本编码）` : '';
  const retries = stats.retries || {};
  return `<div class="card mb-card" >
    <div class="card-head"><h2>生产统计</h2>
      <span class="sub">口径与成本账、异常目录同一处</span></div>
    <div class="table-wrap"><table>
      <thead><tr><th>能力</th><th class="c">条数</th><th class="c">成功</th><th class="c">失败</th>
        <th class="c">需处理</th><th class="c">其他</th><th class="c">平均耗时</th><th class="c">最慢</th>
        <th class="c">单条内存峰值</th></tr></thead>
      <tbody>${capRows}</tbody></table></div>
    <div class="card-body" style="border-top:1px solid var(--line)">
      <p class="note" style="margin:0 0 8px"><b>钱与进度。</b>
        计费 ${escapeHtml(cash || '没有计得上价的作业（本地作业不计价）')}；
        云端未定价 ${money.unpriced_cloud || 0} 条。
        还差 ${est.remaining_shots || 0} 镜没有片，按本项目已完成视频作业的均值
        ${est.avg_ms_per_shot ? Math.round(est.avg_ms_per_shot / 1000) + 's/镜' : '—'} 算：${warm}${cold}。
        <span class="t-dim">${escapeHtml(est.basis || '')}</span></p>
      <p class="note" style="margin:0 0 8px"><b>这台机器扛得住几条并行。</b>${memoryLine(stats)}</p>
      <p class="note" style="margin:0 0 8px"><b>磁盘还够不够。</b>${diskLine(stats)}</p>
      ${teamTable(stats)}
      <p class="note" style="margin:0 0 8px"><b>签字分布。</b>${signs || '<span class="t-dim">还没有签字记录</span>'}
        <span class="t-dim">｜没签的那部分由上面「还差几镜」回答，这里不合成一个通过率</span></p>
      <p class="note flush" ><b>卡住的原因排行。</b>
        ${errs ? `<ul class="hint" style="margin:4px 0 0">${errs}</ul>` : '<span class="t-dim">没有失败或需人工处理的作业</span>'}
        <span class="t-dim">｜重投 ${retries.resubmitted || 0} 条、被顶掉 ${retries.superseded || 0} 条</span></p>
      ${(stats.not_computed || []).length
        ? `<p class="hint" style="margin:8px 0 0">这一屏不算的：${
            stats.not_computed.map(escapeHtml).join('；')}</p>` : ''}
    </div></div>`;
}

/** 内存与并行参考的一行话。任何一个数缺了就写「问不到」，不补一个看着合理的默认值。 */
function memoryLine(stats) {
  const mem = stats.memory || {};
  const total = mem.machine_total_gb;
  const caps = Object.entries(mem.by_capability || {});
  if (!caps.length) {
    return '<span class="t-dim">还没有按秒采到的内存读数'
      + '（这一栏自 2026-11-05 起由 worker 记录，之前的作业不补数）</span>';
  }
  const parts = caps.map(([cap, item]) => {
    const hint = ((mem.parallel_reference || {})[cap] || {});
    return escapeHtml(CAPS[cap] || cap) + ' 单条峰值 ' + item.peak_gb + ' GB'
      + (hint.parallel_reference
        ? '｜参考并行 ' + hint.parallel_reference + ' 条'
          + '（' + escapeHtml(hint.formula || '') + '）' : '');
  }).join('；');
  return (total ? '本机 ' + total + ' GB｜' : '问不到这台机器的内存总量｜') + parts
    + ' <span class="t-dim">｜' + escapeHtml(mem.note || '') + '</span>';
}

/** 「重投卡住的镜头」那颗按钮。
 *
 *  按钮只管问服务端要结果，**不自己筛哪些该重投**：
 *  云端与待核对为什么不能自动重投，判据在 `services.job_retry_batch()` 一处
 *  （云端要逐次确认计费、待核对结果未知）——界面猜一遍就会给出一个"点了必被拒"的钮。
 *  只读账号不摆钮（服务端那道 `job:submit` 照样拦，这里只是不给注定 403 的按钮）。
 */
/* 重投的反馈条住在模块级变量里，而不是直接写进 DOM：
 * `app:refresh` 会把整页重画一遍，直接写进去的那句话会在下一次重绘时被擦掉
 * （v0.99 那轮"回稿面板被重画后没人监听"是同一族缺陷，这里是它的另一种形态——
 * 文字还在、只是被擦掉了，人以为没生效）。
 * key 跟着「项目 × 集」走，换集就不能把上一集的结果当这一集的。
 */
let retryNotice = '';
let retryTone = 'good';
let retryKey = '';

function retryBtn(board, stats) {
  const failed = (stats && stats.by_status ? Number(stats.by_status.failed || 0) : 0)
    + (stats && stats.by_status ? Number(stats.by_status.cancelled || 0) : 0);
  if (!store.can('job:submit') || !failed) return '';
  return `<button class="btn xs" id="pb-retry-failed" title="只重投本机失败与已取消的作业；云端与待核对逐条拒并写明原因">`
    + `重投卡住的镜头（${failed}）</button>`;
}

/** 磁盘那一行：把「预计还要多少」与「还剩多少」并排给出来。
 *
 *  这句话与两处预检、产线发起检查是同一处算式（`diskplan.sentence()`），
 *  所以不会出现"这里说够、点下去被拦"。没有样本时服务端自己会退回提醒线那句。
 */
function diskLine(stats) {
  const disk = stats.disk || {};
  if (!disk.sentence) return '<span class="t-dim">读不到磁盘读数</span>';
  return escapeHtml(disk.sentence)
    + (disk.shortfall_bytes
      ? ` <span class="chip red">还差 ${Math.round(Number(disk.shortfall_bytes) / 1024 ** 2)} MB</span>`
      : '');
}

/** 协作读数：谁提交了多少、谁签了几次。
 *
 *  刻意不做两件事，并把"为什么不做"显示在同一张表下面：
 *  不排产量（一次批量重投会算成同一个人的多条），不排通过率（谁能审到哪一条
 *  由可见性与分工决定，而这个平台没有操作审计）。数字只回答"谁按了提交、谁签了字"。
 */
function teamTable(stats) {
  const team = stats.team || {};
  const members = team.members || [];
  if (!members.length) return '';
  const rows = members.map((m) => {
    const signs = Object.entries(m.signs || {}).map(([d, n]) =>
      `<span class="chip ${DECISION_TONE[d] || 'gray'}">${escapeHtml(DECISION_LABEL[d] || d)} ${n}</span>`
    ).join(' ') || '<span class="t-dim">—</span>';
    return `<tr><td>${escapeHtml(m.name || `账号 #${m.user_id}`)}</td>
      <td class="c">${m.submitted}</td>
      <td class="c">${m.succeeded}</td>
      <td class="c">${m.busy_ms ? Math.round(m.busy_ms / 60000) + ' 分钟' : '—'}</td>
      <td>${signs}</td></tr>`;
  }).join('');
  return `<div class="table-wrap" style="margin-top:10px"><table>
      <thead><tr><th>成员</th><th class="c">提交</th><th class="c">成功</th>
        <th class="c">作业占用机时</th><th>签字</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
      ${(team.caveats || []).length ? `<p class="hint" style="margin:6px 0 0">${
        (team.caveats || []).map(escapeHtml).join('；')}</p>` : ''}`;
}

export default {
  async render() {
    const project = store.current;
    if (!project) {
      return `<section class="view on"><div class="page-head"><h1>项目工作台</h1>
        <p>先选一个项目</p></div>
        <div class="card"><div class="empty"><div class="empty-t">还没有项目</div>
        <p>到项目中心建一部作品，再回这里看进度。</p></div></div></section>`;
    }
    let data;
    try {
      data = await api.production(project.id, store.currentEpisodeId);
    } catch (err) {
      return `<section class="view on"><div class="page-head"><h1>项目工作台</h1>
        <p>${escapeHtml(project.name)}</p></div>
        <div class="card"><div class="empty"><div class="empty-t">读不到生产进度</div>
        <p>${escapeHtml(err.message || '未知错误')}</p></div></div></section>`;
    }
    const ov = data.overview;
    const stats = data.stats || null;
    const viewKey = `${project.id}:${store.currentEpisodeId ?? ''}`;
    if (retryKey && retryKey !== viewKey) { retryNotice = ''; retryKey = ''; }
    const board = data.board || { stages: [], next_tasks: [] };
    return `
    <section class="view on">
      <div class="page-head">
        <h1>项目工作台</h1>
        <p>${escapeHtml(project.name)} · 从剧本到成片走到哪一步、下一步做什么</p>
        <div class="right">
          <select class="ctl" id="pb-episode" aria-label="当前集">
            ${(ov.per_episode || []).map((e) => `<option value="${e.episode_id}"
              ${store.currentEpisodeId === e.episode_id ? 'selected' : ''}>第 ${e.seq} 集${e.title ? ' · ' + escapeHtml(e.title) : ''}</option>`).join('')
              || '<option value="">（还没有集）</option>'}
          </select>
          <button class="btn" id="pb-refine">精细制作</button>
          <button class="btn" id="pb-pipeline">产线生产</button>
        </div>
      </div>
      ${headCard(ov, board)}
      <div class="card mb-card" >
        <div class="card-head"><h2>这一集的生产阶段</h2>
          <span class="sub">顺序是推荐，不是锁——资产可以和剧本并行推进</span></div>
        ${stageTable(board)}
        <div class="card-body" style="border-top:1px solid var(--line)">
          <p class="note flush" ><b>阶段之间不互相锁定。</b>
            人物资产可以在写剧本时就先建好；分镜也可以先搭骨架再回补资产。
            真正会拦人的只有两处：提交生成前的预检，和发起成片合成前的检查。</p>
        </div>
      </div>
      <div class="card mb-card" >
        <div class="card-head"><h2>下一步</h2>
          <span class="sub">按真实缺口排：阻塞 → 失败 → 待处理</span>
          ${retryBtn(board, stats)}</div>
        ${taskList(board)}
        <div class="card-body" style="border-top:1px solid var(--line)">
          <p class="bar ${retryTone}" id="pb-retry-out"${retryNotice ? '' : ' hidden'}>`
            + escapeHtml(retryNotice) + `</p>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>各集进度</h2>
          <span class="sub">项目级只看这几列；点开某一集才算九阶段</span></div>
        ${episodeTable(ov)}
      </div>
      ${statsCard(data.stats)}
    </section>`;
  },

  mount(root) {
    const go = (entry) => {
      gotoEntry(entry);
    };
    root.querySelectorAll('[data-goto]').forEach((btn) => {
      btn.addEventListener('click', () => {
        let entry;
        try { entry = JSON.parse(btn.dataset.goto); } catch { return; }
        go(entry);
      });
    });
    root.querySelectorAll('[data-ep]').forEach((btn) => {
      btn.addEventListener('click', () => store.selectEpisode(Number(btn.dataset.ep)));
    });
    const pick = root.querySelector('#pb-episode');
    pick?.addEventListener('change', () => {
      const id = Number(pick.value);
      if (id) store.selectEpisode(id);
    });
    root.querySelector('#pb-refine')?.addEventListener('click', () => go({ module: 'writing', sub: 2 }));
    root.querySelector('#pb-pipeline')?.addEventListener('click', () => go({ module: 'pipeline', sub: 0 }));
    root.querySelector('#pb-retry-failed')?.addEventListener('click', async () => {
      const eid = Number(root.querySelector('#pb-episode')?.value) || store.currentEpisodeId;
      if (!eid) return;
      const go_ahead = await confirmDanger({
        title: '重投卡住的镜头', danger: false, okText: '重投',
        body: '只重投本机的失败与已取消；云端要逐次确认计费、待核对的不自动重投，'
            + '这两种会逐条拒并写明原因。一次最多 50 条。'});
      if (!go_ahead) return;
      const btn = root.querySelector('#pb-retry-failed');
      btn.disabled = true;
      // `run()` 只回 true/false（它负责统一的失败提示与 401），
      // 而这一条要把逐条原因显示出来，所以把响应体从闭包里带出来。
      let result = null;
      const ok = await run('批量重投', async () => { result = await api.jobRetryBatch(eid); });
      btn.disabled = false;
      if (!ok || !result) return;
      const c = result.counts || {};
      const why = (result.skipped || []).slice(0, 6)
        .map((x) => `镜 ${x.shot_id}：${x.reason}`).join('；');
      retryTone = (result.retried && result.retried.length) ? 'good' : 'warn';
      retryNotice = `重投 ${c.retried || 0} 条、跳过 ${c.skipped || 0} 条`
        + `（这一集共 ${c.shots || 0} 镜有记录）`
        + (why ? `｜跳过原因：${why}${(result.skipped || []).length > 6 ? ' …' : ''}` : '');
      retryKey = `${store.currentId}:${eid}`;
      window.dispatchEvent(new CustomEvent('app:refresh'));
    });
  },
};
