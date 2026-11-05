import { api } from '../api.js';
import { store } from '../store.js';
import {
  bindModalDismiss, bindSubChips, confirmDanger, escapeHtml, run, subChipRow,
} from '../util.js';
import { getSub, setSub } from '../navstate.js';
import { HEALTH, VAL_BADGE, alertLabel } from '../validation.js';
import { REG_SOURCES } from '../enums.js';

/**
 * 模型中心（v0.99，0035）。四个工作区：
 *   0 模型仓库   清单 × 登记 的联表：这台机器上到底有哪些档、各是什么状态
 *   1 引擎管理   能力轨十行（从系统设置搬来，一行功能没减）＋ 按引擎分组的健康
 *   2 调用策略   九个任务场景 → 必需能力 / 建议轨 / 偏好 / 备用轨
 *   3 运行监控   队列、worker、成本、最近失败——全部现读，不复制 Job 的事实
 *
 * 三条贯穿全模块的边界，都是为了让"模型中心"不长出第二个执行系统：
 *
 * 1. **规格不入库**。参数范围、计价、出处一律来自 `GET /api/providers/manifest`
 *    （后端 `adapters.py`）。这一页没有"新建模型规格"这回事，只有"登记一个实例"。
 * 2. **选路不在这里**。默认轨的唯一来源是能力轨那一列打勾（`provider_config.is_default`），
 *    策略页的备用轨只是"可改选"的提示——后端 `resolve_provider()` 不补位、不兜底。
 *    界面每次说到备用轨，都必须把这一句一起说。
 * 3. **状态不自己拼**。健康、验证、生产可用、队列计数全部读服务端算好的字段；
 *    这一页算的只有"筛选后的行数"这种纯视图数字。
 */
const SUBS = ['模型仓库', '引擎管理', '调用策略', '运行监控'];

// 健康与验证的中文译名一律来自 js/validation.js（唯一出口）。
// 这一页以前自己写过一份 HEALTH，与生成中心那份并存——
// 同一个 ok 在两屏上叫两个名字，就是外部评审点过的那类口径事故。
const VAL_KIND_LABEL = {
  env_check: '环境检查', mock: '模拟', real_call: '真实调用',
  target_machine: '目标机基准', artifact_review: '产物验收',
};
const VAL_STATE = {
  ok: ['green', '通过'], missing: ['red', '缺东西'], unknown: ['amber', '未检查'],
  unconfigured: ['gray', '未配置'], unregistered: ['red', '未登记'],
  failed: ['red', '失败'], never: ['gray', '没有记录'],
};

const norm = (s) => String(s || '').trim().toLowerCase();

// 一次 render 把四份数据都缓存下来；mount 里的交互不重新发请求，
// 免得点一下筛选就把整页的读请求打一遍
let cache = { catalog: null, manifest: null, providers: null, policies: null,
              stats: null, worker: null, validations: [] };

function stateChip(state) {
  const [tone, label] = VAL_STATE[state] || ['gray', state || '未知'];
  return `<span class="chip ${tone}">${escapeHtml(label)}</span>`;
}

function healthChip(health) {
  const [tone, label] = HEALTH[health?.state] || ['gray', '未知'];
  return `<span class="chip ${tone}" title="${escapeHtml(health?.detail || '')}">${label}</span>`;
}

function adapterOf(manifest, cap, backend, provider) {
  if (!manifest || !provider) return null;
  return (manifest.adapters || []).find((a) => a.capability === cap
    && a.backend === backend
    && (norm(a.code) === norm(provider)
      || (a.aliases || []).some((x) => norm(x) === norm(provider))));
}

/**
 * 运行位置（登记层）的中文名。权威在后端 `models.REG_SOURCES`，随
 * `GET /api/models` 的 `reg_sources` 下发；`enums.js` 那份只用在"清单还没回来"
 * 的那一瞬。原来这里是一份独立的字典，标签还和后端差过一个字（"远程自建服务"
 * vs "远程自建"），两屏上同一档模型两种叫法。
 */
function sourceLabels() {
  const fromApi = cache.catalog && cache.catalog.reg_sources;
  return fromApi && Object.keys(fromApi).length ? fromApi : REG_SOURCES;
}

/** 一行的运行位置说法：接口那份优先，认不出来就原样念 code（不猜近义档）。 */
function sourceLabel(code) {
  return sourceLabels()[code] || String(code || '');
}


/* ================= 顶部概览 ================= */
/**
 * 顶部四数收成一条带。
 *
 * 数字仍然全部来自 `GET /api/models/stats`（后端一处算，口径随返回值一起给），
 * 界面只念不算——模型数、可用数、引擎数一旦在两处各算一遍，迟早说出两句不一样的话。
 * 口径那一行不再摊开成一段说明，压成带子末尾的一句小字；
 * 每个数自己另带 title，鼠标停上去就是它自己的口径。
 */
function overviewCard(s) {
  const cap = s.registered_by_capability || {};
  const capText = Object.entries(cap)
    .map(([k, v]) => `${cache.catalog.capabilities[k] || k} ${v}`).join(' · ');
  return `
  <div class="card" style="margin-bottom:12px">
    <div class="mc-strip">
      <span class="st" title="${escapeHtml(s.caliber.registered_total)}">已登记
        <b>${s.registered_total}</b>
        <span class="hint">${escapeHtml(capText || '还没有登记')}</span></span>
      <span class="st" title="${escapeHtml(s.caliber.available_total)}">可用
        <b>${s.available_total}</b>
        ${s.needs_attention
          ? `<span class="chip amber">${s.needs_attention} 个要看一眼</span>`
          : '<span class="chip green">都齐了</span>'}</span>
      <span class="st" title="${escapeHtml(s.caliber.engine_total)}">引擎
        <b>${s.engine_total}</b>
        <span class="hint">${s.engine_params_known} 个参数已核验 ·
          ${s.unintegrated_engines.length} 个未接入</span></span>
      <span class="st" title="与生成中心同一个口径：${escapeHtml(s.caliber.queued)}">任务
        <b>${s.running + s.queued}</b>
        <span class="hint">跑 ${s.running} · 等 ${s.queued}</span></span>
      <span class="st-caliber">可用＝已登记 ∧ 已启用 ∧ 健康就绪 ∧ 这一档自己有过真实调用或产物验收。
        引擎在线不代表模型可用，文件在也不代表依赖齐。</span>
    </div>
  </div>`;
}

/* ================= 工作区 0 · 模型仓库 ================= */
function tagChips(tags) {
  if (!tags || !tags.length) return '<span class="hint">没有能力标签</span>';
  return tags.map((t) => {
    // 人工声明未核验的必须一眼看得出来：清单派生是事实，人工声明是主张
    const cls = t.source === 'manifest' ? 'plain' : (t.verified ? 'blue' : 'amber');
    const tip = t.source === 'manifest'
      ? '来自适配器清单已登记的参数表，可复算'
      : (t.verified ? '人工声明，且这一档有过真实调用或产物验收' : '人工声明，尚未核验');
    return `<span class="chip ${cls}" title="${escapeHtml(tip)}">${escapeHtml(t.tag)}</span>`;
  }).join('');
}

function priceChips(prices) {
  if (!prices || !prices.length) return '<span class="hint">无按量计价</span>';
  return prices.map((p) => {
    const yuan = (p.amount_micros / 1e6).toFixed(p.amount_micros % 1e6 ? 2 : 1);
    const unit = { second: '秒', million_token: '百万 token', image: '张' }[p.unit] || p.unit;
    const when = Object.entries(p.when || {}).map(([k, v]) => `${k}=${v}`).join(' ');
    return `<span class="chip plain" title="${escapeHtml(p.source)}">
      ${yuan} ${escapeHtml(p.currency)}/${escapeHtml(unit)}${when ? ' · ' + escapeHtml(when) : ''}</span>`;
  }).join('');
}

function statusChip(item, labels) {
  const tone = { available: 'green', unverified: 'amber', not_registered: 'gray',
                 unchecked: 'amber', disabled: 'gray', not_installed: 'gray',
                 dependency_missing: 'red', connection_failed: 'red',
                 unregistered: 'red', unregistered_tier: 'amber' }[item.status] || 'gray';
  const label = labels[item.status] || item.status;
  return `<span class="chip ${tone}" title="${escapeHtml(item.status_detail || '')}">${escapeHtml(label)}</span>`;
}

/**
 * 状态＋健康两枚徽章。文字撞车时只留一枚——
 * "未检查｜未检查"这种并排不是信息，是噪声；两枚各说各的时才并排。
 * 撞车是常态而非例外：状态为 unchecked 的唯一来源就是健康探测返回 unknown，
 * 两边都译成"未检查"。
 */
function statusHealthChips(item) {
  const s = statusChip(item, cache.catalog.status_labels);
  const h = healthChip(item.health);
  const sLabel = (cache.catalog.status_labels[item.status] || item.status);
  const hLabel = (HEALTH[item.health?.state] || ['gray', '未知'])[1];
  return sLabel === hLabel ? s : `${s} ${h}`;
}

