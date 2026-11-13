import { chatgptPlanCard, mountChatGPTPlan } from './chatgpt-plan.js';
import { submissionIdentity } from '../submission.js';
/**
 * 智能体中心（0041／《智能体管理中心》说明书）。四个工作区：
 *   0 总览        四个数 ＋ 两条腿摘要 ＋ 运行列表
 *   1 智能体配置  本地／云端两条腿现在是什么状态 ＋ 工具白名单表
 *   2 任务路由    九个任务场景配到哪了——只读，改在模型中心
 *   3 运行与审批  一条运行的步骤、事件、暂停／恢复／取消，以及"等你审批几步"
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
import { getParam, getSub, setParam, setSub } from '../navstate.js';
import {
  agoText, bindModalDismiss, bindSubChips, escapeHtml, errorBox, run, skeleton, subChipRow,
} from '../util.js';

const SUBS = ['总览', '智能体配置', '任务路由', '运行与审批'];
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

/** 两条腿摘要：配置·健康·驻留·端点四件事分开念（说明书 §2「状态概念不混淆」）。 */
function legsHtml(configs) {
  if (!configs || !configs.cards) {
    return '<p class="hint">两条腿的详细状态需要配置权限；这里只显示运行。</p>';
  }
  const rows = configs.cards.map((c) => `<tr>
    <td><b>${escapeHtml(c.backend_label)}</b></td>
    <td>${c.configured ? escapeHtml([c.provider, c.model].filter(Boolean).join(' / '))
      : '<span class="t-desc">槽位还没填</span>'}</td>
    <td>${chip(HEALTH_TONE[c.health_state] || 'gray',
      c.health_detail || '未检查')}</td>
    <td><span class="t-desc">${escapeHtml(c.endpoint || '（这一档不是本机服务型）')}</span></td>
    <td><span class="t-desc">${escapeHtml(c.resident || '—')}</span></td>
    <td class="r"><button class="btn sm" data-goto-models="${escapeHtml(c.backend)}"
      >去模型中心</button></td>
  </tr>`).join('');
  return `<div class="table-wrap"><table>
    <thead><tr><th>哪条腿</th><th>配的档</th><th>现在能不能用</th><th>端点</th>
      <th>驻留</th><th class="r"></th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="card-foot"><p class="modal-meta">${escapeHtml(configs.caliber || '')}</p></div>`;
}

