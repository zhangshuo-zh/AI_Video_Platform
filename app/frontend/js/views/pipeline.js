/**
 * 产线中心（0026／阶段 C，v0.90）：单集生产的编排层。
 *
 * 三条边界写在这一页里，别的模块不替它做：
 *   * 只做编排：生成节点派成 Job，仍然由生成中心那一个队列与 worker 执行；
 *     这一页不显示成本账、不读日志、不自己算产物——那些是 Job 的事实。
 *   * 不写审核结论：审核节点只给出「去审核」的入口，签字仍在审核中心落。
 *   * 节点状态只读服务端：进度条与计数都来自 run.progress，页面不自己数。
 *
 * 这一版是**竖向节点列表，不是画布**（需求 §1.2 禁节点图编辑器）。
 * 模板也是只读的：没有拖拽、没有新建模板，改流程要改代码再升版本。
 */
import { api } from '../api.js';
import { store } from '../store.js';
import { currentSubTab, getSub, setSub, setParam } from '../navstate.js';
import { agoText, bindModalDismiss, bindSubChips, confirmDanger, errorBox, escapeHtml, run, subChipRow } from '../util.js';
import { publicStatus } from '../validation.js';
import { gotoEntry } from '../goto.js';
import { diagnosisHtml } from './diagnosis.js';

const TABS = ['运行', '模板', '整季'];
// 与后端 pipeline.RUN_STATUS_LABELS／NODE_STATUS_LABELS 同一套词，这里只决定颜色
const RUN_TONE = {
  running: 'blue', waiting_review: 'amber', attention: 'amber', paused: 'amber',
  cancelled: 'gray', completed: 'green', completed_with_errors: 'green', failed: 'red',
};
const NODE_TONE = {
  blocked: 'gray', pending: 'gray', ready: 'plain', running: 'blue',
  waiting_review: 'amber', attention: 'amber', completed: 'green',
  failed: 'red', skipped: 'gray', cancelled: 'gray',
};
// 模板里只有 type，没有 type_label（节点行才有）。这张表要和后端
// pipeline.NODE_TYPE_LABELS 一字不差；新增节点类型时两处一起改。
const TYPE_LABEL = {
  project_input: '读项目', episode_input: '读集', document_input: '读剧本',
  shot_validate: '分镜检查', asset_validate: '资产检查',
  artifact_validate: '产物检查', audio_prepare: '音频准备',
  image_generate: '关键帧生成', video_generate: '镜头视频',
  image_review: '关键帧验收', shot_review: '镜头审核',
  human_gate: '人工确认', export_prepare: '成片准备',
  export_compose: '成片合成', export_validate: '成片验收', export_review: '成片审核',
};
const FINISHED = ['completed', 'completed_with_errors', 'failed', 'cancelled'];
/**
 * 产线事件的中文名**不在这里**：它由服务端随详情下发（`pipeline.EVENT_LABELS`，
 * `GET /pipeline/runs/{id}` 的 `event_labels` 键）。
 *
 * 这一段原来是一份 17 档的手抄字典，与 `0031_shot_source_version.sql` 里
 * `pipeline_event.event_type` 的 CHECK 各写一份，中间没有任何守卫——
 * 清单加一档，界面会安静地把一句话变成事件码。与智能体那 23 档同一类问题
 * （《写作与引用规范》§2「第二份定义」），同一轮一并收回服务端。
 *
 * 兜底：响应没带（老后端）就原样显示事件码，不显示空白。
 */
let eventLabels = {};

function eventLabel(eventType) {
  return eventLabels[eventType] || eventType;
}

// 打开的运行与同一句提示都要跨重绘活着；提示带上「项目 × 页签」的键，
// 换项目或换页签就收走——同一个失败的提示不该跟着人到下一个项目。
let openRunId = null;
let openSeasonId = null;
let notice = '';
let noticeKey = '';
// 整季页的 worker 表默认只列在线进程（与模型中心·运行监控同一档行为）。
// 视图状态、不进地址栏也不进库，但必须活过轮询重绘，否则刚点开就被折回去。
let showWorkerHistory = false;

const SEASON_TONE = {
  running: 'blue', paused: 'amber', cancelled: 'gray',
  completed: 'green', completed_with_errors: 'amber',
};
const SEASON_ITEM_TONE = {
  pending: 'gray', failed: 'red', running: 'blue', paused: 'amber',
  waiting_review: 'amber', attention: 'amber', cancelled: 'gray',
  completed: 'green', completed_with_errors: 'amber',
};
const RES_LABEL = { text: '文本', image: '图片', video: '视频', audio: '音频', compose: '合成' };

function eventText(ev) {
  const p = ev.payload || {};
  const base = eventLabel(ev.event_type);
  const who = ev.node_label || ev.node_key;
  if (ev.event_type === 'run_created') {
    return `${base} · ${p.template || '模板'} v${p.version ?? ''} · ${p.nodes ?? '?'} 个节点 · ${p.shots ?? 0} 镜`;
  }
  if (ev.event_type === 'job_submitted') {
    const parts = [`#${p.job_id}`];
    if (p.capability) parts.push(p.capability === 'image' ? '图像' : '视频');
    if (p.provider) parts.push(`${p.provider}/${p.model}`);
    return `${base} ${parts.join(' · ')}${p.reused ? '（认回已提交的那一条）' : ''}`;
  }
  if (ev.event_type === 'node_retried') {
    return `${base}：#${p.old_job_id ?? '—'} → #${p.new_job_id ?? '—'}（第 ${p.retry_count ?? 1} 次）`;
  }
  if (ev.event_type === 'node_failed') {
    const reason = p.reason || p.job_status;
    return `${who ? who + ' · ' : ''}${base}${reason ? ' · ' + reason : ''}`;
  }
  if (ev.event_type === 'preflight_warning') {
    const list = (p.warnings || []).slice(0, 3);
    const more = (p.warnings || []).length > list.length ? ' …' : '';
    return `${base} · ${list.join('；')}${more}`;
  }
  if (ev.event_type === 'run_cancelled' && p.reason) return `${base} · ${p.reason}`;
  if (ev.event_type === 'run_completed' || ev.event_type === 'run_failed') {
    return `${base} · ${p.nodes ?? '?'} 个节点${p.failed ? `，失败 ${p.failed}` : ''}`;
  }
  return `${who ? who + ' · ' : ''}${base}`;
}

