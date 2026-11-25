import { chatgptPlanCard, mountChatGPTPlan } from './chatgpt-plan.js';
import { submissionIdentity } from '../submission.js';
/**
 * 智能体中心（0041／《智能体管理中心》说明书）。三个工作区：
 *   0 总览        运行概况 ＋ 快捷制片任务 ＋ 近期编排任务
 *   1 智能体配置  本地／云端规划推理通道 ＋ 授权工具能力目录（附场景调用策略直达）
 *   2 运行与审批  项目编排任务列表、行内步骤详情、事件时间线及待审批追踪
 *
 * 三条边界，都是为了让这一页不长成第二个模型中心、第二个审核中心：
 *
 * 1. **配置只引用不复制**。这一页没有"新建智能体配置"那回事：模型在哪、
 *    能不能跑、什么价，唯一出处在模型中心那三张表。这里给的是跳转，
 *    不是第二个表单（取证与裁定见《文档/开发/智能体管理/00-接入前现状审计.md》§7）。
 * 2. **签字不在这里**。待审批只显示状态与条数，按钮把人送去审核中心——
 *    不变量 3：Review 只有一个写入口。
 * 3. **数字只念不算**。四个数、健康、成本全来自服务端；前端算的只有
 *    "这一屏筛出几行"这种纯视图数（同模型中心第 3 条）。
 */
import { api } from '../api.js';
import { gotoEntry } from '../goto.js';
import { store } from '../store.js';
import { getParam, getSub, setParam, setSub, takeAgentPrefill } from '../navstate.js';
import {
  agoText, bindModalDismiss, bindSubChips, escapeHtml, errorBox, run, skeleton, subChipRow,
} from '../util.js';

const SUBS = ['总览', '智能体配置', '运行与审批'];
const currentSub = () => {
  const raw = getSub('agents') ?? 0;
  return raw >= 0 && raw < SUBS.length ? raw : 0;
};

const RUN_TONE = {
  planning: 'gray', awaiting_approval: 'amber', running: 'blue',
  paused: 'gray', cancelled: 'gray', completed: 'green', failed: 'red',
};
const STEP_TONE = {
  pending: 'gray', awaiting_approval: 'amber', approved: 'blue',
  running: 'blue', succeeded: 'green', failed: 'red', skipped: 'gray',
  cancelled: 'gray', expired: 'amber',
};
const HEALTH_TONE = {
  ok: 'green', missing: 'red', unknown: 'amber', unconfigured: 'gray',
  unregistered: 'red',
};
/**
 * 事件类型的中文名**不在这里**：它由服务端随响应下发（`agents.EVENT_LABEL`，
 * 详情与列表都带 `event_labels` 这个键）。
 *
 * 这一段原来是一份 23 档的手抄字典，与库里 `agent_event.event_type` 的 CHECK 各写一份，
 * 中间没有任何守卫——清单加一档，界面会安静地少说一句"发生了什么"，
 * 而那一句恰恰是事件时间线唯一的内容。这类"同一枚举两份定义"是本仓库反复在修的形状
 * （《写作与引用规范》§2），所以标签收回服务端。
 *
 * `eventLabel()` 保留一层兜底：万一响应没带（老版本后端），原样显示事件码，
 * 而不是显示空白——空白的下一步永远是"这是不是坏了"。
 */
function eventLabel(eventType) {
  return (cache.eventLabels || {})[eventType] || eventType;
}

// 一次 render 把这一屏要读的几份缓存下来；mount 里的交互不重新发请求，
// 免得点一下筛选就把整页读请求打一遍（同模型中心的做法）
let cache = { overview: null, configs: null, tools: null,
              // 事件中文名来自服务端（见 `eventLabel` 上面那段），不在这里手抄。
              eventLabels: {} };
let openRunId = null;

const chip = (tone, text) => `<span class="chip ${tone}">${escapeHtml(text)}</span>`;
const stat = (value, label, warn = false) =>
  `<div class="stat ${warn ? 'warn' : ''}"><span class="stat-n">${value}</span>
   <span class="stat-l">${escapeHtml(label)}</span></div>`;

/** 与模型中心同款的问号悬浮提示，把长口径与操作说明收进气泡，保持标题栏清爽。 */
function helpTip(title, desc, bullets = [], lg = false) {
  const list = bullets.length
    ? `<ul class="tip-list">${bullets.map((b) => `<li>${b}</li>`).join('')}</ul>`
    : '';
  return `<span class="help-tip${lg ? ' lg' : ''}">
    <span class="help-icon">?</span>
    <span class="tip-pop tip-right">
      <span class="tip-title">${escapeHtml(title)}</span>
      ${desc ? `<div class="tip-desc">${escapeHtml(desc)}</div>` : ''}
      ${list}
    </span>
  </span>`;
}

/**
 * 面向漫剧生产的快捷编排任务模板（P1 专业化升级，2026-11-24）。
 * 每条模板严格对应当前后端工具白名单里已落地的能力，点击直接打开「新建编排任务」
 * 并预填目标，实时展示将走的推理通道与计价口径，不绕过任何人工确认。
 */