function pageRepo() {
  const items = cache.catalog.items;
  // 筛选条件留在模块级：切走再切回来不该把人筛的东西重置（和 sub 同一个道理）
  const rows = items.map((it) => {
    const name = it.display_name || it.model_label || it.model_code || '—';
    return `<tr class="repo-row" data-key="${escapeHtml(it.provider)}/${escapeHtml(it.model_code)}"
        data-cap="${escapeHtml(it.capability)}" data-source="${escapeHtml(it.source)}"
        data-name="${escapeHtml((name + ' ' + it.model_code + ' ' + it.provider).toLowerCase())}"
        data-rid="${it.registration_id || ''}">
      <td><b>${escapeHtml(name)}</b>
        <div class="hint mono">${escapeHtml(it.provider)}/${escapeHtml(it.model_code)}</div></td>
      <td>${escapeHtml(cache.catalog.capabilities[it.capability] || it.capability)}
        <div class="param-chips">${tagChips(it.tags)}</div></td>
      <td><span class="chip ${it.backend === 'local' ? 'plain' : 'blue'}">${escapeHtml(sourceLabel(it.source))}</span></td>
      <td>${escapeHtml(it.engine_label || it.engine_code || '—')}</td>
      <td>${statusHealthChips(it)}</td>
      <td>${it.registration_id
        // 一行只留一个入口。检查／编辑／撤登记收进详情面板底部：
        // 那里有完整上下文（状态、健康、验证出处都在），做决定比在一排小按钮里点靠谱，
        // 而撤登记这种破坏性操作多走一层本来就是对的。
        ? `<button class="btn xs" data-detail="${escapeHtml(it.provider)}/${escapeHtml(it.model_code)}">详情</button>`
        : `<button class="btn xs" data-register="${escapeHtml(it.provider)}/${escapeHtml(it.model_code)}">登记</button>`}</td>
    </tr>`;
  }).join('');

  return `
  <div class="card">
    <div class="card-head">
      <h2>模型仓库</h2>
      <span class="sub">清单 × 本机登记。规格住代码里，这里只登记实例</span>
      <span class="right"><span class="hint" id="repo-count"></span></span>
    </div>
    <div class="card-body" style="padding-bottom:0">
      <div class="filters" style="display:flex;gap:8px;flex-wrap:wrap">
        <input class="ctl" id="repo-search" placeholder="搜索显示名、模型标识或 provider…" style="min-width:230px">
        <select class="ctl" id="repo-cap">
          <option value="">全部类型</option>
          ${Object.entries(cache.catalog.capabilities)
            .map(([k, v]) => `<option value="${k}">${escapeHtml(v)}</option>`).join('')}
        </select>
        <select class="ctl" id="repo-source">
          <option value="">全部来源</option>
          <option value="local">local</option><option value="cloud">cloud</option>
          <option value="remote">remote</option>
        </select>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>模型</th><th>类型 / 能力</th><th>运行位置</th><th>所属引擎</th>
          <th>状态 / 健康</th><th>操作</th></tr></thead>
        <tbody id="repo-rows">${rows}</tbody>
      </table>
    </div>
    <div class="card-body" style="border-top:1px solid var(--line)">
      <p class="note"><b>「未登记」那一档为什么也列出来</b>：清单里有、本机没登记，
        说明规格核过但没人用它；只列已登记的话，"装了不等于配了"这件事就看不见。
        登记不会去下载权重，也不会启动任何推理。</p>
      <p class="note"><b>「撤登记」不删模型文件</b>：它只删这一行登记。
        权重仍在盘上，历史作业与验证记录一律不动。</p>
    </div>
  </div>`;
}

/* ================= 工作区 1 · 引擎管理 ================= */
/* 下面这一段是原 system-config.js 的「模型与密钥」整页搬过来：
   十行能力轨、允许参数、密钥变量名、健康、验证列、环境检查、验证记录弹窗、
   整批保存——一行功能都没减。搬动的理由只有一个：模型这件事的入口现在只有一个。 */

function paramChips(model) {
  if (!model || !model.params || !model.params.length) return '';
  return model.params.map((p) => {
    const bits = [];
    if (p.default !== null && p.default !== undefined) bits.push(`=${p.default}`);
    if (p.min !== undefined && p.max !== undefined) bits.push(`${p.min}—${p.max}`);
    if (p.multiple_of) bits.push(`${p.multiple_of} 的倍数`);
    const title = [p.type, ...bits, p.note || ''].filter(Boolean).join(' · ');
    return `<span class="chip plain" title="${escapeHtml(title)}">${escapeHtml(p.name)}</span>`;
  }).join('');
}

function paramsCell(adapter, model, provider) {
  if (!provider) return '<span class="hint">选 provider 后按清单显示</span>';
  if (!adapter) return '<span class="hint">未登记这个 provider；参数不猜，运行方式也不猜</span>';
  if (!adapter.params_known) {
    return '<span class="hint">参数未核验，平台不预填（接云端 adapter 时按官方 schema 补齐）</span>';
  }
  if (!adapter.models.length) return '<span class="hint">清单里没有模型档</span>';
  if (!model) return '<span class="hint">从「模型」列选一档，或按清单输入</span>';
  const spec = adapter.models.find((m) => m.code === model);
  if (!spec) return '<span class="hint">这个模型 ID 不在清单里；参数不猜</span>';
  const chips = paramChips(spec);
  return chips ? `<div class="param-chips">${chips}</div>` : '<span class="hint">这一档没有可调参数</span>';
}

function noteText(adapter, provider) {
  if (!provider) return '清单：选 provider 后显示适配器与出处';
  if (!adapter) return '清单：没有登记这个 provider，参数与运行方式不会被猜';
  return `清单：${escapeHtml(adapter.label)} · ${escapeHtml(adapter.source)}`;
}

function healthCell(health) {
  const [tone, label] = HEALTH[health?.state] || ['gray', '未知'];
  const detail = health?.detail || '';
  return `<span class="chip ${tone}" title="${escapeHtml(detail)}">${label}</span>
          <span class="hint">${escapeHtml(detail)}</span>`;
}

function badgeOf(v) {
  for (const kind of ['target_machine', 'artifact_review', 'real_call', 'mock', 'env_check']) {
    if ((v?.[kind] || {}).state === 'ok') return kind;
  }
  return 'never';
}

function validationCell(r) {
  const v = r.validation || {};
  const kind = badgeOf(v);
  const [tone, label] = VAL_BADGE[kind];
  const tip = ['env_check', 'mock', 'real_call', 'target_machine', 'artifact_review'].map((k) => {
    const item = v[k] || {};
    const st = (VAL_STATE[item.state] || ['', item.state || '没有记录'])[1];
    return `${VAL_KIND_LABEL[k]}：${st}${item.at ? '（' + String(item.at).slice(5, 16) + '）' : ''}`;
  }).join('\n');
  /* 生产可用与否用徽章而不是 hint：hint 会被状态列的密度收口藏掉，
     而"未达生产可用"恰恰是最该常驻可见的那半句。 */
  const ready = v.production_ready
    ? '<span class="chip green">生产可用</span>'
    : '<span class="chip gray">未达生产可用</span>';
  return `<span class="chip ${tone}" title="${escapeHtml(tip)}">${label}</span> ${ready}
    <span class="mc-acts">
      <button class="btn xs" data-env-check="${r.id}">环境检查</button>
      <button class="btn xs" data-val-history="${r.id}">验证记录</button>
    </span>`;
}

/** 健康与验证并成一列：两件事都是"这一轨现在算什么状态"，
 *  拆两列各自窄到折行，不如并一列让它自己换行。 */
function statusCell(r) {
  return `<div class="mc-status">
      <span class="mc-health">${healthCell(r.health)}</span>
      <span class="mc-validation">${validationCell(r)}</span>
    </div>`;
}

/* 展开态记在模块级：切走再切回来不该把人打开的行全关上，
   但也不写进地址——这不是"你在看哪一条"，只是临时视图状态。 */
let resolveSeq = 0;         // 场景候选弹窗的请求序号（只认最后一次）
// 运行监控里"已离线的 worker 进程"默认收着。这是视图状态、不是业务状态：
// 它不进地址栏、不进库，但必须活过轮询重绘——否则每 5 秒刷新一次，
// 人刚点开历史就被折叠回去，等于这个开关白做（与 production.js 的结果条同一类坑）。
let showWorkerHistory = false;
const slotOpen = new Set();
const emptyOpen = new Set();

/* 展开态的细节行。两个按钮留在主行——"检查"是这一页最常用的动作，
   不该藏两层；这里只补主行为了密度而藏起来的那句长解释。 */