function progressBar(p) {
  const pct = Math.max(0, Math.min(100, Number(p?.percent) || 0));
  // 跑完的条不再流光：一直动会让人以为还在跑（CSS 里 .done 摘掉那层高光）
  const done = pct >= 100 || (p?.total != null && (p?.done ?? 0) >= p.total);
  return `<div class="pl-bar${done ? ' done' : ''}" role="img" aria-label="完成 ${pct}%">
      <i style="width:${pct}%"></i></div>
    <div class="t-desc">${p?.done ?? 0} / ${p?.total ?? 0} 完成${p?.failed ? ` · ${p.failed} 失败` : ''}${p?.waiting_review ? ` · ${p.waiting_review} 待审` : ''}${p?.attention ? ` · ${p.attention} 待核对` : ''}</div>`;
}

function goto(viewId, sub) {
  setSub(viewId, sub);
  setParam(viewId, undefined);
  window.dispatchEvent(new CustomEvent('app:go', { detail: viewId }));
}

/** 发起运行弹窗：只收「哪一集、哪个模板」——参数由模板冻结，不在这里改。 */
function createModal(episodes, templates, busy) {
  const epOptions = episodes.map((e) => {
    const blocked = busy.has(e.id);
    return `<option value="${e.id}" ${blocked ? 'disabled' : ''}>第 ${e.seq} 集 · ${escapeHtml(e.title)}${blocked ? '（已有未结束的运行）' : ''}</option>`;
  }).join('');
  const tplOptions = templates.map((t) =>
    `<option value="${t.id}">${escapeHtml(t.name)} v${t.version} · ${t.nodes.length} 个节点</option>`).join('');
  return `<div class="modal" id="pl-modal" hidden role="dialog" aria-modal="true" aria-label="发起生产运行">
    <div class="modal-box">
      <div class="modal-head">发起生产运行</div>
      <div class="modal-body">
        <label class="modal-label" for="pl-ep">哪一集</label>
        <select id="pl-ep" class="ctl">${epOptions || '<option value="">本项目还没有集</option>'}</select>
        <label class="modal-label" for="pl-tpl" style="margin-top:12px">用哪个模板</label>
        <select id="pl-tpl" class="ctl">${tplOptions || '<option value="">没有可用模板</option>'}</select>
        <div class="pl-pre" id="pl-pre"><p class="hint">正在做生产前检查…</p></div>
        <p class="modal-meta">发起后由 worker 的节拍按依赖推进；同一集同时只允许一条未结束的运行。
          生成节点会派成普通作业，走平台配的默认模型轨——<b>不会</b>替人切云端、也不会替人确认计费。</p>
        <p class="bar bad" id="pl-modal-err" hidden></p>
      </div>
      <div class="modal-actions">
        <button class="btn" data-pl-cancel>取消</button>
        <button class="btn primary" data-pl-ok>开始生产</button>
      </div>
    </div>
  </div>`;
}

/** 生产前检查的三档渲染。阻断红、警告琥珀；都空时说一句"检查过"。
 *
 * 数据来自统一预检（`precheck.episode_precheck`）而不是 `pipeline.preflight` 原样：
 * 任务书 §10 要「产线与人工两种模式共用一套」，落到界面上就是——
 * 发起弹窗里看到的这一张清单，与工作台、生成中心提交前看到的是**同一份条目与同一份结论**，
 * 每条还带一个跳转（认不出位置的就不摆按钮，比丢回默认页诚实）。
 * 老版响应只有 `blocking`／`warnings` 两组字符串时照样能画（不硬依赖新字段）。
 */
function preflightHtml(data) {
  const items = data?.items || [];
  if (items.length) {
    const group = (level, title, tone) => {
      const list = items.filter((i) => i.level === level);
      if (!list.length) return '';
      return `<div class="pl-pre-group ${tone}"><b>${title}（${list.length}）</b><ul>
        ${list.map((i) => {
          const loc = i.locate || {};
          const jump = loc.module
            ? ` <button class="btn xs" data-goto="${escapeHtml(JSON.stringify(loc))}">${
                loc.shot_id ? '去这一镜' : '去处理'}</button>` : '';
          return `<li class="${tone}">${escapeHtml(i.text)}${jump}</li>`;
        }).join('')}</ul></div>`;
    };
    return group('阻塞', '不能发起', 'bad') + group('警告', '可以发起，但要注意', 'warn')
      + (items.some((i) => i.level === '通过') && items.length === 1
        ? '<p class="hint">生产前检查通过：分镜、引用资产、默认模型轨与运行冲突都没问题。</p>' : '');
  }
  const block = data?.blocking || [];
  const warn = data?.warnings || [];
  if (!block.length && !warn.length) {
    return '<p class="hint">生产前检查通过：分镜、引用资产、默认模型轨与运行冲突都没问题。</p>';
  }
  const items2 = (list, tone) => list.map((t) => `<li class="${tone}">${escapeHtml(t)}</li>`).join('');
  return `${block.length ? `<div class="pl-pre-group bad">
      <b>不能发起（${block.length}）</b><ul>${items2(block, 'bad')}</ul></div>` : ''}
    ${warn.length ? `<div class="pl-pre-group warn">
      <b>可以发起，但要注意（${warn.length}）</b><ul>${items2(warn, 'warn')}</ul></div>` : ''}`;
}

// 发起弹窗里生产前检查的请求序号：改集/改模板都会重问一次，先发的请求可能后到，不挡就会把上一集的阻断结果显示成这一集的，或者反过来——按钮的禁用状态必须跟着最后一次点击的判断走。
let preReqId = 0;