const QUICK_TEMPLATES = [
  {
    id: 'preflight',
    title: '单集备料与缺口巡检',
    badge: '只读预检 · 零费用',
    tone: 'green',
    desc: '盘点指定集缺失的角色、场景、道具及产线阻塞项，生成生产准备报告。',
    goal: '盘点当前集缺少哪些角色、场景与道具资产，列出所有会阻塞产线的准备项。',
  },
  {
    id: 'shot-audit',
    title: '分镜进度与异常镜头巡检',
    badge: '只读巡检 · 零费用',
    tone: 'green',
    desc: '巡检单集九阶段进度，汇总缺参考图、生成失败、待返工与待审镜头清单。',
    goal: '巡检当前集的分镜生产进度，汇总缺参考图、生成失败与待返工的异常镜头清单。',
  },
  {
    id: 'audio-audit',
    title: '对白与配音缺口巡检',
    badge: '只读巡检 · 零费用',
    tone: 'green',
    desc: '识别单集含台词但尚未生成配音的分镜，统计音频片段状态与缺失说话人。',
    goal: '巡检当前集所有带台词的分镜，列出尚未建立或生成配音的对白缺口与音频状态统计。',
  },
  {
    id: 'consistency-scan',
    title: '项目设定与资产矛盾扫描',
    badge: '审阅写入 · 需审批',
    tone: 'amber',
    desc: '全项目扫描资产别名冲突、分镜漏绑与设定矛盾，写入设定中心审阅列表。',
    goal: '对当前项目执行设定与资产一致性矛盾扫描，并将新发现的矛盾项写入设定中心审阅列表。',
  },
  {
    id: 'asset-register',
    title: '缺口角色识别与资产建档',
    badge: '正式写入 · 需审批',
    tone: 'amber',
    desc: '检查单集分镜缺口，为缺失角色起草正式资产登记单并送审入库。',
    goal: '盘点当前集缺失的角色资产，并为缺失角色起草正式资产登记单等待审核中心批准。',
  },
  {
    id: 'char-bio',
    title: '缺口角色履历与设定扩写',
    badge: '文本作业 · 需确认',
    tone: 'blue',
    desc: '结合单集备料缺口，提交文本生成作业起草角色人物小传与一致性描述。',
    goal: '盘点当前集缺哪些角色资产，并提交文本生成作业为缺失角色起草人物履历与视觉设定描述。',
  },
];

function quickTemplatesHtml() {
  const canSubmit = store.can('job:submit');
  return `<div class="card mb-card ag-tpl-card">
    <div class="card-head">
      <h2>快捷制片任务</h2>
      ${helpTip(
        '快捷制片任务说明',
        '按漫剧常用场景预置目标，点击即可带入编排弹窗预览路线与计价。',
        [
          '<b>绿色只读</b>：不改业务库、不产生文本生成费用，计划生成后自动执行。',
          '<b>黄色写入</b>：写库或扫描入库前自动暂停，须在「审核中心 → 动作审批」签字放行。',
          '<b>蓝色作业</b>：提交文本生成作业进生成中心队列，走云端时需人工勾选计费确认。',
        ],
      )}
      ${canSubmit ? '<div class="right"><button class="btn primary sm" data-ag-new>新建编排任务</button></div>' : ''}
    </div>
    <div class="card-body">
      <div class="ag-tpl-grid">
        ${QUICK_TEMPLATES.map((t) => `<button type="button" class="ag-tpl-item"
          data-ag-tpl="${escapeHtml(t.id)}"${canSubmit ? '' : ' disabled title="需要作业提交权限（job:submit）"'}>
          <div class="ag-tpl-head">
            <b>${escapeHtml(t.title)}</b>
            ${chip(t.tone, t.badge)}
          </div>
          <p class="t-desc">${escapeHtml(t.desc)}</p>
        </button>`).join('')}
      </div>
    </div>
  </div>`;
}

const HEALTH_SHORT = {
  ok: '可用', missing: '未就绪', unknown: '待探测', unconfigured: '未配置',
  unregistered: '未登记',
};