function slotDetail(r, adapter) {
  const model = adapter?.models?.find((m) => m.code === r.model);
  const params = model && model.params && model.params.length
    ? `<div class="mc-params" style="display:flex;flex-wrap:wrap;gap:4px;align-self:center">
         ${paramChips(model)}</div>`
    : adapter && !adapter.params_known
      ? '<span class="hint">参数未核验，平台不预填</span>'
      : '<span class="hint">这一档没有可调参数</span>';
  const keyLine = r.backend === 'cloud'
    ? `<div class="mc-detail-actions" style="align-items:center;flex-wrap:wrap">
         <input class="ctl sm mono" style="max-width:260px" data-f="key_env"
                value="${escapeHtml(r.key_env_custom)}"
                placeholder="${escapeHtml(r.key_env || 'FV_KEY_<PROVIDER>')}">
         <span class="hint">实际读 <code>${escapeHtml(r.key_env || '按 provider 推')}</code> ·
           ${r.has_key ? '<b>已配置</b>' : '未配置'}；值永远只从进程环境读</span></div>`
    : `<span class="hint">本机运行时不需要密钥；权重目录走
         <code>${escapeHtml((adapter && adapter.home_env) || 'FASTVIDEO_<CODE>_HOME')}</code></span>`;
  return `<tr class="mc-detail" data-detail-for="${r.id}">
    <td colspan="6"><div class="form-grid">
      <label class="top">清单</label>
      <span class="hint mc-src">${noteText(adapter, r.provider)}</span>
      <label>允许参数</label>${params}
      <label>密钥</label>${keyLine}
      <label>健康说明</label><div class="mc-detail-actions">
        <span class="hint">${escapeHtml(r.health.detail || '没有健康检查记录')}</span></div>
      <label>备注</label><input class="ctl" data-n="notes" value="${escapeHtml(r.notes)}"
        placeholder="为什么选它、计价出处、哪天核过">
    </div></td></tr>`;
}

function slotRow(r, providerLists) {
  const adapter = adapterOf(cache.manifest, r.capability, r.backend, r.provider);
  const providerOptions = (providerLists[`${r.capability}|${r.backend}`] || [])
    .map((a) => `<option value="${escapeHtml(a.code)}">${escapeHtml(a.label)}</option>`).join('');
  const modelOptions = (adapter?.models || [])
    .map((m) => `<option value="${escapeHtml(m.code)}">${escapeHtml(m.label)}</option>`).join('');
  const videoCloud = r.capability === 'video' && r.backend === 'cloud';
  const main = `<tr class="slot"
      data-id="${r.id}" data-cap="${r.capability}" data-backend="${r.backend}">
    <td class="t-strong">${r.capability_label}</td>
    <td><span class="chip ${r.backend === 'local' ? 'plain' : 'blue'}">${r.backend_label}</span></td>
    <td><div class="mc-picks">
        <input class="ctl sm" data-f="provider" list="prov-${r.id}"
               value="${escapeHtml(r.provider)}" placeholder="provider 标识">
        <input class="ctl sm" data-f="model" list="model-${r.id}"
               value="${escapeHtml(r.model)}" placeholder="模型 ID">
        <datalist id="prov-${r.id}">${providerOptions}</datalist>
        <datalist id="model-${r.id}">${modelOptions}</datalist>
      </div></td>
    <td>${statusCell(r)}</td>
    <td class="c"><input type="checkbox" data-f="enabled" ${r.enabled ? 'checked' : ''}></td>
    <td class="c"><input type="radio" name="def-${r.capability}" data-f="default"
             ${r.is_default ? 'checked' : ''} ${videoCloud ? 'disabled' : ''}
             ${videoCloud ? 'title="云端视频只能逐镜头显式选择，不能设为默认"' : ''}></td>
  </tr>`;
  return main + (slotOpen.has(r.id) ? slotDetail(r, adapter) : '');
}

/* 没填 provider 的轨折成一行灰字。十行里本机只填了三行，
   剩下七行不该和已配置的等重——这是这一屏最大的噪声源。 */
function slotCollapsed(r) {
  if (emptyOpen.has(r.id)) return slotRow(r, {});
  const marked = '';
  return `<tr class="mc-collapsed" data-empty-for="${r.id}">
    <td class="t-strong">${r.capability_label}</td>
    <td><span class="chip gray">${r.backend_label}</span></td>
    <td colspan="4">未配置 · <button class="mc-open" data-open-empty="${r.id}">去配置</button>
      ${marked}</td>
  </tr>`;
}

function pageEngines() {
  const manifest = cache.catalog && cache.manifest;
  const providerLists = {};
  (manifest?.adapters || []).forEach((a) => {
    (providerLists[`${a.capability}|${a.backend}`] ||= []).push(a);
  });

  /* 一槽一行；没填 provider 的折成一行灰字，填过的点开才看细节。
     上一版每槽固定两行、备注带里又塞四层，十槽 20 行往上，
     一屏看不见四类能力——这是这一屏最大的噪声源。 */
  const body = cache.providers.map((r) =>
    (r.provider || slotOpen.has(r.id) || emptyOpen.has(r.id))
      ? slotRow(r, providerLists) : slotCollapsed(r)).join('');

  /* 引擎从 13 张卡收成一张表。13 行数据用"每项一行"的卡片是负资产：
     整页 4582px，一屏看不见三张卡，比较"哪家参数核过"要靠滚动记忆。 */
  const byCode = {};
  (manifest?.adapters || []).forEach((a) => { (byCode[a.code] ||= []).push(a); });
  const lastCheck = {};
  (cache.validations || []).forEach((v) => {
    const key = v.adapter_code || v.provider;
    if (key && !lastCheck[key]) lastCheck[key] = v.created_at;
  });
  const capLabel = cache.catalog.capabilities;
  const engineRows = Object.entries(byCode).map(([code, list]) => {
    const tiers = list.reduce((n, a) => n + a.models.length, 0);
    const conn = [...new Set(list.map((a) => a.home_env || a.key_env).filter(Boolean))]
      .map((v) => `<code>${escapeHtml(v)}</code>`).join(' ');
    const checked = lastCheck[code] ? String(lastCheck[code]).slice(0, 16) : '';
    return `<tr>
      <td class="t-strong">${escapeHtml(list[0].label)}
        <div class="hint mono">${escapeHtml(code)}</div></td>
      <td>${list.map((a) => `<span class="chip ${a.backend === 'local' ? 'plain' : 'blue'}">${a.backend}</span>`).join(' ')}</td>
      <td><span class="hint">${[...new Set(list.map((a) => capLabel[a.capability] || a.capability))].join(' · ')}</span></td>
      <td class="c">${tiers || '<span class="chip amber">只登记名字</span>'}</td>
      <td>${list.every((a) => a.params_known)
          ? '<span class="chip green">已核验</span>'
          : '<span class="chip amber">未核验</span>'}</td>
      <td><span class="hint">${conn || '—'}</span></td>
      <td class="t-dim">${escapeHtml(checked || '没有记录')}</td>
    </tr>`;
  }).join('');

  /* 未接入行与"能力轨折叠行"必须分两个类名：前者整行没有按钮，
     后者带一个「去配置」。共用一个类的话，"未接入行没有可点按钮"这条断言
     会被折叠行里的按钮判成假——上一轮就是这么撞上的。 */
  const unintegrated = (cache.catalog.unintegrated_engines || []).map((e) => `<tr class="mc-offline">
    <td class="t-strong">${escapeHtml(e.label)}<div class="hint mono">${escapeHtml(e.code)}</div></td>
    <td><span class="chip gray">${escapeHtml(e.kind)}</span></td>
    <td colspan="3"><span class="hint">${escapeHtml(e.note)}</span></td>
    <td><span class="chip gray">未接入</span></td>
    <td><span class="hint">没有连接检查：平台里没有它的适配器</span></td>
  </tr>`).join('');

  return `
  <div class="card" style="margin-bottom:12px">
    <div class="card-head">
      <h2>能力轨</h2>
      <span class="sub">默认轨走打勾那条；云端视频只能逐镜头另选</span>
      <span class="right">
        <span class="chip plain" id="save-state" hidden>已保存</span>
        <button class="btn xs" id="slots-expand">${slotOpen.size ? '全部收起' : '展开全部细节'}</button>
        <button class="btn primary" id="save-providers">保存能力轨</button>
      </span>
    </div>
    <div class="table-wrap">
      <table>
      <thead><tr>
        <th>能力</th><th>后端</th><th>选型（Provider / 模型）</th>
        <th>状态（健康 · 验证）</th><th class="c">启用</th><th class="c">默认</th>
      </tr></thead>
      <tbody>${body}</tbody>
      </table>
    </div>
    <div class="card-body" style="border-top:1px solid var(--line)">
      <p class="note" style="margin:0"><b>点「能力」那一格展开细节</b>：清单出处、允许参数、
        密钥变量名、环境检查与验证记录、备注都在展开里。
        这一张表是默认轨的唯一来源——调用策略的建议轨与备用轨只是提示，本地失败不会自动转云端。
        健康与验证也不是一回事：健康只说环境齐不齐，验证说凭什么认为真能产出。</p>
    </div>
  </div>

  <div class="card">
    <div class="card-head"><h2>引擎</h2>
      <span class="sub">清单里登记过的适配器；最后一排是本机装了但平台未接入的</span></div>
    <div class="table-wrap"><table>
      <thead><tr><th>引擎</th><th>位置</th><th>能力</th><th class="c">模型档</th>
        <th>参数核验</th><th>连接方式（变量名）</th><th>最近检查</th></tr></thead>
      <tbody>${engineRows}${unintegrated}</tbody>
    </table></div>
    <div class="card-body" style="border-top:1px solid var(--line)">
      <p class="note" style="margin:0"><b>未接入那一排没有「测试连接」按钮</b>：
        平台里没有它们的适配器，点了也不会有真结果。接 ComfyUI 是独立工作包。</p>
    </div>
  </div>`;
}