function nodeRows(detail, canSubmit) {
  const labelOf = {};
  detail.nodes.forEach((n) => { labelOf[n.node_key] = n.label; });
  return detail.nodes.map((n, i) => {
    const tone = NODE_TONE[n.status] || 'gray';
    const deps = (n.depends_on || []).map((d) => labelOf[d] || d).join('、');
    // 「能不能重试」是服务端的规则（`pipeline.retry_gate`），不是前端按状态猜的：
    // 被驳回的审核节点 status 也是 failed，但那颗钮点了必吃 409。
    // 老版本的响应里若没有这两个字段，退回原来的判法，不让页面报错。
    // 权限这一半仍在前端：服务端那道 `job:submit` 已经拦在接口上，
    // 这里只是不给只读账号摆一颗注定 403 的钮。
    const retryable = canSubmit && ('retryable' in n
      ? n.retryable : n.status === 'failed');
    const retryHint = n.retry_hint || '只有失败的节点能重试';
    // 读项目／读集／读剧本这三格，类型名和节点名几乎一样（"读取项目"与"读项目"），
    // 再挂一枚类型 chip 只是噪音；从生成节点往后类型才是有用的信息
    const quietType = ['project_input', 'episode_input', 'document_input'].includes(n.node_type);
    // 节点失败与作业失败给的是同一张七字段目录：服务端 `pipeline._node_view` 直接把
    // `diagnosis` 挂在节点上带下来，所以这里不再另打一次接口（两处问会问出两份答复）。
    return `<li class="pl-node st-${n.status}">
      <span class="pl-idx">${i + 1}</span>
      <div class="pl-main">
        <div class="pl-title"><b>${escapeHtml(n.label)}</b>
          <span class="chip ${tone}">${escapeHtml(n.status_label)}</span>
          ${quietType ? '' : `<span class="chip plain">${escapeHtml(n.type_label || TYPE_LABEL[n.node_type] || n.node_type)}</span>`}
          ${n.retry_count ? `<span class="t-desc">已重试 ${n.retry_count} 次</span>` : ''}
        </div>
        <div class="pl-meta">
          <span class="mono">${escapeHtml(n.node_key)}</span>
          ${deps ? `<span>前置：${escapeHtml(deps)}</span>` : ''}
          ${n.job_id ? `<span>作业 #${n.job_id}${n.job_status ? ' · ' + escapeHtml(publicStatus(n.job_status)) : ''}${n.job_status_label ? `（原词：${escapeHtml(n.job_status_label)}）` : ''}</span>
            <button class="btn xs" data-go-generate>去生成中心</button>` : ''}
          ${n.review_id ? `<span>签字 #${n.review_id}</span>
            <button class="btn xs" data-go-review>去审核</button>` : ''}
          ${n.started_at ? `<span class="t-dim">开始 ${escapeHtml(n.started_at)}</span>` : ''}
          ${n.finished_at ? `<span class="t-dim">结束 ${escapeHtml(n.finished_at)}</span>` : ''}
        </div>
        ${n.error_message ? `<p class="pl-err">${escapeHtml(n.error_message)}</p>` : ''}
        ${n.diagnosis && n.error_code ? `<div class="pl-diag">`
          + diagnosisHtml({ error_message: n.error_message, diagnosis: n.diagnosis },
            '这一格为什么停在这里，接下来怎么办') + `</div>` : ''}
        ${retryable ? `<div class="row" style="gap:6px;margin-top:6px">
          <button class="btn sm" data-retry-node="${n.id}">重试这一格</button>
          <span class="t-desc">旧作业、旧产物、旧签字都留着，新作业另起一行</span></div>` : ''}
        ${canSubmit && !retryable && n.status === 'failed'
          ? `<p class="t-desc" style="margin-top:6px">${escapeHtml(n.retry_hint || retryHint)}</p>`
          : ''}
      </div>
    </li>`;
  }).join('');
}