function runsTable(runs) {
  if (!runs || !runs.length) {
    return `<div class="empty"><div class="empty-t">还没有智能体运行</div>
      <p>发起一次：写一句目标，平台先算计划（这一步就是一条普通文本作业）；
        计划回来后按风险档决定"直接做"还是"停下来等你批"。</p>
      <p class="hint">有配置权限的人才在这一页发起；制作角色可以在这里看自己发起的运行。</p></div>`;
  }
  return `<div class="table-wrap"><table>
    <thead><tr><th class="num">#</th><th>目标</th><th>状态</th><th class="num">步骤</th>
      <th>走哪条轨</th><th class="num">发起于</th><th class="r"></th></tr></thead>
    <tbody>${runs.map((r) => {
      const snap = r.model_snapshot || {};
      const done = r.steps.filter((s) => s.status === 'succeeded').length;
      const waiting = r.step_counts.awaiting_approval || 0;
      const stamp = (r.created_at || '').replace(' ', 'T');
      return `<tr class="${r.id === openRunId ? 'sel' : ''}">
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
        <td class="r"><button class="btn sm ${r.id === openRunId ? '' : 'primary'}"
          data-open-run="${r.id}">${r.id === openRunId ? '收起' : '详情'}</button></td>
      </tr>`;
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
      <div class="row" style="gap:8px;margin-top:12px">
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
      <label class="modal-label" for="ag-cloud"
        style="display:flex;gap:8px;align-items:flex-start;margin-top:12px">
        <input type="checkbox" id="ag-cloud" style="width:auto;margin-top:3px">
        <span>确认用云端算这次计划：<b id="ag-cloud-what"></b><br>
          <span class="t-desc" id="ag-cloud-cost"></span>
          平台不会替你勾这一格，也不会因为本地失败就自己换到云端。</span></label></span>`;
  return `<div class="modal" id="ag-modal" hidden role="dialog" aria-modal="true"
    aria-label="发起一次智能体运行">
    <div class="modal-box">
      <div class="modal-head">发起一次智能体运行</div>
      <div class="modal-body">
        <label class="modal-label" for="ag-goal">要它做什么（一句话说清）</label>
        <textarea class="ctl" id="ag-goal" rows="3"
          placeholder="例：盘点第 1 集缺哪些资产，并为每个缺的角色生成一段人物履历"></textarea>
        <label class="modal-label" for="ag-ep" style="margin-top:12px">哪一集（可留空＝整个项目）</label>
        <select class="ctl" id="ag-ep">
          <option value="">（不指定集）</option>
          ${episodes.map((e) => `<option value="${e.id}">第 ${e.seq} 集${
            e.title ? ' · ' + escapeHtml(e.title) : ''}</option>`).join('')}
        </select>
        <label class="modal-label" for="ag-scene" style="margin-top:12px">按哪个任务场景</label>
        <select class="ctl" id="ag-scene">
          <option value="">（不按场景：走文本能力的默认轨）</option>
          ${policies.map((p) => `<option value="${escapeHtml(p.scene_key)}">${
            escapeHtml(p.scene_label)}${p.enabled ? '' : '（待配置）'}</option>`).join('')}
        </select>
        <label class="modal-label" for="ag-valid" style="margin-top:12px">批准的有效期</label>
        <select class="ctl" id="ag-valid">
          ${choices.map((c) => `<option value="${c.seconds}"${
            c.seconds === defaultValid ? ' selected' : ''}>${escapeHtml(c.label)}</option>`).join('')}
        </select>
        <p class="modal-meta" id="ag-route">正在读路线与计价…</p>
        ${confirmLine}
        <p class="modal-meta">发起后先排一条<b>算计划的文本作业</b>（就进生成中心那一个队列）。
          计划回来后：只读的步骤当场做完；要花钱或改正式业务事实的会停下来等你批。</p>
        <p class="bar bad" id="ag-modal-err" hidden></p>
      </div>
      <div class="modal-actions">
        <button class="btn" data-ag-close>取消</button>
        <button class="btn primary" data-ag-submit>发起</button>
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
        <h1>智能体中心</h1><p>${escapeHtml(line)}</p>
        <div class="row" style="gap:6px">${subChipRow(SUBS, sub)}</div>
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
          return `<section class="view on">${head('配置与工具白名单')}${errorBox('读配置', e)}</section>`;
        }
      }
      return `<section class="view on">${head('配置与工具白名单')}${this.configPane()}</section>`;
    }
    if (sub === 2) return `<section class="view on">${head('任务路由：九个场景现在配到哪了')}${await this.routingPane()}</section>`;
    if (sub === 3) return `<section class="view on">${head('运行与审批')}${await this.runsPane(project)}</section>`;

    let err = null;
    let overview = null;
    try {
      if (store.can('system:config')) {
        [overview, cache.configs, cache.tools] = await Promise.all([
          api.agentOverview(), api.agentConfigs(), api.agentTools(),
        ]);
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
      ${head('谁替人做决定、决定到哪一步了')}
      ${err ? `<div class="card">${errorBox('读概览', err)}</div>` : ''}
      ${counts.runs !== undefined ? `<div class="card mb-card" >
        <div class="card-head"><h2>四个数</h2>
          <span class="sub">${escapeHtml((overview && overview.caliber) || '')}</span></div>
        <div class="proj-stats">
          ${stat(counts.runs ?? '—', '运行')}
          ${stat(counts.active ?? '—', '进行中')}
          ${stat(counts.awaiting_approval ?? '—', '等你审批（步骤）',
            !!counts.awaiting_approval)}
          ${stat(`${counts.usable_configs ?? '—'}/${counts.total_configs ?? '—'}`, '可用配置')}
        </div></div>` : ''}
      ${cache.configs ? `<div class="card mb-card" >
        <div class="card-head"><h2>两条腿</h2>
          <span class="sub">配置·健康·驻留·运行是四件事，分开显示</span></div>
        ${legsHtml(cache.configs)}</div>` : ''}
      <div class="card">
        <div class="card-head"><h2>最近的运行</h2>
          <span class="sub">点一行看步骤与事件；这一页没有签字按钮，是有意的</span></div>
        ${runsTable(overview ? overview.runs : [])}
      </div>
    </section>`;
  },

  /** 配置页：没配置权限时不装样子，直接说明去哪一栏能看见。 */
  configPane() {
    if (!store.can('system:config')) {
      return `<div class="card"><div class="empty">
        <div class="empty-t">这一栏需要配置权限</div>
        <p>智能体的配置就是模型中心的配置：谁能改默认轨，谁就能改这里看到的两行。</p>
        <p class="hint">你仍然可以在「运行与审批」里看自己发起的运行。</p></div></div>`;
    }
    const cards = (cache.configs && cache.configs.cards) || [];
    const tools = Object.entries((cache.tools && cache.tools.items) || {});
    return `
      <div class="card mb-card" >
        <div class="card-head"><h2>本地与云端两条腿</h2>
          <span class="sub">要改的是模型中心那三张表；这一页只读，每次进来现查一次健康</span></div>
        ${legsHtml(cache.configs)}
        <div class="card-foot">
          ${cards.map((c) => `<p class="modal-meta"><b>${escapeHtml(c.backend_label)}</b>：
            密钥 ${c.key_env
              ? `${escapeHtml(c.key_env)}·${c.secret_present ? '环境里有值' : '环境里没值'}`
              : '本机服务不需要密钥'}（值从不入库、不进出参）；
            计价 ${escapeHtml(c.cost_note || '—')}</p>`).join('')}
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>工具白名单</h2>
          <span class="sub">智能体能做的事就这几行；加一个是改代码，不是加一个按钮</span></div>
        <div class="table-wrap"><table>
          <thead><tr><th>工具码</th><th>能做什么</th><th>风险档</th><th>往外送什么</th>
            <th>怎么验收</th></tr></thead>
          <tbody>${tools.map(([code, t]) => `<tr>
            <td><code>${escapeHtml(code)}</code></td>
            <td>${escapeHtml(t.label)}</td>
            <td>${chip('gray', t.risk_label)}</td>
            <td><span class="t-desc">${escapeHtml(t.data_out)}</span></td>
            <td><span class="t-desc">${escapeHtml(t.verify)}</span></td></tr>`).join('')}
          </tbody></table></div>
        <div class="card-foot">
          <p class="modal-meta">${escapeHtml((cache.tools && cache.tools.note) || '')}</p>
          <p class="modal-meta">${escapeHtml((cache.tools && cache.tools.forbidden) || '')}
            ｜一次计划上限 ${escapeHtml(String((cache.tools && cache.tools.max_plan_steps) ?? '—'))} 步</p>
        </div>
      </div>`;
  },

  /** 任务路由：只读九场景 ＋ 一句"改在模型中心"。 */
  async routingPane() {
    if (!store.can('system:config')) {
      return `<div class="card"><div class="empty"><div class="empty-t">这一栏需要配置权限</div>
        <p>路由策略就是模型中心「调用策略」那一页的九行；这里不再给一份可编辑的表。</p>
        </div></div>`;
    }
    let data = null;
    try { data = await api.modelPolicies(); } catch (e) { return errorBox('读任务策略', e); }
    const rows = (data.items || []).map((p) => `<tr>
      <td><b>${escapeHtml(p.scene_label || p.scene_key)}</b>
        <span class="t-desc">${escapeHtml(p.scene_key)}</span></td>
      <td>${escapeHtml(p.capability_label)}</td>
      <td>${(p.required || []).map((r) => chip('gray', r)).join(' ') || '—'}</td>
      <td><span class="t-desc">${escapeHtml(p.slot_provider ? `${p.slot_provider}/${p.slot_model}` : '未配')}</span></td>
      <td>${chip('gray', p.preference_label || p.preference)}</td>
      <td><span class="t-desc">${escapeHtml(p.fallback_provider
        ? `${p.fallback_provider}/${p.fallback_model}` : '—')}</span></td>
      <td>${p.enabled ? chip('green', '已启用') : chip('gray', '待配置')}</td>
    </tr>`).join('');
    return `
      <div class="card">
        <div class="card-head"><h2>九个任务场景</h2>
          <span class="sub">这一页只读：要改建议轨与偏好，去模型中心·调用策略</span></div>
        <div class="table-wrap"><table>
          <thead><tr><th>场景</th><th>要的能力</th><th>必需标签</th><th>建议轨</th>
            <th>偏好</th><th>备用轨</th><th>状态</th></tr></thead>
          <tbody>${rows}</tbody></table></div>
        <!-- 表格卡片的下半段统一坐进 .card-foot：卡片本身 padding 是 0（表格要铺满），
             所以表外那几行（提示条、脚注、动作按钮）必须自己带内距。
             原来这里三样都直接挂在 .card 下，按钮行离卡片下沿只有 1px，贴着边框。 -->
        <div class="card-foot">
          <p class="bar">${data.no_auto_fallback
            ? '<b>备用轨不会自动顶上。</b>默认轨的唯一来源仍是「引擎管理」里打勾那一行；'
              + '智能体也一样：它只按人确认过的轨提交，本地失败不静默换云端。'
            : '清单说允许自动兜底——这条与智能体的授权要求冲突，需要人先看一眼策略。'}</p>
          <p class="modal-meta">智能体发起时读的就是这张表的场景那一行；
            「建议轨」显示给人看，最终走哪条仍由默认轨与人的确认决定。</p>
          <div class="row" style="margin-top:12px">
            <button class="btn" data-goto-models="policy">去模型中心·调用策略改这些行</button></div>
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
    return `
      ${chatgptPlanCard()}
      ${err ? `<div class="card mb-card" >${errorBox('读运行列表', err)}</div>` : ''}
      <div class="card mb-card" >
        <div class="card-head"><h2>这个项目的运行</h2>
          <span class="sub">计划、步骤、事件都在这；签字在审核中心</span>
          <div class="right"><button class="btn primary sm" data-ag-new>发起一次运行</button></div></div>
        ${runsTable(runs)}
      </div>
      ${waiting.length ? `<div class="card mb-card" >
        <div class="card-head"><h2>等你批 ${waiting.length} 步</h2>
          <span class="sub">只显示状态——签字那一步在审核中心</span></div>
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
      ${openRunId ? '<div id="ag-run-detail" class="card"><div class="card-body">'
        + skeleton(3, 'text') + '</div></div>' : ''}
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
        // 1＝引擎管理（默认轨与端点），2＝调用策略（九场景）；
        // 下标由模型中心自己的 SUBS 决定，这里只按语义给
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
        openRunId = openRunId === id ? null : id;
        setParam('agents', openRunId || undefined);
        refresh();
      });
    });
    const newBtn = root.querySelector('[data-ag-new]');
    if (newBtn) {
      newBtn.addEventListener('click', () => run('准备发起', async () => {
        const [episodes, policies] = await Promise.all([
          api.episodes(store.current.id),
          store.can('system:config') ? api.modelPolicies() : Promise.resolve({ items: [] }),
        ]);
        // 有效期档位也得先从接口取：这一步原来完全没调 plan-options，
        // 直接把一条写死的 24 小时塞进弹窗。现在档位表与默认档都由服务端说。
        const qs = new URLSearchParams({
          project_id: String(store.current.id), goal: '', scene_key: '',
        }).toString();
        const opts = await api.agentPlanOptions(qs);
        const slot = root.querySelector('#ag-modal-slot');
        slot.innerHTML = createModal(episodes, policies.items || [], opts || {});
        const modal = slot.querySelector('#ag-modal');
        modal.hidden = false;
        bindModal(modal, refresh, policies.items || []);
        bindModalDismiss(modal, () => { modal.hidden = true; });
      }));
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
  holder.outerHTML = `<div class="card" id="ag-run-detail">${detailHtml(d)}</div>`;
  const card = document.getElementById('ag-run-detail');
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