/* ================= 工作区 2 · 调用策略 ================= */
function pagePolicies() {
  const items = cache.policies.items;
  const candidatesOf = (capability) => cache.catalog.items
    .filter((i) => i.capability === capability && i.registered)
    .map((i) => `<option value="${escapeHtml(i.provider)}|${escapeHtml(i.model_code)}">
        ${escapeHtml(i.display_name || i.model_label || i.model_code)}</option>`).join('');

  const rows = items.map((p) => {
    const cur = p.slot_provider ? `${p.slot_provider}|${p.slot_model}` : '';
    const fb = p.fallback_provider ? `${p.fallback_provider}|${p.fallback_model}` : '';
    return `<tr class="policy" data-key="${escapeHtml(p.scene_key)}">
      <td><b>${escapeHtml(p.scene_label || p.scene_key)}</b>
        <div class="hint mono">${escapeHtml(p.scene_key)}</div></td>
      <td>${escapeHtml(p.capability_label)}</td>
      <td><div class="param-chips">${p.required.map((r) => `<span class="chip plain">${escapeHtml(r)}</span>`).join('') || '<span class="hint">没写必需能力</span>'}</div></td>
      <td><select class="ctl sm" data-f="slot">
          <option value="">— 不指定 —</option>${candidatesOf(p.capability)}</select></td>
      <td><select class="ctl sm" data-f="pref">
          ${Object.entries(cache.policies.preference_labels)
            .map(([k, v]) => `<option value="${k}">${escapeHtml(v)}</option>`).join('')}
        </select></td>
      <td><select class="ctl sm" data-f="fallback">
          <option value="">— 无 —</option>${candidatesOf(p.capability)}</select></td>
      <td class="c"><input type="checkbox" data-f="cloud" ${p.cloud_confirm_required ? 'checked' : ''}></td>
      <td class="c"><input type="checkbox" data-f="on" ${p.enabled ? 'checked' : ''}></td>
      <td>${p.enabled
        ? '<span class="chip green">已配置</span>'
        : '<span class="chip gray">待配置</span>'}
        <div><button class="btn xs" data-resolve="${escapeHtml(p.scene_key)}">看候选</button></div></td>
    </tr>`;
  }).join('');

  return `
  <div class="card">
    <div class="card-head">
      <h2>任务调用策略</h2>
      <span class="sub">场景 → 必需能力 → 建议轨 / 偏好 / 备用轨</span>
      <span class="right"><button class="btn primary" id="save-policies">保存策略</button></span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>任务场景</th><th>能力</th><th>必需能力</th><th>建议轨</th>
          <th>偏好</th><th>备用轨</th><th class="c">要确认计费</th><th class="c">启用</th><th>状态</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="card-body" style="border-top:1px solid var(--line)">
      <p class="modal-err" id="policy-error"></p>
      <p class="note"><b>保存只改配置，不会启动任何任务</b>；
        也不会把建议轨写成默认轨——默认轨的唯一来源仍是「引擎管理」那张能力轨表。</p>
      <p class="note"><b>备用轨不自动顶上</b>（清单里 <code>no_auto_fallback</code> 为
        ${String(cache.policies.no_auto_fallback)}）。备用轨的意义是：默认轨不行时，
        「看候选」能立刻告诉你哪一档能力匹配、可以人工改选，而不是悄悄换一条路把钱花了。</p>
      <p class="note"><b>候选只从登记里出</b>：下拉里能看到的前提是这一档已经在
        「模型仓库」登记过。没登记就不是可选项，避免选了一个跑不起来的东西。</p>
    </div>
  </div>`;
}

/* ================= 工作区 3 · 运行监控 ================= */
/**
 * 这一页只读三样现成的东西：`/api/worker/status`、`/api/models/stats`、
 * `/api/providers/validations`。为什么不去新建一套"模型运行记录"表：
 * Job 才是执行事实的唯一出口（系统设计 §1 不变量 2），
 * 这里再攒一份就必然长出两套状态。
 */