function detailHtml(detail) {
  const canSubmit = store.can('job:submit');
  const canCancel = store.can('job:cancel');
  const tone = RUN_TONE[detail.status] || 'gray';
  const finished = FINISHED.includes(detail.status);
  const events = detail.events.slice().reverse().map((ev) => `<li>
      <time>${escapeHtml(ev.created_at)}</time>
      <span>${escapeHtml(eventText(ev))}</span></li>`).join('');
  return `<div class="card mt-card" id="pl-detail" >
    <div class="card-head">
      <h2>${escapeHtml(detail.episode_title || `集 #${detail.episode_id}`)} · 运行 #${detail.id}</h2>
      <span class="sub">${escapeHtml(detail.template_name)} v${detail.template_version}
        · ${detail.shots} 镜</span>
      <div class="right">
        <span class="chip ${tone}">${escapeHtml(detail.status_label)}</span>
        ${!finished && canSubmit ? '<button class="btn sm" data-advance title="worker 的节拍本来也会推进；这里只是手动催一下">推进</button>' : ''}
        ${!finished && canCancel ? `<button class="btn sm" data-pause>${detail.paused ? '恢复' : '暂停'}</button>` : ''}
        ${!finished && canCancel ? '<button class="btn sm danger" data-cancel>取消</button>' : ''}
      </div>
    </div>
    <div class="card-body">
      ${detail.cancelled && detail.cancel_reason
        ? `<div class="bar warn"><p>这条运行已取消：${escapeHtml(detail.cancel_reason)}</p></div>` : ''}
      ${detail.paused ? '<div class="bar warn"><p>已暂停：不再派新节点，已经在跑的作业会跑完，结果照实记。</p></div>' : ''}
      <div class="pl-head">
        <div>${progressBar(detail.progress)}</div>
        <div class="t-desc">
          ${detail.started_at ? `开始 ${escapeHtml(detail.started_at)} · ` : ''}
          ${detail.finished_at ? `结束 ${escapeHtml(detail.finished_at)}` : '尚未结束'}
        </div>
      </div>
      <ol class="pl-nodes">${nodeRows(detail, canSubmit)}</ol>
      ${detail.events.length ? `<div class="pl-events-head">时间线</div>
        <ol class="pl-events">${events}</ol>` : ''}
      <p class="t-desc" style="margin-top:10px">成本、日志与产物都不在这一页：点「去生成中心」按作业号看。
        审核结论也不在这一页：点「去审核」到审核中心的产物审核页签签字。</p>
    </div>
  </div>`;
}

/* ---------------- 整季生产（阶段 E-2／E-3） ---------------- */

function money(micros, currencies) {
  const value = Number(micros || 0) / 1_000_000;
  const cur = (currencies && currencies[0]) || 'CNY';
  const symbol = cur === 'CNY' ? '¥' : `${cur} `;
  return `${symbol}${value.toFixed(2)}`;
}

function workerPanel() {
  const w = store.worker;
  const workers = w?.workers || [];
  const byRes = w?.queue?.by_resource || {};
  const resChips = Object.keys(RES_LABEL).map((key) => {
    const q = byRes[key] || { queued: 0, running: 0 };
    return `<span class="chip ${q.running ? 'blue' : 'plain'}">${RES_LABEL[key]}
      ${q.running ? `跑 ${q.running}` : ''}${q.queued ? ` 等 ${q.queued}` : ''}
      ${!q.running && !q.queued ? '空闲' : ''}</span>`;
  }).join('');
  const liveNodes = workers.filter((n) => n.online);
  const deadCount = w?.history_count != null
    ? Number(w.history_count) : workers.length - liveNodes.length;
  const shownNodes = showWorkerHistory ? workers : liveNodes;
  const rows = shownNodes.map((node) => {
    const limits = node.limits || {};
    const per = limits.per || {};
    const cap = Object.keys(RES_LABEL)
      .map((key) => `${RES_LABEL[key]}${per[key] ?? 1}`).join(' · ');
    const r = node.resources || {};
    // 占用是机器实况、上限是人声明的意愿：出问题时先看左边那一列，
    // 而不是先去调上限——负载 8 而 cores 4 时再怎么调上限都只是排队更久
    const busy = r.cores ? `负载 ${r.load1 ?? 0}/${r.cores} 核` : '';
    const use = `CPU ${r.cpu_pct ?? 0}% · 内存峰值 ${r.rss_peak_mb ?? 0}MB`;
    const disk = r.disk_total_gb ? `盘余 ${r.disk_free_gb ?? 0}/${r.disk_total_gb}GB` : '';
    const occ = [busy, use, disk].filter(Boolean).join(' · ');
    return `<tr>
      <td class="mono">${escapeHtml(node.host || node.worker_id)}<span class="t-desc">pid ${node.pid}</span></td>
      <td><span class="chip ${node.online ? 'green' : 'gray'}">${node.online ? '在线' : '离线'}</span></td>
      <td class="num">${escapeHtml(agoText(node.age_seconds))}</td>
      <td class="num">${escapeHtml(occ || '—')}<span class="t-desc">全局 ${limits.global ?? 1} · ${escapeHtml(cap)}</span></td>
      <td>${node.current_job_id
        ? `<button class="btn xs" data-go-job="${node.current_job_id}">作业 #${node.current_job_id}</button>`
        : '<span class="t-desc">空闲</span>'}</td>
    </tr>`;
  }).join('');
  return `<div class="card season-workers">
    <div class="card-head"><h2>本机 worker</h2>
      <span class="sub">占用是机器实况、上限是人给的额度；能不能并行看槽位，该不该再开一个看左边</span>
      <div class="right"><span class="chip ${w?.online ? 'green' : 'gray'}">${w?.online ? '在线' : '离线'}</span></div></div>
    <div class="card-body">
      <div class="row" style="gap:6px;flex-wrap:wrap">${resChips}</div>
      ${workers.length ? `<div class="table-wrap" style="margin-top:10px"><table>
        <thead><tr><th>进程</th><th>状态</th><th class="num">心跳</th>
          <th class="num">占用与上限</th><th>正在跑</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
        ${deadCount ? `<div class="row" style="margin-top:10px;gap:8px;align-items:center">
          <button class="btn xs" id="sp-worker-history">${
            showWorkerHistory ? '收起离线进程'
              : `另有 ${deadCount} 个进程在 7 天内登记过 · 现已离线（点开看）`}</button>
          <span class="t-desc">离线行不领活、不影响队列；保留 7 天后自动清。
            一行＝一个进程：它的上限与"正在跑哪条"都记在自己这行，合并会把租约算到别人头上。</span>
        </div>` : ''}` : `<p class="hint">还没有 worker 心跳。离线时队列不会自己前进：另开终端跑 <code>./run.sh worker</code>。</p>`}
    </div>
  </div>`;
}

function seasonRows(list) {
  return list.map((s) => {
    const tone = SEASON_TONE[s.status] || 'gray';
    return `<tr class="${s.id === openSeasonId ? 'sel' : ''}">
      <td><b>${escapeHtml(s.name)}</b></td>
      <td>${escapeHtml(s.template_name)} <span class="t-desc">v${s.template_version}</span></td>
      <td><span class="chip ${tone}">${escapeHtml(s.status_label)}</span></td>
      <td style="min-width:150px">${progressBar(s.progress)}</td>
      <td class="num">${s.max_active_runs} 条</td>
      <td class="num">${money(s.cost?.cost_micros, s.cost?.currencies)}</td>
      <td class="num t-dim">${escapeHtml(s.created_at)}</td>
      <td class="r"><button class="btn sm primary" data-open-season="${s.id}">查看</button></td>
    </tr>`;
  }).join('');
}

function seasonDetailHtml(d, canSubmit) {
  const tone = SEASON_TONE[d.status] || 'gray';
  const canRun = canSubmit && !d.cancelled && !d.finished;
  const canRetry = canSubmit && !d.cancelled
    && (d.counts.failed > 0 || d.status === 'completed_with_errors');
  const items = d.items.map((i) => {
    const itemTone = SEASON_ITEM_TONE[i.status] || 'gray';
    return `<tr>
      <td><b>第 ${i.episode_seq} 集</b>
        ${i.episode_title && i.episode_title !== `第 ${i.episode_seq} 集`
          ? `<span class="t-desc">${escapeHtml(i.episode_title)}</span>` : ''}</td>
      <td><span class="chip ${itemTone}">${escapeHtml(i.status_label)}</span></td>
      <td style="min-width:140px">${i.run_id ? progressBar(i.progress) : '<span class="t-desc">还没开始</span>'}</td>
      <td class="num">${money(i.cost?.cost_micros, d.cost?.currencies)}<span class="t-desc">${i.cost?.jobs ?? 0} 条作业</span></td>
      <td>${i.error_message ? `<span class="season-err">${escapeHtml(i.error_message)}</span>` : '<span class="t-desc">—</span>'}</td>
      <td class="r">${i.run_id ? `<button class="btn xs" data-open-run="${i.run_id}">运行</button>` : ''}</td>
    </tr>`;
  }).join('');
  return `<div class="card season-detail">
    <div class="card-head"><h2>${escapeHtml(d.name)}</h2>
      <span class="sub">${escapeHtml(d.template_name)} v${d.template_version}
        · 最多同时 ${d.max_active_runs} 集 · 成本按产线派出的作业汇总</span>
      <div class="right"><span class="chip ${tone}">${escapeHtml(d.status_label)}</span></div></div>
    <div class="card-body">
      ${d.cancel_reason ? `<div class="bar bad"><p>取消原因：${escapeHtml(d.cancel_reason)}</p></div>` : ''}
      <div class="season-stats">
        <div><span class="t-desc">进度</span><b>${d.progress.done} / ${d.progress.total} 集</b></div>
        <div><span class="t-desc">状态分布</span><b>完成 ${d.counts.completed} · 失败 ${d.counts.failed} · 待开始 ${d.counts.pending}</b></div>
        <div><span class="t-desc">累计成本</span><b>${money(d.cost.cost_micros, d.cost.currencies)}</b>
          <span class="t-desc">${d.cost.jobs} 条作业；本地作业为 0</span></div>
      </div>
      ${(canRun || canRetry) ? `<div class="row season-actions">
        ${canRun ? (d.paused
          ? '<button class="btn sm" data-season-resume>恢复排产</button>'
          : '<button class="btn sm" data-season-pause>暂停排产</button>') : ''}
        ${canRetry ? '<button class="btn sm" data-season-retry>继续失败集</button>' : ''}
        ${canRun ? '<button class="btn sm danger" data-season-cancel>取消整季</button>' : ''}
      </div>` : ''}
      <div class="table-wrap" style="margin-top:10px"><table>
        <thead><tr><th>集</th><th>状态</th><th>进度</th><th class="num">成本</th>
          <th>原因</th><th class="r"></th></tr></thead>
        <tbody>${items}</tbody></table></div>
      <p class="hint">「继续失败集」只重试失败节点：已成功的镜头不会重新生成。
        页面不轮询，worker 推进后刷新看最新。</p>
    </div>
  </div>`;
}

function seasonModal(templates, episodes) {
  const tplOptions = templates.map((t) =>
    `<option value="${t.id}">${escapeHtml(t.name)} v${t.version} · ${t.nodes.length} 个节点</option>`).join('');
  const epOptions = episodes.map((e) => `<label class="season-ep">
      <input type="checkbox" value="${e.id}" checked>
      <span>第 ${e.seq} 集 · ${escapeHtml(e.title)}</span>
      <span class="t-desc">${e.shot_count} 镜</span>
    </label>`).join('');
  return `<div class="modal" id="season-modal" hidden role="dialog" aria-modal="true" aria-label="发起整季生产">
    <div class="modal-box wide">
      <div class="modal-head neutral">发起整季生产</div>
      <div class="modal-body">
        <label class="modal-label" for="season-name">批次名（可不填）</label>
        <input class="ctl" id="season-name" maxlength="80" placeholder="例如：第一季 · 十二集">
        <label class="modal-label" for="season-tpl" style="margin-top:10px">用哪个模板</label>
        <select class="ctl" id="season-tpl">${tplOptions || '<option value="">没有可用模板</option>'}</select>
        <label class="modal-label" for="season-max" style="margin-top:10px">同时推进几集</label>
        <select class="ctl" id="season-max">
          <option value="1">1 集（串行，默认）</option>
          <option value="2">2 集</option>
          <option value="3">3 集</option>
          <option value="4">4 集</option>
        </select>
        <div class="season-ep-head"><span class="modal-label">排哪几集</span>
          <button class="btn xs" type="button" data-season-all>全选／全不选</button></div>
        <div class="season-eps" id="season-eps">${epOptions || '<p class="hint">本项目还没有集。</p>'}</div>
        <p class="modal-meta">一次只按上限起新集；生成节点仍走平台默认模型轨，<b>不代发云端作业</b>。
          同时推进几集还要看 worker 的资源类槽位——两个闸门都过才会真的并行。</p>
        <p class="bar bad" id="season-modal-err" hidden></p>
      </div>
      <div class="modal-actions">
        <button class="btn" data-season-cancel-modal>取消</button>
        <button class="btn primary" data-season-ok>开始整季</button>
      </div>
    </div>
  </div>`;
}

function bindSeasons(root, refresh) {
  root.querySelectorAll('[data-go-job]').forEach((btn) => {
    btn.addEventListener('click', () => goto('generate', 0));
  });
  root.querySelectorAll('[data-open-run]').forEach((btn) => {
    btn.addEventListener('click', () => {
      openRunId = Number(btn.dataset.openRun);
      setSub('pipeline', 0);
      refresh();
    });
  });
  root.querySelector('#sp-worker-history')?.addEventListener('click', () => {
    showWorkerHistory = !showWorkerHistory;
    refresh();
  });

  root.querySelectorAll('[data-open-season]').forEach((btn) => {
    btn.addEventListener('click', () => {
      openSeasonId = Number(btn.dataset.openSeason);
      refresh();
    });
  });

  const act = async (label, fn, okText) => {
    if (!(await run(label, fn))) return;
    notice = okText;
    noticeKey = `${store.current.id}:2`;
    refresh();
  };
  root.querySelector('[data-season-pause]')?.addEventListener('click', () =>
    act('暂停', () => api.pauseSeason(openSeasonId),
      '已暂停排产：不再起新集，已经在跑的单集继续跑完。'));
  root.querySelector('[data-season-resume]')?.addEventListener('click', () =>
    act('恢复', () => api.resumeSeason(openSeasonId), '已恢复排产。'));
  root.querySelector('[data-season-retry]')?.addEventListener('click', async () => {
    if (!(await confirmDanger({
      title: '继续失败集',
      body: '只重试失败的节点、重排启动被拒的集；<b>已成功的镜头不会重新生成</b>。'
        + '被驳回的审核节点不自动重试——那要人先决定重做哪一镜。',
      okText: '继续失败集',
      danger: false,
    }))) return;
    await act('继续失败集', () => api.retrySeasonFailed(openSeasonId),
      '已重试失败节点并重排待开始的集；刷新后看每集结果。');
  });
  root.querySelector('[data-season-cancel]')?.addEventListener('click', async () => {
    if (!(await confirmDanger({
      title: '取消整季',
      body: '还没开始的集不再排；已经在跑的单集会收到取消请求。'
        + '已完成节点与已生成的产物<b>一个都不动</b>。',
      okText: '取消整季',
    }))) return;
    await act('取消整季', () => api.cancelSeason(openSeasonId, '在产线中心取消整季'),
      '这一季已取消。');
  });

  const modal = root.querySelector('#season-modal');
  if (!modal) return;
  let idemKey = '';
  const openModal = () => {
    const err = modal.querySelector('#season-modal-err');
    err.hidden = true;
    idemKey = (crypto.randomUUID?.() ?? `season-${Date.now()}-${Math.random().toString(36).slice(2)}`);
    modal.hidden = false;
  };
  const closeModal = () => { modal.hidden = true; };
  bindModalDismiss(modal, closeModal);
  modal.querySelector('[data-season-cancel-modal]').addEventListener('click', closeModal);
  modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
  root.querySelectorAll('[data-new-season]').forEach((b) => b.addEventListener('click', openModal));
  modal.querySelector('[data-season-all]').addEventListener('click', () => {
    const boxes = [...modal.querySelectorAll('#season-eps input[type="checkbox"]')];
    const allOn = boxes.every((b) => b.checked);
    boxes.forEach((b) => { b.checked = !allOn; });
  });
  modal.querySelector('[data-season-ok]').addEventListener('click', async () => {
    const err = modal.querySelector('#season-modal-err');
    const templateId = Number(modal.querySelector('#season-tpl').value);
    const episodeIds = [...modal.querySelectorAll('#season-eps input[type="checkbox"]:checked')]
      .map((b) => Number(b.value));
    const maxActive = Number(modal.querySelector('#season-max').value) || 1;
    const name = modal.querySelector('#season-name').value.trim();
    if (!templateId) {
      err.textContent = '先选一个模板。';
      err.hidden = false;
      return;
    }
    if (!episodeIds.length) {
      err.textContent = '至少勾一集。';
      err.hidden = false;
      return;
    }
    try {
      const view = await api.createSeason(store.current.id, {
        template_id: templateId, episode_ids: episodeIds,
        max_active_runs: maxActive, name, idempotency_key: idemKey,
      });
      idemKey = '';
      openSeasonId = view.id;
      closeModal();
    } catch (e) {
      err.textContent = e.message || '没发起成功。';
      err.hidden = false;
      return;
    }
    notice = '整季已排产：worker 会按上限逐集起运行；刷新看进度。';
    noticeKey = `${store.current.id}:2`;
    refresh();
  });
}

export default {
  subIndex: 0,

  async render() {
    const project = store.current;
    if (!project) {
      openRunId = null; notice = ''; noticeKey = '';
      return `<section class="view on"><div class="card"><div class="empty">
        <div class="empty-t">先选一个项目</div>
        <p>产线运行按项目与集组织；去项目中心选一个再回来。</p>
      </div></div></section>`;
    }
    const tab = currentSubTab('pipeline', TABS);
    const key = `${project.id}:${tab}`;
    if (noticeKey !== key) { notice = ''; noticeKey = ''; }
    const HEAD_DESC = [
      '单集生产的编排层：发起、看节点、暂停与重试',
      '内置流程模板（这一版只读）',
      '把一季的集排成批量：并发上限、失败恢复与 worker 状态',
    ];
    const head = `<div class="page-head">
        <h1>产线中心</h1>
        <p>${HEAD_DESC[tab] || HEAD_DESC[0]} · ${escapeHtml(project.name)}</p>
        <div class="row" style="gap:6px">${subChipRow(TABS, tab)}</div>
      </div>`;

    if (tab === 2) {
      let list = [];
      let templates = [];
      let episodes = [];
      let detail = null;
      let err = null;
      try {
        await store.loadWorker();
        [list, templates, episodes] = await Promise.all([
          api.seasons(project.id), api.pipelineTemplates(), api.episodes(project.id),
        ]);
        if (openSeasonId && !list.some((s) => s.id === openSeasonId)) openSeasonId = null;
        if (openSeasonId) detail = await api.season(openSeasonId);
      } catch (e) { err = e; }
      const canSubmit = store.can('job:submit');
      return `<section class="view on">
        ${head}
        ${notice ? `<div class="bar ok"><p>${escapeHtml(notice)}</p></div>` : ''}
        ${err ? errorBox('整季生产', err) : ''}
        ${workerPanel()}
        <div class="card mt-card" >
          <div class="card-head"><h2>整季批次</h2>
            <span class="sub">每集仍是一条单集运行；这一层只决定何时起下一集与失败怎么恢复</span>
            <div class="right">
              ${canSubmit ? '<button class="btn sm primary" data-new-season>发起整季</button>'
                : '<span class="t-desc">发起整季需要 job:submit</span>'}
            </div></div>
          ${list.length ? `<div class="table-wrap"><table>
            <thead><tr><th>批次</th><th>模板</th><th>状态</th><th>进度</th>
              <th class="num">并发</th><th class="num">成本</th><th class="r">创建</th><th class="r"></th></tr></thead>
            <tbody>${seasonRows(list)}</tbody></table></div>` : `<div class="empty">
            <div class="empty-t">还没有整季批次</div>
            <p>把一季的集一次排进来：按顺序起运行，失败只补失败的那几镜；
              已成功的镜头不会被重新生成。</p>
            ${canSubmit ? '<div class="empty-form"><button class="btn primary" data-new-season>发起整季</button></div>' : ''}
          </div>`}
        </div>
        ${detail ? seasonDetailHtml(detail, canSubmit) : (openSeasonId ? '<div class="card mt-card" ><div class="card-body"><p class="hint">正在读整季…</p></div></div>' : '')}
        ${seasonModal(templates.filter((t) => t.enabled !== false), episodes)}
      </section>`;
    }

    if (tab === 1) {
      let templates = [];
      let err = null;
      try { templates = await api.pipelineTemplates(); } catch (e) { err = e; }
      const cards = templates.map((t) => {
        const nodes = t.nodes.map((n, i) => `<li>
            <span class="pl-idx">${i + 1}</span>
            <b>${escapeHtml(n.label)}</b>
            <span class="chip plain">${escapeHtml(TYPE_LABEL[n.type] || n.type)}</span>
            <span class="mono">${escapeHtml(n.key)}</span>
            ${n.fan_out === 'shot' ? '<span class="chip blue">按镜头展开</span>' : ''}
            ${n.depends_on.length ? `<span class="t-desc">前置 ${escapeHtml(n.depends_on.join('、'))}</span>` : ''}
          </li>`).join('');
        return `<div class="card pl-tpl">
          <div class="card-head"><h2>${escapeHtml(t.name)}</h2>
            <span class="sub">${escapeHtml(t.description || '')}</span>
            <div class="right">
              <span class="chip plain">v${t.version}</span>
              <span class="chip ${t.builtin ? 'blue' : 'gray'}">${t.builtin ? '内置' : '自建'}</span>
              <span class="chip gray">${t.nodes.length} 个节点</span>
            </div></div>
          <div class="card-body">
            <details>
              <summary class="pl-summary">展开看节点与依赖</summary>
              <ol class="pl-tpl-nodes">${nodes}</ol>
            </details>
          </div>
        </div>`;
      }).join('');
      return `<section class="view on">
        ${head}
        <div class="bar"><p>模板这一版是只读的：没有拖拽式流程编辑器（需求 §1.2 禁节点画布）。
          改流程＝版本加一再存一行，已经跑着的运行仍认它创建时那一版。</p></div>
        ${err ? errorBox('模板', err) : ''}
        ${templates.length ? `<div class="pl-tpls">${cards}</div>` : (err ? '' : `<div class="card"><div class="empty">
          <div class="empty-t">还没有模板</div><p>内置模板应在启动时补齐；一份都没有说明迁移或 seed 没跑完。</p>
        </div></div>`)}
      </section>`;
    }

    let list = [];
    let templates = [];
    let episodes = [];
    let err = null;
    try {
      [list, templates, episodes] = await Promise.all([
        api.pipelineRuns(project.id), api.pipelineTemplates(), api.episodes(project.id),
      ]);
    } catch (e) { err = e; }
    if (openRunId && !list.some((r) => r.id === openRunId)) openRunId = null;

    const canSubmit = store.can('job:submit');
    const busy = new Set(list.filter((r) => !FINISHED.includes(r.status)).map((r) => r.episode_id));
    const rows = list.map((r) => {
      const tone = RUN_TONE[r.status] || 'gray';
      // 集的标题常常就是「第 1 集」，跟前面那列重复；只有真有名字时才再显示一次
      const epName = r.episode_title && r.episode_title !== `第 ${r.episode_seq} 集`
        ? `<span class="t-desc">${escapeHtml(r.episode_title)}</span>` : '';
      return `<tr class="${r.id === openRunId ? 'sel' : ''}">
        <td><b>第 ${r.episode_seq ?? '—'} 集</b>
          ${epName}</td>
        <td>${escapeHtml(r.template_name)} <span class="t-desc">v${r.template_version}</span></td>
        <td><span class="chip ${tone}">${escapeHtml(r.status_label)}</span></td>
        <td style="min-width:150px">${progressBar(r.progress)}</td>
        <td class="num t-dim">${escapeHtml(r.started_at || r.created_at)}</td>
        <td class="r"><button class="btn sm primary" data-open-run="${r.id}">查看</button></td>
      </tr>`;
    }).join('');

    return `<section class="view on">
      ${head}
      ${notice ? `<div class="bar ok"><p>${escapeHtml(notice)}</p></div>` : ''}
      ${err ? errorBox('产线运行', err) : ''}
      <div class="card">
        <div class="card-head"><h2>生产运行</h2>
          <span class="sub">一集同时只允许一条未结束的运行；进度与状态都现算，不落库</span>
          <div class="right">
            ${canSubmit ? '<button class="btn sm primary" data-new-run>发起运行</button>'
              : '<span class="t-desc">发起运行需要 job:submit</span>'}
          </div></div>
        ${list.length ? `<div class="table-wrap"><table>
          <thead><tr><th>集</th><th>模板</th><th>状态</th><th>进度</th>
            <th class="r">开始</th><th class="r"></th></tr></thead>
          <tbody>${rows}</tbody></table></div>` : `<div class="empty">
          <div class="empty-t">还没有运行</div>
          <p>选一份模板对某一集发起运行：读项目、查分镜、出关键帧、出镜头视频、等人签字，
            按依赖自己走。生成节点仍然走平台默认的模型轨。</p>
          ${canSubmit ? '<div class="empty-form"><button class="btn primary" data-new-run>发起运行</button></div>' : ''}
        </div>`}
      </div>
      ${openRunId ? '<div id="pl-detail-host"><div class="card mt-card" ><div class="card-body"><p class="hint">正在读运行…</p></div></div></div>' : ''}
      ${createModal(episodes, templates.filter((t) => t.enabled !== false), busy)}
    </section>`;
  },

  async mount(root) {
    if (!store.current) return;
    const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
    bindSubChips(root, (i) => { setSub('pipeline', i); refresh(); });

    if (currentSubTab('pipeline', TABS) === 2) {
      bindSeasons(root, refresh);
      return;
    }

    const detailHost = root.querySelector('#pl-detail-host');
    // 错误卡片自带的「重试」：只重画这一页，不重新发起任何动作
    root.querySelectorAll('.empty.err [data-retry]').forEach((btn) => {
      btn.addEventListener('click', refresh);
    });
    if (detailHost && openRunId) {
      try {
        const detail = await api.pipelineRun(openRunId);
        // 详情是异步读的：读回来时人可能已经切走或换了项目
        if (!detailHost.isConnected) return;
        // 事件中文名来自服务端（见 `eventLabel` 上面那段）。先收进模块状态再画，
        // 因为 `eventText()` 在渲染过程中就要用；老后端没带就保留上一次的。
        if (detail.event_labels) eventLabels = detail.event_labels;
        detailHost.innerHTML = detailHtml(detail);
      } catch (e) {
        if (!detailHost.isConnected) return;
        detailHost.innerHTML = errorBox('这次运行', e);
        detailHost.querySelector('[data-retry]')?.addEventListener('click', refresh);
      }
    }

    root.querySelectorAll('[data-open-run]').forEach((btn) => {
      btn.addEventListener('click', () => {
        openRunId = Number(btn.dataset.openRun);
        refresh();
      });
    });

    // 「去生成中心／去审核」只是跳转：看成本、日志、产物与签字都在东家模块
    root.querySelectorAll('[data-go-generate]').forEach((btn) => {
      btn.addEventListener('click', () => goto('generate', 0));
    });
    root.querySelectorAll('[data-go-review]').forEach((btn) => {
      btn.addEventListener('click', () => goto('review', 1));
    });

    const act = async (label, fn, okText) => {
      if (!(await run(label, fn))) return;
      notice = okText; noticeKey = `${store.current.id}:${currentSubTab('pipeline', TABS)}`;
      refresh();
    };
    root.querySelector('[data-advance]')?.addEventListener('click', () =>
      act('推进', () => api.advancePipelineRun(openRunId), '推进了一轮；worker 的节拍也会自己往前走。'));
    root.querySelector('[data-pause]')?.addEventListener('click', (e) => {
      const pausing = e.currentTarget.textContent.trim() === '暂停';
      return act(pausing ? '暂停' : '恢复',
        () => (pausing ? api.pausePipelineRun(openRunId) : api.resumePipelineRun(openRunId)),
        pausing ? '已暂停：不再派新节点，跑着的作业会跑完。' : '已恢复：节拍继续往下推进。');
    });
    root.querySelector('[data-cancel]')?.addEventListener('click', async () => {
      if (!(await confirmDanger({
        title: '取消这条运行',
        body: '未开始的节点会收成已取消；能安全取消的作业会发取消请求，'
          + '已经完成的节点与已生成的产物<b>一个都不动</b>。',
        okText: '取消运行',
      }))) return;
      await act('取消', () => api.cancelPipelineRun(openRunId, '在产线中心取消'), '这条运行已取消。');
    });
    root.querySelectorAll('[data-retry-node]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        if (!(await confirmDanger({
          title: '只重试这一格',
          body: '旧作业、旧产物、旧签字都留着，新作业另起一行；参数原样带过去，'
            + '原因没除掉就还会再失败一次。',
          okText: '重试',
          danger: false,
        }))) return;
        await act('重试', () => api.retryPipelineNode(Number(btn.dataset.retryNode)),
          '这一格已重试，新作业另起一行。');
      });
    });

    // ---- 发起运行弹窗 ----
    const modal = root.querySelector('#pl-modal');
    if (!modal) return;
    let idemKey = '';
    const openModal = () => {
      const err = modal.querySelector('#pl-modal-err');
      err.hidden = true;
      idemKey = (crypto.randomUUID?.() ?? `pl-${Date.now()}-${Math.random().toString(36).slice(2)}`);
      modal.hidden = false;
      okBtn.disabled = false;
      modal.querySelector('#pl-ep')?.focus();
      checkPreflight();
    };
    const closeModal = () => { modal.hidden = true; };
    const preBox = modal.querySelector('#pl-pre');
    const okBtn = modal.querySelector('[data-pl-ok]');
    const checkPreflight = async () => {
      const episodeId = Number(modal.querySelector('#pl-ep').value);
      const templateId = Number(modal.querySelector('#pl-tpl').value);
      const req = ++preReqId;      // 连改两次集／模板：旧响应不许盖掉新的一次
      if (!episodeId || !templateId) { preBox.innerHTML = ''; okBtn.disabled = false; return; }
      preBox.innerHTML = '<p class="hint">正在做生产前检查…</p>';
      try {
        // 走统一预检（`GET /api/projects/{p}/precheck`）：它内部就是 `pipeline.preflight`，
        // 但翻成了三档条目并带上跳转——于是发起弹窗与工作台看到的是同一张清单，
        // 不会再出现"弹窗说能发起、点下去吃 422"那种界面骗人的事。
        const data = await api.precheckEpisode(store.current.id, episodeId, templateId);
        if (req !== preReqId || !preBox.isConnected) return;
        preBox.innerHTML = preflightHtml(data);
        // 禁不禁只认服务端那句 `can_start`（= 一条阻塞都没有），页面不自己数
        okBtn.disabled = data.can_start === false;
      } catch (e) {
        if (req !== preReqId || !preBox.isConnected) return;
        // 读不到预检不替人做决定：照常允许点，服务端发起时还会再挡一次
        preBox.innerHTML = `<p class="pl-err">读不到预检结果：${escapeHtml(e.message || e)}</p>`;
        okBtn.disabled = false;
      }
    };
    // 清单里的「去这一镜／去处理」：先收弹窗再跳，不然人落在目标页上还被一层蒙着
    modal.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-goto]');
      if (!btn) return;
      let entry;
      try { entry = JSON.parse(btn.dataset.goto); } catch { return; }
      closeModal();
      gotoEntry(entry);
    });
    modal.querySelector('#pl-ep')?.addEventListener('change', checkPreflight);
    modal.querySelector('#pl-tpl')?.addEventListener('change', checkPreflight);
    bindModalDismiss(modal, closeModal);
    modal.querySelector('[data-pl-cancel]').addEventListener('click', closeModal);
    modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
    root.querySelectorAll('[data-new-run]').forEach((b) => b.addEventListener('click', openModal));
    modal.querySelector('[data-pl-ok]').addEventListener('click', async () => {
      const err = modal.querySelector('#pl-modal-err');
      const episodeId = Number(modal.querySelector('#pl-ep').value);
      const templateId = Number(modal.querySelector('#pl-tpl').value);
      if (!episodeId || !templateId) {
        err.textContent = '先选一集、再选一个模板。';
        err.hidden = false;
        return;
      }
      try {
        const view = await api.createPipelineRun(store.current.id, {
          episode_id: episodeId, template_id: templateId, idempotency_key: idemKey,
        });
        // 发起成功后连点同一个键会拿回同一条运行，而不是排第二套作业
        idemKey = '';
        openRunId = view.id;
        closeModal();
      } catch (e) {
        // 409／422 的原话就在弹窗里说，别让人关了弹窗再猜
        err.textContent = e.message || '没发起成功。';
        err.hidden = false;
        return;
      }
      notice = '运行已发起，worker 的节拍会按依赖往下走。';
      noticeKey = `${store.current.id}:0`;
      refresh();
    });
  },
};