/** 规划推理通道摘要：表内只放标准短态与密钥计价，长说明收进 title 与卡片问号气泡。 */
function legsHtml(configs) {
  if (!configs || !configs.cards) {
    return '<p class="hint">推理通道详细状态需要系统配置权限；当前角色可查看自己的任务记录。</p>';
  }
  const shortResident = (text) => {
    if (!text) return '—';
    const idx = text.indexOf('（');
    return idx > 0 ? text.slice(0, idx) : text;
  };
  const rows = configs.cards.map((c) => {
    const secretText = c.key_env
      ? `${c.key_env}（${c.secret_present ? '已配置' : '未配置'}）`
      : '无需密钥';
    const costShort = !c.cost_note ? '—'
      : c.cost_note.includes('不计费') ? '本地不计费'
      : c.cost_note.split('：').slice(-1)[0].trim();
    return `<tr>
      <td class="ag-nowrap"><b>${escapeHtml(c.backend_label)}</b></td>
      <td>${c.configured ? escapeHtml([c.provider, c.model].filter(Boolean).join(' / '))
        : '<span class="t-desc">未配置默认模型</span>'}</td>
      <td><span title="${escapeHtml(c.health_detail || '未检查')}">${chip(HEALTH_TONE[c.health_state] || 'gray',
        HEALTH_SHORT[c.health_state] || '待探测')}</span></td>
      <td><span class="t-desc">${escapeHtml(c.endpoint || '—')}</span></td>
      <td><span class="t-desc" title="${escapeHtml(c.resident || '')}">${escapeHtml(shortResident(c.resident))}</span></td>
      <td><div class="ag-cred">${escapeHtml(secretText)}</div>
        <div class="t-desc" title="${escapeHtml(c.cost_note || '')}">${escapeHtml(costShort)}</div></td>
      <td class="ag-act-col"><button class="btn sm ag-nowrap" data-goto-models="${escapeHtml(c.backend)}"
        >前往模型中心</button></td>
    </tr>`;
  }).join('');
  return `<div class="table-wrap"><table class="ag-legs-table">
    <thead><tr><th>推理通道</th><th>已配置模型</th><th>健康状态</th><th>服务端点</th>
      <th>驻留状态</th><th>密钥与计价</th><th class="ag-act-col">操作</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

/**
 * 运行表。`inlineDetail` 为真时（运行与审批页），被点开那一行下面紧跟一行详情；
 * 总览没有详情位，按钮直接把人带到「运行与审批」并展开那一条——
 * 原来总览也画「详情」按钮却没有 `#ag-run-detail` 容器，点了只会把字变成「收起」。
 */
function runsTable(runs, { inlineDetail = false } = {}) {
  if (!runs || !runs.length) {
    return `<div class="empty"><div class="empty-t">暂无编排任务记录</div>
      <p class="hint">${inlineDetail
        ? '点击右上角「新建编排任务」或展开上方 ChatGPT 草案即可发起'
        : '选择上方快捷制片模板或点击「新建编排任务」即可发起'}</p></div>`;
  }
  return `<div class="table-wrap"><table>
    <thead><tr><th class="num">#</th><th>任务目标</th><th>状态</th><th class="num">步骤进度</th>
      <th>推理通道</th><th class="num">发起时间</th><th class="r"></th></tr></thead>
    <tbody>${runs.map((r) => {
      const snap = r.model_snapshot || {};
      const done = r.steps.filter((s) => s.status === 'succeeded').length;
      const waiting = r.step_counts.awaiting_approval || 0;
      const stamp = (r.created_at || '').replace(' ', 'T');
      const open = inlineDetail && r.id === openRunId;
      return `<tr class="${open ? 'sel' : ''}">
        <td class="num">${r.id}</td>
        <td><b>${escapeHtml(r.goal)}</b>
          ${r.episode_id ? `<span class="t-desc">集 #${r.episode_id}</span>` : ''}</td>
        <td>${chip(RUN_TONE[r.status] || 'gray', r.status_label)}
          ${waiting ? `<span class="t-desc">等 ${waiting} 步</span>` : ''}</td>
        <td class="num">${r.steps.length ? `${done}/${r.steps.length}` : '—'}</td>
        <td><span class="t-desc">${escapeHtml([snap.backend, snap.provider, snap.model]
          .filter(Boolean).join(' / ') || '—')}</span></td>
        <td class="num"><span class="t-desc">${stamp ? escapeHtml(agoText(Math.round(
          Date.now() / 1000 - new Date(stamp).getTime() / 1000))) : '—'}</span></td>
        <td class="r"><button class="btn sm ${open ? '' : 'primary'}"
          data-open-run="${r.id}">${inlineDetail ? (open ? '收起' : '详情') : '详情 →'}</button></td>
      </tr>${open ? `<tr class="ag-detail-row"><td colspan="7">
        <div id="ag-run-detail" class="card"><div class="card-body">${skeleton(3, 'text')}</div></div>
      </td></tr>` : ''}`;
    }).join('')}</tbody></table>`;
}

function eventRows(events) {
  if (!events || !events.length) return '<p class="hint">还没有事件。</p>';
  return `<div class="table-wrap"><table>
    <thead><tr><th class="num">时间</th><th>事件</th><th>说明</th></tr></thead>
    <tbody>${events.slice(0, 20).map((e) => `<tr>
      <td class="num"><span class="t-desc">${escapeHtml((e.created_at || '').slice(5, 16))}</span></td>
      <td>${chip('gray', eventLabel(e.event_type))}</td>
      <td><span class="t-desc">${escapeHtml(eventText(e))}</span></td></tr>`).join('')}</tbody>
    </table></div>`;
}

function eventText(ev) {
  const p = ev.payload || {};
  switch (ev.event_type) {
    case 'run_created': return `目标「${String(p.goal || '').slice(0, 40)}」`;
    case 'plan_requested': return `排队作业 #${p.job_id}（提示词 ${p.chars || 0} 字）`;
    case 'plan_ready': return `${p.steps} 步：${(p.tools || []).join(' → ')}`;
    case 'approval_requested': return `第 ${p.seq} 步 ${p.tool_code || ''}·风险 ${p.risk || ''}`;
    case 'job_submitted': return `第 ${p.seq} 步排入作业 #${p.job_id}`;
    case 'step_succeeded': return `第 ${p.seq} 步 ${p.tool_code || ('作业 #' + p.job_id) || ''}`;
    case 'step_failed': return `第 ${p.seq} 步失败：${p.code || ''}`;
    case 'run_cancelled': return p.voided_steps ? `同时作废 ${p.voided_steps} 步` : '';
    case 'run_failed': return `${p.code || ''}：${String(p.message || '').slice(0, 60)}`;
    default: return JSON.stringify(p).slice(0, 90);
  }
}

/** 运行详情：步骤（关联作业状态现读）＋ 事件 ＋ 三个意愿按钮。 */
function detailHtml(d) {
  const canCancel = store.can('job:cancel');
  const canSign = store.can('review:sign');
  const steps = d.steps.map((s) => `<tr>
    <td class="num">${s.seq}</td>
    <td><b>${escapeHtml(s.tool_label)}</b>
      <span class="t-desc">${escapeHtml(s.args_preview || '')}</span></td>
    <td>${chip('gray', s.risk_label)}</td>
    <td>${chip(STEP_TONE[s.status] || 'gray', s.status_label)}</td>
    <td>${s.job_id
      ? `<button class="btn xs" data-goto-job="${s.job_id}">作业 #${s.job_id}</button>
         <span class="t-desc">${escapeHtml(s.job_status_label || '')}</span>`
      : '<span class="t-desc">—</span>'}</td>
    <td><span class="t-desc">${s.error_code
      ? `<span class="t-bad">${escapeHtml(s.error_code)}</span> ${escapeHtml(s.error_message || '')}`
      : '—'}</span></td>
  </tr>`).join('');
  const snap = d.model_snapshot || {};
  return `
    <div class="card-head"><h2>运行 #${d.id}：${escapeHtml(d.status_label)}</h2>
      <span class="sub">${escapeHtml(d.goal)}</span></div>
    <div class="card-body">
      <p class="modal-meta">计划：${d.steps.length ? `${d.steps.length} 步` : '还在算'}
        ｜走的轨：${escapeHtml([snap.backend, snap.provider, snap.model]
          .filter(Boolean).join(' / ') || '—')}
        ｜审批有效期：${Math.round((d.approval_valid_seconds || 0) / 3600)} 小时</p>
      ${d.failure ? `<p class="bar bad">${escapeHtml(d.failure.where)}：
        ${escapeHtml(d.failure.code || '')} ${escapeHtml(d.failure.message || '')}</p>` : ''}
      ${d.plan_job ? `<p class="hint">算计划那条作业：#${d.plan_job.id}
        ${escapeHtml(d.plan_job.status_label)}
        <button class="btn xs" data-goto-job="${d.plan_job.id}">去生成中心看</button>
        （状态、成本与日志都在那一页，智能体表里不复制这些事实）</p>` : ''}
      ${d.steps.length ? `<div class="table-wrap"><table>
        <thead><tr><th class="num">步</th><th>工具与参数</th><th>风险</th><th>状态</th>
          <th>关联作业</th><th>失败原因</th></tr></thead>
        <tbody>${steps}</tbody></table></div>`
        : `<p class="hint">${d.status === 'planning'
          ? '计划还没回来。它现在就是一条排在生成中心队列里的文本作业——刷新或稍后再看为准。'
          : '没有步骤。'}</p>`}
      ${d.steps.some((s) => s.status === 'awaiting_approval')
        ? `<p class="bar">有步骤在等人批。<b>这一页不能签</b>——批准与驳回只落在审核中心。
          <button class="btn sm ${canSign ? 'primary' : ''}" data-goto-review="2"
            ${canSign ? '' : 'disabled title="需要 review:sign"'}>去审核中心</button></p>` : ''}
      <div class="row u-gap-8 u-mt-12">
        ${canCancel && !d.cancelled ? `<button class="btn sm" data-run-pause="${d.id}"
          ${d.paused ? 'disabled' : ''}>暂停</button>
          <button class="btn sm" data-run-resume="${d.id}"
          ${d.paused ? '' : 'disabled'}>恢复</button>
          <button class="btn sm" data-run-cancel="${d.id}">取消</button>` : ''}
        ${d.cancelled ? `<span class="t-desc">已取消：${escapeHtml(d.cancel_reason || '')}
          （取消不能恢复：那批待审步骤已经同时作废了）</span>` : ''}
      </div>
      ${eventRows(d.events)}
    </div>`;
}

/** 发起弹窗：先把"走哪条轨、送什么出去、什么价"念出来，再要那句目标。 */
function createModal(episodes, policies, options) {
  const route = (options && options.route) || {};
  // 有效期档位与默认档都来自接口（`/api/agent/plan-options`）。
  // 这里原来写死成一条固定档（秒数取 86400、标签取「24 小时」）：接口明明给了四档，
  // 弹窗只显示一档——那正是《系统设计说明书》§16 第 28 条登记的那处
  // 「界面上的承诺比代码少一步」。
  const choices = (options && options.approval_choices) || [];
  const defaultValid = (options && options.default_valid_seconds) || null;
  const confirmLine = `<span id="ag-cloud-line" hidden>
      <label class="modal-label u-check-row" for="ag-cloud">
        <input type="checkbox" id="ag-cloud" class="u-check-box">
        <span>确认用云端算这次计划：<b id="ag-cloud-what"></b><br>
          <span class="t-desc" id="ag-cloud-cost"></span>
          平台不会替你勾这一格，也不会因为本地失败就自己换到云端。</span></label></span>`;
  return `<div class="modal" id="ag-modal" hidden role="dialog" aria-modal="true"
    aria-label="新建编排任务">
    <div class="modal-box">
      <div class="modal-head">新建编排任务</div>
      <div class="modal-body">
        <label class="modal-label" for="ag-goal">任务目标（请简明描述要完成的制片工作）</label>
        <textarea class="ctl" id="ag-goal" rows="3"
          placeholder="例：盘点第 1 集缺哪些资产，并为每个缺的角色生成一段人物履历"></textarea>
        <label class="modal-label u-mt-12" for="ag-ep">作用范围（集，留空则作用于整个项目）</label>
        <select class="ctl" id="ag-ep">
          <option value="">（整个项目）</option>
          ${episodes.map((e) => `<option value="${e.id}">第 ${e.seq} 集${
            e.title ? ' · ' + escapeHtml(e.title) : ''}</option>`).join('')}
        </select>
        <label class="modal-label u-mt-12" for="ag-scene">匹配生产场景</label>
        <select class="ctl" id="ag-scene">
          <option value="">（不指定场景：使用文本能力默认推理通道）</option>
          ${policies.map((p) => `<option value="${escapeHtml(p.scene_key)}">${
            escapeHtml(p.scene_label)}${p.enabled ? '' : '（待配置）'}</option>`).join('')}
        </select>
        <label class="modal-label u-mt-12" for="ag-valid">审批有效期</label>
        <select class="ctl" id="ag-valid">
          ${choices.map((c) => `<option value="${c.seconds}"${
            c.seconds === defaultValid ? ' selected' : ''}>${escapeHtml(c.label)}</option>`).join('')}
        </select>
        <p class="modal-meta" id="ag-route">正在读取推理通道与计价口径…</p>
        ${confirmLine}
        <p class="modal-meta">提交后将先创建一条<b>任务规划文本作业</b>（统一进入生成中心队列）。
          计划生成后：只读步骤自动执行；涉及付费调用或正式业务写入的步骤将自动暂停，等待审核中心签字放行。</p>
        <p class="bar bad" id="ag-modal-err" hidden></p>
      </div>
      <div class="modal-actions">
        <button class="btn" data-ag-close>取消</button>
        <button class="btn primary" data-ag-submit>发起任务</button>
      </div>
    </div>
  </div>`;
}

export default {
  subIndex: 0,

  async render() {
    const sub = currentSub();
    this.subIndex = sub;
    const project = store.current;
    const head = (line) => `<div class="page-head">
        <div class="page-title-wrap">
          <h1>智能体中心</h1>
          ${helpTip(
            '智能体中心定位与安全边界',
            line,
            [
              '<b>配置只引用不复制</b>：推理通道与模型唯一出处在「模型中心」，本页只读引用。',
              '<b>白名单工具与审批护栏</b>：只读步骤自动完成；写库或付费动作一律暂停并送「审核中心」签字。',
              '<b>跨模块联动</b>：支持从总览快捷卡，或项目工作台 / 音频中心 / 设定中心一键带入上下文发起。',
            ],
            true,
          )}
        </div>
        <div class="row u-gap-6">${subChipRow(SUBS, sub)}</div>
      </div>`;

    if (!project) {
      return `<section class="view on">${head('先选一个项目')}
        <div class="card"><div class="empty"><div class="empty-t">还没有项目</div>
        <p>智能体的每一次运行都属于一个项目：先去项目中心建一部作品。</p></div></div></section>`;
    }

    if (sub === 1) {
      // 直接进 #agents/1（深链、刷新、别人发的地址）时不能指望"总览那一屏已经读过数据"：
      // 缓存是空的，页面就会老实显示 0 行——验收当场抓到过一次。
      if (!cache.configs || !cache.tools) {
        try {
          [cache.configs, cache.tools] = await Promise.all([
            api.agentConfigs(), api.agentTools(),
          ]);
        } catch (e) {
          return `<section class="view on">${head('推理通道状态与授权工具能力目录')}${errorBox('读配置', e)}</section>`;
        }
      }
      return `<section class="view on">${head('推理通道状态与授权工具能力目录')}${this.configPane()}</section>`;
    }
    if (sub === 2) return `<section class="view on">${head('项目编排任务执行、步骤时间线与待审批追踪')}${await this.runsPane(project)}</section>`;

    let err = null;
    let overview = null;
    try {
      if (store.can('system:config')) {
        overview = await api.agentOverview();
      } else {
        // 制作角色看不到配置面。概览那一条也需要 system:config，
        // 所以这一屏改读"这个项目的运行"——少一个数就少显示一个，不编
        cache.configs = null; cache.tools = null;
        overview = { runs: (await api.agentRuns(project.id)).items, counts: null };
      }
      cache.overview = overview;
    } catch (e) { err = e; }

    const counts = (overview && overview.counts) || {};
    return `<section class="view on">
      ${head('跨模块任务规划、自动化执行与人工审批调度')}
      ${err ? `<div class="card">${errorBox('读概览', err)}</div>` : ''}
      ${counts.runs !== undefined ? `<div class="card mb-card" >
        <div class="card-head"><h2>运行概况</h2>
          ${helpTip('统计口径说明', (overview && overview.caliber) || '')}</div>
        <div class="proj-stats ag-stats">
          ${stat(counts.runs ?? '—', '累计任务')}
          ${stat(counts.active ?? '—', '执行中')}
          ${stat(counts.awaiting_approval ?? '—', '待人工审批',
            !!counts.awaiting_approval)}
          ${stat(`${counts.usable_configs ?? '—'}/${counts.total_configs ?? '—'}`, '可用推理通道')}
        </div></div>` : ''}
      ${quickTemplatesHtml()}
      <div class="card">
        <div class="card-head"><h2>近期编排任务</h2>
          ${helpTip(
            '任务执行与审批规则',
            '点击「详情 →」前往运行与审批查看步骤与事件；所有写操作均须经审核中心签字。',
            [
              '<b>自动与人工分工</b>：提交后先规划步骤，只读步骤自动执行；付费调用或正式写库步骤自动暂停等待审核中心签字。',
              '<b>成员权限范围</b>：具作业提交权限（<code>job:submit</code>）的成员可发起任务，项目成员均可追踪进度与审批状态。',
            ],
          )}</div>
        ${runsTable(overview ? overview.runs : [])}
      </div>
      <div id="ag-modal-slot"></div>
    </section>`;
  },

  /** 配置页：没配置权限时不装样子，直接说明去哪一栏能看见。 */
  configPane() {
    if (!store.can('system:config')) {
      return `<div class="card"><div class="empty">
        <div class="empty-t">此页面需要系统配置权限</div>
        <p>智能体推理通道直接引用「模型中心」的默认轨配置，无独立副本。</p>
        <p class="hint">你仍可在「运行与审批」中查看和追踪自己发起的任务。</p></div></div>`;
    }
    const tools = Object.entries((cache.tools && cache.tools.items) || {});
    return `
      <div class="card mb-card" >
        <div class="card-head"><h2>规划推理通道（本地 / 云端）</h2>
          ${helpTip(
            '通道与状态口径说明',
            '统一引用模型中心配置；本页只读，进入时实时检查通道健康。',
            [
              escapeHtml((cache.configs && cache.configs.caliber) || ''),
              '<b>凭据安全</b>：密钥仅从系统环境变量或加密密钥库读取，永不入库、不回显。',
            ].filter(Boolean),
          )}
          <div class="right"><button class="btn sm" data-goto-models="policy">场景调用策略 →</button></div>
        </div>
        ${legsHtml(cache.configs)}
      </div>
      <div class="card">
        <div class="card-head"><h2>授权工具能力目录</h2>
          ${helpTip(
            '工具白名单与风控边界',
            '智能体仅允许调用下列白名单工具，按风险等级自动执行或拦截送审。',
            [
              escapeHtml((cache.tools && cache.tools.note) || ''),
              '<b>人工审批门禁</b>：风险等级为写操作 / 付费作业 / 正式变更时，执行前自动暂停并送审核中心签字。',
            ].filter(Boolean),
          )}</div>
        <div class="table-wrap"><table class="ag-tools-table">
          <thead><tr><th>工具标识</th><th>能力说明</th><th>风险等级</th><th>数据外发范围</th>
            <th>验收标准</th></tr></thead>
          <tbody>${tools.map(([code, t]) => `<tr>
            <td><code>${escapeHtml(code)}</code></td>
            <td>${escapeHtml(t.label)}</td>
            <td>${chip('gray', t.risk_label)}</td>
            <td><span class="t-desc">${escapeHtml(t.data_out)}</span></td>
            <td><span class="t-desc">${escapeHtml(t.verify)}</span></td></tr>`).join('')}
          </tbody></table></div>
        <div class="card-foot">
          <p class="modal-meta">安全基线：${escapeHtml((cache.tools && cache.tools.forbidden) || '')}
            ｜单次任务步骤上限 ${escapeHtml(String((cache.tools && cache.tools.max_plan_steps) ?? '—'))} 步</p>
        </div>
      </div>`;
  },

  async runsPane(project) {
    let runs = [];
    let waiting = [];
    let err = null;
    try {
      runs = (await api.agentRuns(project.id)).items || [];
      waiting = store.can('review:read') ? ((await api.agentPending()).items || []) : [];
    } catch (e) { err = e; }
    if (openRunId && !runs.some((r) => r.id === openRunId)) openRunId = getParam('agents') ? openRunId : null;
    // 打开的那条不在本项目列表里（从总览或别人发来的深链进来）时，
    // 没有可以"贴着展开"的那一行，退回到列表下面单独一张详情卡。
    const orphanDetail = openRunId && !runs.some((r) => r.id === openRunId);
    return `
      ${chatgptPlanCard()}
      ${err ? `<div class="card mb-card" >${errorBox('读运行列表', err)}</div>` : ''}
      <div class="card mb-card" >
        <div class="card-head"><h2>项目编排任务列表</h2>
          <span class="sub">查看任务计划、步骤与事件流；高风险动作请在审核中心签字</span>
          <div class="right"><button class="btn primary sm" data-ag-new>新建编排任务</button></div></div>
        ${runsTable(runs, { inlineDetail: true })}
      </div>
      ${orphanDetail ? '<div id="ag-run-detail" class="card mb-card"><div class="card-body">'
        + skeleton(3, 'text') + '</div></div>' : ''}
      ${waiting.length ? `<div class="card mb-card" >
        <div class="card-head"><h2>待人工审批动作（${waiting.length} 步）</h2>
          <span class="sub">仅展示审批队列状态，批准与驳回统一在审核中心执行</span></div>
        <div class="table-wrap"><table>
          <thead><tr><th class="num">步</th><th>工具</th><th>目标</th><th>风险</th>
            <th class="num">有效期到</th><th class="r"></th></tr></thead>
          <tbody>${waiting.map((s) => `<tr>
            <td class="num">${s.seq}</td><td>${escapeHtml(s.tool_label)}</td>
            <td><span class="t-desc">${escapeHtml(s.goal)}</span></td>
            <td>${chip('gray', s.risk_label)}</td>
            <td class="num"><span class="t-desc">${escapeHtml(s.approval_expires_at || '—')}</span></td>
            <td class="r"><button class="btn sm primary" data-goto-review="2">去审核中心</button></td>
          </tr>`).join('')}</tbody></table></div></div>` : ''}
      <div id="ag-modal-slot"></div>`;
  },

  async mount(root) {
    const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
    mountChatGPTPlan(root, (id) => {
      openRunId = id;
      setParam('agents', id);
      refresh();
    });
    bindSubChips(root, (i) => { setSub('agents', i); refresh(); });

    // 错误卡上那颗「重试」必须接上（2026-11-10 自查修的真 bug）。
    // `util.errorBox()` 默认渲染**可点**的 `[data-retry]`，但全站没有
    // 任何 `document`/容器级的委托接住它（已逐个文件普查确认），
    // 于是这一屏 4 处读失败（读配置 :293、读概览 :320、读任务策略 :395、
    // 读运行列表 :441）画出来的按钮点了没有任何反应：不发请求、不 disabled、
    // 控制台也不报错——人只会以为"重试没用"，而它本来是唯一不用刷整页的出路。
    // 做法与 `writing.js:411` 一致：绑在这一屏持久容器上、一次一绑。
    // 用 `querySelectorAll` 而不是 `querySelector`：概览那一屏可能同时出现
    // 两张错误卡（读概览 :320 与读运行列表 :441），只绑第一颗等于漏绑。
    root.querySelectorAll('[data-retry]').forEach((btn) =>
      btn.addEventListener('click', refresh));

    root.querySelectorAll('[data-goto-models]').forEach((btn) => {
      btn.addEventListener('click', () => {
        // 跳哪一个二级页由模型中心自己的 SUBS 决定，这里只按语义给下标：
        // 1＝引擎管理（默认轨与端点），2＝调用策略（九场景）
        gotoEntry({ module: 'models',
                    sub: btn.dataset.gotoModels === 'policy' ? 2 : 1 });
      });
    });
    root.querySelectorAll('[data-goto-review]').forEach((btn) => {
      btn.addEventListener('click', () => {
        gotoEntry({ module: 'review', sub: Number(btn.dataset.gotoReview) || 2 });
      });
    });
    root.querySelectorAll('[data-open-run]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const id = Number(btn.dataset.openRun);
        if (currentSub() !== 2) {
          // 总览没有详情位：带着这条运行去「运行与审批」，在那一行下面展开
          openRunId = id;
          setParam('agents', id);
          setSub('agents', 2);
          refresh();
          return;
        }
        openRunId = openRunId === id ? null : id;
        setParam('agents', openRunId || undefined);
        refresh();
      });
    });
    const openCreateModal = (presetGoal = '', presetEpisodeId = null) => run('准备发起', async () => {
      const [episodes, policies] = await Promise.all([
        api.episodes(store.current.id),
        store.can('system:config') ? api.modelPolicies() : Promise.resolve({ items: [] }),
      ]);
      // 有效期档位也得先从接口取：这一步原来完全没调 plan-options，
      // 直接把一条写死的 24 小时塞进弹窗。现在档位表与默认档都由服务端说。
      const qs = new URLSearchParams({
        project_id: String(store.current.id), goal: presetGoal, scene_key: '',
      });
      if (presetEpisodeId) qs.set('episode_id', String(presetEpisodeId));
      const opts = await api.agentPlanOptions(qs.toString());
      const slot = root.querySelector('#ag-modal-slot');
      if (!slot) return;
      slot.innerHTML = createModal(episodes, policies.items || [], opts || {});
      const modal = slot.querySelector('#ag-modal');
      if (presetGoal) modal.querySelector('#ag-goal').value = presetGoal;
      if (presetEpisodeId) {
        const epSel = modal.querySelector('#ag-ep');
        if (epSel && [...epSel.options].some((o) => Number(o.value) === Number(presetEpisodeId))) {
          epSel.value = String(presetEpisodeId);
        }
      }
      modal.hidden = false;
      bindModal(modal, () => {
        if (currentSub() !== 2) setSub('agents', 2);
        refresh();
      }, policies.items || []);
      bindModalDismiss(modal, () => { modal.hidden = true; });
    });
    root.querySelectorAll('[data-ag-new]').forEach((btn) => {
      btn.addEventListener('click', () => openCreateModal('', store.currentEpisodeId || null));
    });
    root.querySelectorAll('[data-ag-tpl]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const tpl = QUICK_TEMPLATES.find((t) => t.id === btn.dataset.agTpl);
        if (tpl) openCreateModal(tpl.goal, store.currentEpisodeId || null);
      });
    });
    const prefill = takeAgentPrefill();
    if (prefill && store.can('job:submit')) {
      openCreateModal(prefill.goal || '', prefill.episode_id || null);
    }
    if (openRunId) await renderDetail(root);
  },
};