function pageMonitor() {
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
        ${n.age_seconds != null ? `（${n.age_seconds}s 前）` : ''}</td>
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
      <span class="sub">只追加不改；密钥值不进记录</span></div>
    <div class="table-wrap"><table>
      <thead><tr><th>出处</th><th>结果</th><th>槽位</th><th>作业</th><th>说明</th><th>时间</th></tr></thead>
      <tbody>${(cache.validations || []).slice(0, 20).map((v) => `<tr>
        <td><span class="chip plain">${escapeHtml(VAL_KIND_LABEL[v.kind] || v.kind)}</span></td>
        <td>${stateChip(v.state)}</td>
        <td class="hint">${escapeHtml(v.capability)} · ${escapeHtml(v.backend)} · ${escapeHtml(v.provider || '—')}</td>
        <td>${v.job_id ? '#' + v.job_id : '—'}</td>
        <td class="t-desc">${escapeHtml(v.detail || '')}</td>
        <td class="t-dim">${escapeHtml(String(v.created_at || '').slice(0, 16))}</td>
      </tr>`).join('') || '<tr><td colspan="6" class="hint">还没有验证记录</td></tr>'}</tbody>
    </table></div>
  </div>`;
}

/* ================= 弹窗 ================= */
const MODALS = `
  <div class="modal" id="mc-detail" hidden>
    <div class="modal-box wide">
      <div class="modal-head neutral" id="mc-detail-title">模型详情</div>
      <div class="modal-body"><div id="mc-detail-body"></div></div>
      <div class="modal-actions">
        <span id="mc-detail-acts" style="margin-right:auto"></span>
        <button class="btn" data-close="mc-detail">关闭</button>
      </div>
    </div>
  </div>

  <div class="modal" id="mc-form" hidden>
    <div class="modal-box wide">
      <div class="modal-head neutral" id="mc-form-title">登记模型</div>
      <div class="modal-body">
        <div class="form-grid">
          <label for="mf-cap" class="top">能力</label>
          <select id="mf-cap" class="ctl"></select>
          <label for="mf-be" class="top">运行位置</label>
          <select id="mf-be" class="ctl"></select>
          <label for="mf-prov">Provider</label>
          <input id="mf-prov" class="ctl" maxlength="60" placeholder="清单里的 provider code">
          <label for="mf-model">模型标识</label>
          <input id="mf-model" class="ctl" maxlength="80" placeholder="清单里的 model code">
          <label for="mf-name">显示名</label>
          <input id="mf-name" class="ctl" maxlength="80" placeholder="给人看的名字，留空就用清单里的">
          <label for="mf-ver">版本</label>
          <input id="mf-ver" class="ctl" maxlength="60" placeholder="权重版本 / 服务版本">
          <label for="mf-path" class="top">权重路径</label>
          <input id="mf-path" class="ctl" maxlength="300" placeholder="本地：目录或文件路径">
          <label for="mf-remote" class="top">远程模型 ID</label>
          <input id="mf-remote" class="ctl" maxlength="120" placeholder="云端／远程：供应商那边的模型 ID">
          <label for="mf-caps" class="top">能力声明</label>
          <div>
            <input id="mf-caps" class="ctl" maxlength="200" placeholder="逗号分隔，例如：图生图, 参考图">
            <p class="hint" style="margin-top:6px">人工声明未核验前，界面会标成「人工声明 · 未核验」。
              清单里已登记的参数（比如收不收参考图）不用在这里重复写。</p>
          </div>
          <label for="mf-note" class="top">备注</label>
          <textarea id="mf-note" class="ctl" maxlength="500" placeholder="为什么登记它、哪天核过"></textarea>
          <span class="pf-label"></span>
          <label class="pf-check"><input type="checkbox" id="mf-enabled" checked> 登记后立即启用</label>
        </div>
        <p class="modal-err" id="mf-error"></p>
        <p class="modal-meta">登记不会下载权重、不会启动推理、也不会改默认轨。
          清单里没有这一档也允许登记，但界面会标「清单未登记」，提交侧照旧拒。</p>
      </div>
      <div class="modal-actions">
        <button class="btn" data-close="mc-form">取消</button>
        <button class="btn primary" id="mf-submit">保存登记</button>
      </div>
    </div>
  </div>

  <div class="modal" id="mc-resolve" hidden>
    <div class="modal-box wide">
      <div class="modal-head neutral">场景候选</div>
      <div class="modal-body"><div id="mc-resolve-body"></div></div>
      <div class="modal-actions"><button class="btn" data-close="mc-resolve">关闭</button></div>
    </div>
  </div>

  <div class="modal" id="val-modal" hidden>
    <div class="modal-box wide">
      <div class="modal-head neutral" id="val-title">验证记录</div>
      <div class="modal-body">
        <div id="val-summary"></div>
        <h3 class="drawer-h">从作业登记</h3>
        <div class="form-grid">
          <label for="val-job">作业号</label>
          <input id="val-job" class="ctl" type="number" min="1" placeholder="一条已成功的作业 #id">
          <label for="val-kind">出处</label>
          <select id="val-kind" class="ctl">
            <option value="real_call">真实调用</option>
            <option value="target_machine">目标机基准</option>
            <option value="artifact_review">产物验收（人工看片）</option>
            <option value="mock">模拟（假适配器）</option>
          </select>
          <label for="val-passed" id="val-passed-label">验收结论</label>
          <select id="val-passed" class="ctl">
            <option value="pass">通过</option><option value="fail">不通过</option>
          </select>
          <label for="val-detail">说明</label>
          <input id="val-detail" class="ctl" maxlength="500"
                 placeholder="例如 在目标机上跑通 640×352×81，耗时与峰值显存见作业">
          <span class="pf-label"></span>
          <label class="pf-check"><input type="checkbox" id="val-paid"> 云端调用已确认计费</label>
        </div>
        <p class="modal-meta">真实调用、目标机基准与产物验收都读一条已成功的作业；云端调用必须先有计费确认。
          产物验收记的是人看过文件后的结论，通过与否必须明说。环境检查请用列表里的「环境检查」按钮，不走这里。</p>
        <p class="modal-err" id="val-error"></p>
        <h3 class="drawer-h">最近记录</h3>
        <div id="val-list"><p class="hint">正在读验证历史……</p></div>
      </div>
      <div class="modal-actions">
        <button class="btn" id="val-close">关闭</button>
        <button class="btn primary" id="val-submit">登记</button>
      </div>
    </div>
  </div>`;

/* ================= 视图 ================= */
export default {
  subIndex: 0,

  async render() {
    const sub = getSub('models') ?? 0;
    this.subIndex = sub;
    if (!store.can('system:config')) {
      return `<section class="view on">
        <div class="page-head"><h1>模型中心</h1><p>${SUBS[sub]}</p>
          <div class="right">${subChipRow(SUBS, sub)}</div></div>
        <div class="card"><div class="empty">
          <div class="empty-t">没有权限</div>
          <p>模型中心只有管理员能打开。要看现在走哪条模型轨，
             生成中心的发起弹窗里就写着；要改，找管理员。</p>
        </div></div></section>`;
    }

    const load = await loadAll();
    if (load.error) {
      return `<section class="view on">
        <div class="page-head"><h1>模型中心</h1><p>${SUBS[sub]}</p></div>
        <div class="card"><div class="empty">
          <div class="empty-t">读不到模型数据</div><p>${escapeHtml(load.error)}</p>
        </div></div></section>`;
    }

    const body = sub === 0 ? pageRepo() : sub === 1 ? pageEngines()
      : sub === 2 ? pagePolicies() : pageMonitor();
    const s = cache.stats;

    return `
    <section class="view on">
      <div class="page-head">
        <h1>模型中心</h1>
        <p>模型登记、引擎真相、调用策略与运行概览；选路与排队仍归生成中心</p>
        <div class="right">
          ${subChipRow(SUBS, sub)}
          <button class="btn" id="mc-check-all">检查引擎连接</button>
          <button class="btn" id="mc-goto-monitor">查看运行异常</button>
          <button class="btn primary" id="mc-add">登记模型</button>
        </div>
      </div>
      ${overviewCard(s)}
      ${body}
      ${MODALS}
    </section>`;
  },

  mount(root) {
    bindSubChips(root, (i) => {
      setSub('models', i);
      window.dispatchEvent(new CustomEvent('app:refresh'));
    });

    /* ---------- 页头三个入口 ---------- */
    root.querySelector('#mc-add')?.addEventListener('click', () => openForm(null));
    root.querySelector('#mc-goto-monitor')?.addEventListener('click', () => {
      setSub('models', 3); window.dispatchEvent(new CustomEvent('app:refresh'));
    });
    root.querySelector('#mc-check-all')?.addEventListener('click', async () => {
      const filled = cache.providers.filter((r) => r.provider);
      if (!filled.length) { alertNotify('十条能力轨都还没填 provider，没有可检查的对象。'); return; }
      const btn = root.querySelector('#mc-check-all');
      btn.disabled = true;
      const ok = await run('环境检查', async () => {
        // 逐条走和列表页同一个接口：不发请求、不加载权重，只留痕
        for (const r of filled) {
          await api.validateProvider({ capability: r.capability, backend: r.backend,
                                       provider: r.provider, kind: 'env_check' });
        }
      });
      btn.disabled = false;
      if (ok) window.dispatchEvent(new CustomEvent('app:refresh'));
    });

    if ((getSub('models') ?? 0) === 0) mountRepo(root);
    if ((getSub('models') ?? 0) === 1) mountEngines(root);
    if ((getSub('models') ?? 0) === 2) mountPolicies(root);
    if ((getSub('models') ?? 0) === 3) mountMonitor(root);

    root.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', () => {
      root.querySelector('#' + b.dataset.close).hidden = true;
    }));
    root.querySelectorAll('.modal').forEach((m) => {
      m.addEventListener('click', (e) => { if (e.target === m) m.hidden = true; });
      bindModalDismiss(m, () => { m.hidden = true; });
    });
  },
};

/* ================= 数据装载 ================= */
async function loadAll() {
  try {
    const [catalog, manifest, providers, policies, stats, worker, validations] =
      await Promise.all([api.models(), api.providerManifest(), api.providers(),
                        api.modelPolicies(), api.modelStats(), api.workerStatus(),
                        api.providerValidations(50)]);
    cache = { catalog, manifest, providers, policies, stats, worker, validations };
    return { error: '' };
  } catch (err) {
    // 部分失败不整页挡死：登记读不到时至少还能看能力轨。真读不到才给空态。
    try {
      const [providers, manifest] = await Promise.all([api.providers(), api.providerManifest()]);
      cache.providers = providers; cache.manifest = manifest;
      cache.catalog = { items: [], capabilities: {}, status_labels: {},
                        unintegrated_engines: [] };
      cache.stats = { registered_total: 0, available_total: 0, needs_attention: 0,
                      engine_total: 0, engine_params_known: 0, unintegrated_engines: [],
                      queued: 0, running: 0, registered_by_capability: {} };
      cache.policies = { items: [], preference_labels: {}, no_auto_fallback: true };
      return { error: '' };
    } catch (e2) {
      return { error: e2.message || err.message };
    }
  }
}

/* ================= 工作区 0 交互 ================= */
function findItem(key) {
  return cache.catalog.items.find((i) => `${i.provider}/${i.model_code}` === key);
}

function mountRepo(root) {
  const rows = [...root.querySelectorAll('#repo-rows tr')];
  const count = root.querySelector('#repo-count');
  const apply = () => {
    const q = (root.querySelector('#repo-search').value || '').trim().toLowerCase();
    const cap = root.querySelector('#repo-cap').value;
    const src = root.querySelector('#repo-source').value;
    let n = 0;
    rows.forEach((tr) => {
      const ok = (!q || tr.dataset.name.includes(q))
        && (!cap || tr.dataset.cap === cap) && (!src || tr.dataset.source === src);
      tr.style.display = ok ? '' : 'none';
      if (ok) n += 1;
    });
    count.textContent = `当前筛选 ${n} / ${rows.length} 档`;
  };
  ['repo-search', 'repo-cap', 'repo-source'].forEach((id) => {
    root.querySelector('#' + id).addEventListener(
      id === 'repo-search' ? 'input' : 'change', apply);
  });
  apply();

  root.querySelectorAll('[data-detail]').forEach((b) => b.addEventListener('click', () => {
    const it = findItem(b.dataset.detail);
    if (!it) return;
    openDetail(root, it);
  }));
  root.querySelectorAll('[data-register]').forEach((b) => b.addEventListener('click', () => {
    const it = findItem(b.dataset.register);
    if (it) openForm(null, it);
  }));
  // 检查／编辑／撤登记三个动作搬进了详情面板底部（见 openDetail），
  // 行内只留「详情」与「登记」两个入口。
}

function openDetail(root, it) {
  root.querySelector('#mc-detail-title').textContent =
    `${it.display_name || it.model_label || it.model_code} · ${it.provider}/${it.model_code}`;
  const params = (it.params || []).map((p) => {
    const bits = [];
    if (p.default !== null && p.default !== undefined) bits.push(`默认 ${p.default}`);
    if (p.min != null && p.max != null) bits.push(`${p.min}—${p.max}`);
    if (p.multiple_of) bits.push(`${p.multiple_of} 的倍数`);
    return `<div class="kv"><dt><code>${escapeHtml(p.name)}</code></dt><dd>${escapeHtml(bits.join(' · '))}
      ${p.note ? `<div class="t-desc">${escapeHtml(p.note)}</div>` : ''}</dd></div>`;
  }).join('');
  root.querySelector('#mc-detail-body').innerHTML = `
    <div class="kv-grid">
      <div class="kv"><dt>状态</dt><dd>${statusChip(it, cache.catalog.status_labels)}
        <span class="t-desc">${escapeHtml(it.status_detail || '')}</span></dd></div>
      <div class="kv"><dt>健康</dt><dd>${healthChip(it.health)}
        <span class="t-desc">${escapeHtml(it.health?.detail || '')}</span></dd></div>
      <div class="kv"><dt>运行位置</dt><dd>${escapeHtml(sourceLabel(it.source))} ·
        ${escapeHtml(it.engine_label || it.engine_code || '—')}</dd></div>
      <div class="kv"><dt>版本</dt><dd>${escapeHtml(it.version || '未填')}</dd></div>
      <div class="kv"><dt>权重路径</dt><dd class="mono">${escapeHtml(it.artifact_path || '未填')}</dd></div>
      <div class="kv"><dt>远程 ID</dt><dd class="mono">${escapeHtml(it.remote_model_id || '未填')}</dd></div>
      <div class="kv"><dt>能力标签</dt><dd>${tagChips(it.tags)}</dd></div>
      <div class="kv"><dt>计价</dt><dd>${priceChips(it.prices)}</dd></div>
      <div class="kv"><dt>目录变量</dt><dd class="mono">${escapeHtml(it.home_env || '不适用')}</dd></div>
      <div class="kv"><dt>清单说明</dt><dd class="t-desc">${escapeHtml(it.manifest_note || '无')}</dd></div>
      <div class="kv"><dt>验证出处</dt><dd>${(it.validation_kinds || []).map((k) =>
        `<span class="chip green">${escapeHtml(VAL_KIND_LABEL[k] || k)}</span>`).join(' ')
        || '<span class="hint">一条验证记录都没有</span>'}</dd></div>
      <div class="kv"><dt>备注</dt><dd class="t-desc">${escapeHtml(it.note || '无')}</dd></div>
    </div>
    ${params ? `<h3 class="drawer-h">允许参数（来自清单）</h3><div class="kv-grid">${params}</div>` : ''}`;

  /* 动作跟着详情一起进来：这里看得见状态、健康、验证出处与路径，
     做"撤登记"这种决定比在一排小按钮里点靠谱。
     每次开面板都重绑——面板是同一个 DOM，但对应的行每次都可能不同。 */
  const acts = root.querySelector('#mc-detail-acts');
  if (!it.registration_id) {
    acts.innerHTML = `<button class="btn" data-act-register="${escapeHtml(`${it.provider}/${it.model_code}`)}">登记这一档</button>`;
  } else {
    acts.innerHTML = `
      <button class="btn" data-act-check="${it.registration_id}">环境检查</button>
      <button class="btn" data-act-edit="${it.registration_id}">编辑登记</button>
      <button class="btn danger" data-act-remove="${it.registration_id}">撤登记…</button>`;
  }
  const close = () => { root.querySelector('#mc-detail').hidden = true; };
  acts.querySelector('[data-act-check]')?.addEventListener('click', async (e) => {
    e.target.disabled = true;
    const ok = await run('检查', () => api.checkModel(Number(e.target.dataset.actCheck)));
    e.target.disabled = false;
    if (ok) { close(); window.dispatchEvent(new CustomEvent('app:refresh')); }
  });
  acts.querySelector('[data-act-edit]')?.addEventListener('click', () => {
    close(); openForm(it.registration_id, it);
  });
  acts.querySelector('[data-act-register]')?.addEventListener('click', () => {
    close(); openForm(null, it);
  });
  acts.querySelector('[data-act-remove]')?.addEventListener('click', async () => {
    const yes = await confirmDanger({
      title: '撤登记',
      body: '这一步只删除这条登记：权重文件仍在盘上，历史作业与验证记录一律不动。'
            + '要改默认轨请去「引擎管理」的能力轨表。',
      okText: '撤登记',
    });
    if (!yes) return;
    close();
    await run('撤登记', () => api.removeModel(it.registration_id));
    window.dispatchEvent(new CustomEvent('app:refresh'));
  });
  root.querySelector('#mc-detail').hidden = false;
}

/** 登记弹窗：regId 给了就是编辑，不给就是新增；preset 用来从清单行直接带出键。 */
function openForm(regId, preset) {
  const modal = document.querySelector('#mc-form');
  if (!modal) return;
  const err = modal.querySelector('#mf-error');
  err.textContent = '';
  modal.querySelector('#mc-form-title').textContent = regId ? '编辑登记' : '登记模型';
  const capSel = modal.querySelector('#mf-cap');
  capSel.innerHTML = Object.entries(cache.catalog.capabilities)
    .map(([k, v]) => `<option value="${k}">${escapeHtml(v)}</option>`).join('');
  // 运行位置的取值也来自接口：登记层比选型层多一档 remote，
  // 原来这里硬写三个 option，后端加第四档时界面永远不会知道
  modal.querySelector('#mf-be').innerHTML = Object.entries(sourceLabels())
    .map(([k, v]) => `<option value="${k}">${escapeHtml(v)}</option>`).join('');
  const set = (id, value) => { modal.querySelector(id).value = value ?? ''; };
  capSel.value = preset?.capability || 'image';
  modal.querySelector('#mf-be').value = preset?.backend || 'local';
  set('#mf-prov', preset?.provider); set('#mf-model', preset?.model_code);
  set('#mf-name', preset?.display_name); set('#mf-ver', preset?.version);
  set('#mf-path', preset?.artifact_path); set('#mf-remote', preset?.remote_model_id);
  set('#mf-caps', (preset?.tags || []).filter((t) => t.source === 'manual')
    .map((t) => t.tag).join(', '));
  set('#mf-note', preset?.note);
  modal.querySelector('#mf-enabled').checked = preset ? !!preset.enabled : true;
  // 编辑时能力／运行位置／provider／模型标识是登记行的键，改键等于换一档——
  // 那应该是撤了再登记。只禁用而不在提交里删字段是不够的：用户会以为改了，
  // 保存后却什么都没变。后端 PATCH 也不收 backend/capability，口径两边一致。
  modal.querySelector('#mf-prov').disabled = !!regId;
  modal.querySelector('#mf-model').disabled = !!regId;
  modal.querySelector('#mf-cap').disabled = !!regId;
  modal.querySelector('#mf-be').disabled = !!regId;
  modal.hidden = false;

  const submit = modal.querySelector('#mf-submit');
  submit.onclick = async () => {
    err.textContent = '';
    const provider = modal.querySelector('#mf-prov').value.trim();
    const modelCode = modal.querySelector('#mf-model').value.trim();
    if (!provider || !modelCode) {
      err.textContent = 'provider 与模型标识都要填——这两个键决定登记的是哪一档。';
      return;
    }
    const caps = modal.querySelector('#mf-caps').value.split(/[,，]/)
      .map((s) => s.trim()).filter(Boolean);
    const body = {
      // source 是"运行位置"的人话口径，必须和这次选的后端一起提交：
      // 后端字段默认空串会跟 backend 走，但显式带上更不容易再漂。
      capability: capSel.value, backend: modal.querySelector('#mf-be').value,
      source: modal.querySelector('#mf-be').value,
      provider, model_code: modelCode,
      display_name: modal.querySelector('#mf-name').value.trim(),
      version: modal.querySelector('#mf-ver').value.trim(),
      artifact_path: modal.querySelector('#mf-path').value.trim(),
      remote_model_id: modal.querySelector('#mf-remote').value.trim(),
      capabilities: caps, note: modal.querySelector('#mf-note').value.trim(),
      enabled: modal.querySelector('#mf-enabled').checked,
    };
    submit.disabled = true;
    let ok;
    if (regId) {
      const patch = { ...body };
      delete patch.provider; delete patch.model_code;
      delete patch.capability; delete patch.backend;
      // source 跟着这四个键一起固定：界面没有独立的"人话运行位置"输入，
      // 放它进 patch 会把接口侧手工标的 remote 静默改回本地。
      delete patch.source;
      ok = await run('保存登记', () => api.patchModel(regId, patch));
    } else {
      ok = await run('登记', () => api.createModel(body));
    }
    submit.disabled = false;
    if (ok) { modal.hidden = true; window.dispatchEvent(new CustomEvent('app:refresh')); }
  };
}

/* ================= 工作区 1 交互（能力轨，功能原样搬自系统设置） ================= */
function mountEngines(root) {
  const saveBtn = root.querySelector('#save-providers');
  const saveStateChip = root.querySelector('#save-state');
  let rowsCache = cache.providers;

  /* 展开/收起。整页重绘是唯一改 DOM 的路子，但会把正在输入的框和光标一起弄丢，
     所以只在"展开态变了"时重绘；改 provider/model 走下面 refresh() 的局部更新。 */
  /* 展开/收起要重画整页：这是全站切页与保存后共用的那一条路（app:refresh），
     不另发明一套局部重绘。代价是此刻若有人正在输入框里打字会被打断——
     所以只在"展开态变了"时走它，改 provider/model 走下面 refresh() 的局部更新。 */
  const repaint = () => window.dispatchEvent(new CustomEvent('app:refresh'));
  root.querySelectorAll('tr.slot td:first-child').forEach((td) => {
    td.style.cursor = 'pointer';
    td.title = '点开看清单出处、允许参数、密钥与验证动作';
    td.addEventListener('click', () => {
      const id = Number(td.closest('tr').dataset.id);
      slotOpen.has(id) ? slotOpen.delete(id) : slotOpen.add(id);
      repaint();
    });
  });
  root.querySelectorAll('[data-open-empty]').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    emptyOpen.add(Number(b.dataset.openEmpty));
    slotOpen.add(Number(b.dataset.openEmpty));   // 去配置的人下一步就是要填细节
    repaint();
  }));
  root.querySelector('#slots-expand')?.addEventListener('click', () => {
    if (slotOpen.size) {
      slotOpen.clear();
    } else {
      cache.providers.filter((r) => r.provider).forEach((r) => slotOpen.add(r.id));
    }
    repaint();
  });

  root.querySelectorAll('input[data-f="default"]').forEach((radio) => {
    radio.addEventListener('change', () => {
      radio.closest('tr').querySelector('[data-f="enabled"]').checked = true;
    });
  });

  // 改 provider/model 只刷新这一行的提示，不重绘整页——重绘会丢输入框焦点
  root.querySelectorAll('tr.slot').forEach((tr) => {
    const refresh = () => {
      const provider = tr.querySelector('[data-f="provider"]').value;
      const model = tr.querySelector('[data-f="model"]').value;
      const adapter = adapterOf(cache.manifest, tr.dataset.cap, tr.dataset.backend, provider);
      const dl = tr.querySelector(`datalist[id="model-${tr.dataset.id}"]`);
      if (dl) {
        dl.innerHTML = (adapter?.models || [])
          .map((m) => `<option value="${escapeHtml(m.code)}">${escapeHtml(m.label)}</option>`).join('');
      }
      // 展开态才有细节行；没展开就只等下一次重绘
      const detail = root.querySelector(`tr.mc-detail[data-detail-for="${tr.dataset.id}"]`);
      if (detail) {
        detail.querySelector('.mc-src').innerHTML = noteText(adapter, provider);
        detail.querySelector('.mc-params').innerHTML = paramsCell(adapter, model, provider);
        const keyInput = detail.querySelector('[data-f="key_env"]');
        if (keyInput) keyInput.placeholder = adapter?.key_env || 'FV_KEY_<PROVIDER>';
      }
      // 健康是服务端按"已保存的 provider"算的；这一行还没保存时先别摆旧结论，
      // 否则改完 provider 还亮着"未配置"，看着像输入没生效。
      tr.querySelector('.mc-health').innerHTML =
        '<span class="chip gray">待保存</span><span class="hint">保存后按新 provider 重新检查</span>';
    };
    tr.querySelector('[data-f="provider"]').addEventListener('input', refresh);
    tr.querySelector('[data-f="model"]').addEventListener('input', refresh);
  });

  saveBtn.addEventListener('click', async () => {
    // 必须以**服务端那十行**为骨架，不能只扫 DOM 里的 tr.slot：
    // 密度收口之后，没填 provider 的槽位折成 tr.mc-collapsed（不带输入框），
    // 只扫 tr.slot 就只提交 3 行，而 PUT /api/providers 要求"每类能力恰好一个默认"
    // ——图像／配音／音乐三类的默认行恰好都在折叠行里，于是整批被拒：
    // 「每类能力都要选一个默认后端：图像、配音、音乐」。按钮点了没反应，
    // 界面只会弹一句像是自己填错的话。
    const rows = (rowsCache.length ? rowsCache : cache.providers).map((saved) => {
      const tr = root.querySelector(`tr.slot[data-id="${saved.id}"]`);
      const detail = root.querySelector(`tr.mc-detail[data-detail-for="${saved.id}"]`);
      if (!tr) {
        // 折叠行：原样回读服务端那份。把这一槽的 provider／model／启用／默认
        // 一起丢了，等于"整批保存成功、顺带把没展开的槽位清空"
        return {
          id: saved.id, provider: saved.provider, model: saved.model,
          enabled: saved.enabled, is_default: saved.is_default,
          key_env: saved.key_env_custom ?? saved.key_env ?? '',
          notes: saved.notes ?? '',
        };
      }
      return {
        id: saved.id,
        provider: tr.querySelector('[data-f="provider"]').value,
        model: tr.querySelector('[data-f="model"]').value,
        enabled: tr.querySelector('[data-f="enabled"]').checked,
        is_default: tr.querySelector('[data-f="default"]').checked,
        key_env: detail
          ? detail.querySelector('[data-f="key_env"]').value
          : (saved.key_env_custom ?? saved.key_env ?? ''),
        notes: detail ? detail.querySelector('[data-n="notes"]').value : (saved.notes ?? ''),
      };
    });
    saveBtn.disabled = true;
    const ok = await run('保存', () => api.saveProviders(rows));
    saveBtn.disabled = false;
    if (ok) {
      saveStateChip.hidden = false;
      setTimeout(() => { saveStateChip.hidden = true; }, 2000);
      try {
        const fresh = await api.providers();
        rowsCache = fresh;
        cache.providers = fresh;
        fresh.forEach(paintRow);
        bindValidationActions(root);
      } catch { /* 回读失败不影响保存本身 */ }
    }
  });

  function paintRow(r) {
    const tr = root.querySelector(`tr.slot[data-id="${r.id}"]`);
    if (!tr) return;
    tr.querySelector('.mc-health').innerHTML = healthCell(r.health);
    tr.querySelector('.mc-validation').innerHTML = validationCell(r);
  }

  async function reloadRows() {
    const fresh = await api.providers();
    rowsCache = fresh; cache.providers = fresh;
    fresh.forEach(paintRow);
    bindValidationActions(root);
    return fresh;
  }

  function summaryHtml(v) {
    const kinds = ['env_check', 'mock', 'real_call', 'target_machine', 'artifact_review'];
    const rows = kinds.map((k) => {
      const item = v?.[k] || {};
      const at = item.at ? String(item.at).slice(0, 16) : '—';
      const job = item.job_id ? ` · 作业 #${item.job_id}` : '';
      return `<div class="kv"><dt>${escapeHtml(VAL_KIND_LABEL[k])}</dt>
        <dd>${stateChip(item.state || 'never')}
          <span class="t-dim">${escapeHtml(at)}${escapeHtml(job)}</span>
          ${item.detail ? `<span class="t-desc">${escapeHtml(item.detail)}</span>` : ''}</dd></div>`;
    }).join('');
    const ready = v?.production_ready
      ? '<span class="chip green">生产可用</span>'
      : '<span class="chip gray">未达生产可用</span>';
    const target = v?.target_machine_ready ? '<span class="chip green">目标机已验证</span>' : '';
    return `<div class="kv-grid">${rows}</div>
      <p class="modal-meta">${ready} ${target}
        <span class="hint">生产可用＝环境齐＋真实调用过＋产物验收通过，由服务端判定；界面不自己拼。</span></p>`;
  }

  function valListHtml(records) {
    if (!records.length) return '<p class="hint">这一轨还没有验证记录。</p>';
    return `<div class="table-wrap"><table><thead><tr>
      <th>出处</th><th>结果</th><th>作业</th><th>说明</th><th>登记人</th><th>时间</th>
      </tr></thead><tbody>${records.map((r) => `<tr>
        <td><span class="chip plain">${escapeHtml(r.kind_label || VAL_KIND_LABEL[r.kind] || r.kind)}</span></td>
        <td>${stateChip(r.state)}</td>
        <td>${r.job_id ? `#${r.job_id}` : '—'}</td>
        <td class="t-desc">${escapeHtml(r.detail || '')}</td>
        <td>${escapeHtml(r.created_by_name || '—')}</td>
        <td class="t-dim">${escapeHtml(String(r.created_at || '').slice(0, 16))}</td>
      </tr>`).join('')}</tbody></table></div>`;
  }

  const valModal = root.querySelector('#val-modal');
  let valSlot = null;

  async function openValModal(slot) {
    valSlot = slot;
    valModal.hidden = false;
    root.querySelector('#val-title').textContent =
      `验证记录 · ${slot.capability_label} · ${slot.backend_label}`;
    root.querySelector('#val-summary').innerHTML = summaryHtml(slot.validation);
    root.querySelector('#val-job').value = '';
    root.querySelector('#val-kind').value = 'real_call';
    root.querySelector('#val-detail').value = '';
    root.querySelector('#val-paid').checked = false;
    root.querySelector('#val-error').textContent = '';
    syncPassedRow();
    root.querySelector('#val-list').innerHTML = '<p class="hint">正在读验证历史……</p>';
    try {
      const all = await api.providerValidations(100);
      root.querySelector('#val-list').innerHTML = valListHtml(all.filter((r) =>
        r.capability === slot.capability && r.backend === slot.backend
        && r.provider === slot.provider));
    } catch (err) {
      root.querySelector('#val-list').innerHTML =
        `<p class="t-bad">验证历史读不到：${escapeHtml(err.message)}</p>`;
    }
    root.querySelector('#val-job').focus();
  }

  function syncPassedRow() {
    const isReview = root.querySelector('#val-kind').value === 'artifact_review';
    root.querySelector('#val-passed-label').hidden = !isReview;
    root.querySelector('#val-passed').hidden = !isReview;
  }

  async function submitValidation() {
    const errEl = root.querySelector('#val-error');
    errEl.textContent = '';
    if (!valSlot) return;
    const jobId = Number(root.querySelector('#val-job').value);
    if (!jobId) { errEl.textContent = '填一条已成功的作业号。'; return; }
    const kind = root.querySelector('#val-kind').value;
    const body = {
      capability: valSlot.capability, backend: valSlot.backend,
      provider: valSlot.provider, model: valSlot.model,
      kind, job_id: jobId,
      paid_confirmed: root.querySelector('#val-paid').checked,
      detail: root.querySelector('#val-detail').value.trim(),
    };
    if (kind === 'artifact_review') body.passed = root.querySelector('#val-passed').value === 'pass';
    const btn = root.querySelector('#val-submit');
    btn.disabled = true;
    try {
      await api.validateJob(jobId, body);
      const fresh = await reloadRows();
      valSlot = fresh.find((r) => r.id === valSlot.id) || valSlot;
      root.querySelector('#val-summary').innerHTML = summaryHtml(valSlot.validation);
      const all = await api.providerValidations(100);
      root.querySelector('#val-list').innerHTML = valListHtml(all.filter((r) =>
        r.capability === valSlot.capability && r.backend === valSlot.backend
        && r.provider === valSlot.provider));
      root.querySelector('#val-job').value = '';
      root.querySelector('#val-detail').value = '';
    } catch (err) {
      errEl.textContent = err.message || '登记失败';
    } finally {
      btn.disabled = false;
    }
  }

  // 重画验证列后要重绑，所以用 onclick 赋值而不是 addEventListener（不叠监听器）
  function bindValidationActions(scope) {
    scope.querySelectorAll('[data-env-check]').forEach((btn) => {
      btn.onclick = async () => {
        const slot = rowsCache.find((r) => String(r.id) === btn.dataset.envCheck);
        if (!slot) return;
        btn.disabled = true;
        const ok = await run('环境检查', () => api.validateProvider({
          capability: slot.capability, backend: slot.backend,
          provider: slot.provider, kind: 'env_check',
        }));
        btn.disabled = false;
        if (ok) await reloadRows().catch(() => {});
      };
    });
    scope.querySelectorAll('[data-val-history]').forEach((btn) => {
      btn.onclick = () => {
        const slot = rowsCache.find((r) => String(r.id) === btn.dataset.valHistory);
        if (slot) openValModal(slot);
      };
    });
  }

  bindValidationActions(root);
  root.querySelector('#val-close').addEventListener('click', () => { valModal.hidden = true; });
  root.querySelector('#val-submit').addEventListener('click', submitValidation);
  root.querySelector('#val-kind').addEventListener('change', syncPassedRow);
}

/* ================= 工作区 2 交互 ================= */
function mountMonitor(root) {
  // 折叠/展开离线进程。走 app:refresh 重画整页（与引擎页展开同一条件：
  // 只有重画才能让页内切换器与表体同一轮更新），展开状态存在模块变量里，
  // 所以轮询不会把人刚点开的那段又折回去。
  root.querySelector('#mc-worker-history')?.addEventListener('click', () => {
    showWorkerHistory = !showWorkerHistory;
    window.dispatchEvent(new CustomEvent('app:refresh'));
  });
}

function mountPolicies(root) {
  // 把当前值填进 select：渲染时没带 selected，统一在这里回填，省一遍字符串拼接
  root.querySelectorAll('tr.policy').forEach((tr) => {
    const key = tr.dataset.key;
    const p = cache.policies.items.find((x) => x.scene_key === key);
    if (!p) return;
    tr.querySelector('[data-f="slot"]').value = p.slot_provider ? `${p.slot_provider}|${p.slot_model}` : '';
    tr.querySelector('[data-f="fallback"]').value = p.fallback_provider ? `${p.fallback_provider}|${p.fallback_model}` : '';
    tr.querySelector('[data-f="pref"]').value = p.preference;
  });

  root.querySelector('#save-policies').addEventListener('click', async () => {
    const rows = [...root.querySelectorAll('tr.policy')].map((tr) => {
      const p = cache.policies.items.find((x) => x.scene_key === tr.dataset.key);
      const slot = tr.querySelector('[data-f="slot"]').value.split('|');
      const fb = tr.querySelector('[data-f="fallback"]').value.split('|');
      return {
        scene_key: tr.dataset.key, scene_label: p.scene_label, capability: p.capability,
        required: p.required,
        slot_provider: slot[0] || '', slot_model: slot[1] || '',
        preference: tr.querySelector('[data-f="pref"]').value,
        fallback_provider: fb[0] || '', fallback_model: fb[1] || '',
        resource_limit: p.resource_limit || {},
        cloud_confirm_required: tr.querySelector('[data-f="cloud"]').checked,
        enabled: tr.querySelector('[data-f="on"]').checked,
      };
    });
    const errEl = root.querySelector('#policy-error');
    const btn = root.querySelector('#save-policies');
    errEl.textContent = '';
    btn.disabled = true;
    try {
      await api.saveModelPolicies(rows);
      btn.disabled = false;
      window.dispatchEvent(new CustomEvent('app:refresh'));
    } catch (err) {
      btn.disabled = false;
      // 这里不走全站那个 alert：后端的校验是"一次说清所有问题"，
      // 四五条错误拼成一句长文本塞进系统弹窗，人既读不完也复制不走。
      // 原话留在表下面，改一条就能对着看。
      errEl.textContent = err.message || '保存失败';
    }
  });

  root.querySelectorAll('[data-resolve]').forEach((b) => b.addEventListener('click', async () => {
    const modal = root.querySelector('#mc-resolve');
    const body = root.querySelector('#mc-resolve-body');
    modal.hidden = false;
    body.innerHTML = '<p class="hint">正在按能力匹配……</p>';
    // 请求序号：连点两行时两条候选都会回来，**后到的那条覆盖先到的**——
    // 而"后到"未必是"后点的"（同屏两个请求，一个走缓存一个不走）。
    // 音频中心与产线页的同场景早就加了这道闸（audio.js:157／pipeline.js:790），
    // 这一处是漏的那个。
    const wanted = ++resolveSeq;
    let out;
    try { out = await api.resolveScene(b.dataset.resolve); } catch (err) {
      if (wanted !== resolveSeq) return;
      body.innerHTML = `<p class="t-bad">候选读不到：${escapeHtml(err.message)}</p>`;
      return;
    }
    if (wanted !== resolveSeq) return;   // 已经被更新的一次点击接管了
    const cand = out.candidates.map((c) => `<tr>
      <td><b>${escapeHtml(c.label)}</b><div class="hint mono">${escapeHtml(c.provider)}/${escapeHtml(c.model_code)}</div></td>
      <td><span class="chip ${c.role === 'fallback' ? 'blue' : 'plain'}">${c.role === 'fallback' ? '备用轨' : '建议轨'}</span></td>
      <td><span class="chip ${c.status === 'available' ? 'green' : 'amber'}">${escapeHtml(c.status_label)}</span></td>
      <td>${c.needs_cloud_confirm ? '<span class="chip amber">要逐次确认计费</span>' : '<span class="hint">本地</span>'}</td>
    </tr>`).join('');
    body.innerHTML = `
      <p class="modal-meta">${escapeHtml(out.policy_note || '')}</p>
      <h3 class="drawer-h">满足必需能力的候选（${out.candidates.length}）</h3>
      ${out.candidates.length
        ? `<div class="table-wrap"><table><thead><tr><th>模型</th><th>角色</th><th>状态</th><th>云端</th></tr></thead>
            <tbody>${cand}</tbody></table></div>`
        : '<p class="hint">一条都没有。</p>'}
      <h3 class="drawer-h">为什么别的不行（${out.blockers.length}）</h3>
      <ul class="hint">${out.blockers.map((b) => `<li>${escapeHtml(b)}</li>`).join('') || '<li>没有</li>'}</ul>
      <p class="modal-meta">必需能力：${out.required.map(escapeHtml).join('、') || '（没写）'}。
        这一屏只回答"谁可能行"；要真用它，请在发起弹窗里显式选，或在能力轨把它设为默认。</p>`;
  }));
}

/* 页头的"检查引擎连接"用一条轻提示代替弹窗：整批检查是 10 个 POST，
   结果本身已经写进各行的健康列，不需要再弹一层。 */
function alertNotify(text) {
  const el = document.createElement('div');
  el.className = 'chip amber';
  el.textContent = text;
  el.style.cssText = 'position:fixed;right:24px;bottom:24px;z-index:40;padding:10px 14px';
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}