/** 详情异步读：连点两行时旧响应不能盖掉新打开那条（同审核中心那条纪律）。 */
async function renderDetail(root) {
  const holder = root.querySelector('#ag-run-detail');
  if (!holder) return;
  const wanted = openRunId;
  let d = null;
  let err = null;
  try { d = await api.agentRun(wanted); } catch (e) { err = e; }
  if (wanted !== openRunId) return;              // 已经有更新的点击在等回来
  if (err) {
    holder.outerHTML = `<div class="card" id="ag-run-detail">${errorBox('读运行详情', err)}</div>`;
    // `outerHTML` 换掉的是**整张卡**，上面 mount 里绑的那次早就跟着旧节点没了
    // （mount 只在进这一屏时跑一遍，而这一张是点某一行时才画的），
    // 所以这里必须在新节点上重新绑一次，否则这颗重试依旧是死的。
    document.getElementById('ag-run-detail')
      ?.querySelector('[data-retry]')
      ?.addEventListener('click',
        () => window.dispatchEvent(new CustomEvent('app:refresh')));
    return;
  }
  // 先把服务端给的事件标签收进缓存，再画：`detailHtml` 里那一栏要用它。
  // 详情没带（老后端）就保留上一次的，仍没有就原样显示事件码。
  if (d.event_labels) cache.eventLabels = d.event_labels;
  // 沿用占位卡的 class：行内详情与列表下的单独详情卡外边距不同
  holder.outerHTML = `<div class="${escapeHtml(holder.className || 'card')}" id="ag-run-detail">${detailHtml(d)}</div>`;
  const card = document.getElementById('ag-run-detail');
  // 行内展开后把详情滚进视野（只在看不全时滚，已经看得见就不动）
  card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  const again = () => window.dispatchEvent(new CustomEvent('app:refresh'));
  card.querySelectorAll('[data-goto-review]').forEach((b) => b.addEventListener('click', () => {
    gotoEntry({ module: 'review', sub: 2 });
  }));
  card.querySelectorAll('[data-goto-job]').forEach((b) => b.addEventListener('click', () => {
    // 状态、成本、日志、产物都不在智能体表里，跳过去看的就是生成中心那一页
    gotoEntry({ module: 'generate', sub: 0, job_id: Number(b.dataset.gotoJob) });
  }));
  card.querySelectorAll('[data-run-pause]').forEach((b) => b.addEventListener('click', () => run('暂停', () => api.pauseAgentRun(Number(b.dataset.runPause)).then(again))));
  card.querySelectorAll('[data-run-resume]').forEach((b) => b.addEventListener('click', () => run('恢复', () => api.resumeAgentRun(Number(b.dataset.runResume)).then(again))));
  card.querySelectorAll('[data-run-cancel]').forEach((b) => b.addEventListener('click', () => run('取消', () => api.cancelAgentRun(Number(b.dataset.runCancel),
      '人在智能体中心点的取消').then(again))));
}

/** 弹窗：目标／场景一改就重读路线，念出来的永远是当下那一条。 */
async function refreshRoute(modal) {
  const goal = modal.querySelector('#ag-goal').value.trim();
  const scene = modal.querySelector('#ag-scene').value;
  const ep = modal.querySelector('#ag-ep').value;
  const line = modal.querySelector('#ag-route');
  const cloudLine = modal.querySelector('#ag-cloud-line');
  if (!goal || goal.length < 4) {
    line.textContent = '先把目标写清楚（4 字以上），平台才知道要走哪条轨。';
    cloudLine.hidden = true;
    return;
  }
  const qs = new URLSearchParams({
    project_id: String(store.current.id), goal, scene_key: scene });
  if (ep) qs.set('episode_id', ep);
  let out;
  try {
    out = await api.agentPlanOptions(qs.toString());
  } catch (e) {
    line.textContent = `读不到路线：${e.message}`;
    return;
  }
  const r = out.route || {};
  if (!r.ok) {
    line.textContent = `现在算不出计划：${r.reason || '未知原因'}`;
    cloudLine.hidden = true;
    return;
  }
  line.innerHTML = `算这次计划会走：<b>${escapeHtml([r.backend, r.provider, r.model]
    .filter(Boolean).join(' / '))}</b><br>${escapeHtml(r.data_out || '')}<br>
    ${escapeHtml(r.cost_note || '')}
    ${r.suggestion ? `<br>这一类任务的建议轨：${escapeHtml(r.suggestion)}（只是提示，不改默认轨）` : ''}`;
  cloudLine.hidden = !r.needs_cloud_confirm;
  modal.querySelector('#ag-cloud-what').textContent = r.data_out || '';
  modal.querySelector('#ag-cloud-cost').textContent = `${r.cost_note || ''}。`;
}

function bindModal(modal, refresh) {
  const submission = submissionIdentity();
  let busy = false;
  const errEl = modal.querySelector('#ag-modal-err');
  const close = () => { modal.hidden = true; modal.remove(); };
  modal.querySelector('[data-ag-close]').addEventListener('click', close);
  ['ag-goal', 'ag-scene', 'ag-ep'].forEach((id) => {
    const el = modal.querySelector('#' + id);
    el.addEventListener(id === 'ag-goal' ? 'input' : 'change', () => refreshRoute(modal));
  });
  modal.querySelector('[data-ag-submit]').addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    const submit = modal.querySelector('[data-ag-submit]');
    submit.disabled = true;
    const cloud = modal.querySelector('#ag-cloud');
    errEl.hidden = true;
    try {
      const body = {
        project_id: store.current.id,
        episode_id: modal.querySelector('#ag-ep').value
          ? Number(modal.querySelector('#ag-ep').value) : null,
        goal: modal.querySelector('#ag-goal').value.trim(),
        scene_key: modal.querySelector('#ag-scene').value,
        approval_valid_seconds: Number(modal.querySelector('#ag-valid').value),
        user_confirmed_cloud: cloud ? cloud.checked : false,
      };
      body.idempotency_key = submission.key(body);
      const out = await api.createAgentRun(body);
      submission.clear();
      openRunId = out.run.id;
      setParam('agents', openRunId);
      close();
      refresh();
    } catch (e) {
      errEl.hidden = false;
      errEl.textContent = e.message;
    } finally { busy = false; submit.disabled = false; }
  });
  refreshRoute(modal);
}
