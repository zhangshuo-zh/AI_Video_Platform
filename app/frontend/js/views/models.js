import { api } from '../api.js';
import { store } from '../store.js';
import {
  agoText, bindModalDismiss, bindSubChips, confirmDanger, escapeHtml, run, subChipRow,
} from '../util.js';
import { getSub, setSub } from '../navstate.js';
import { HEALTH, VAL_BADGE, VAL_KIND_LABEL, alertLabel } from '../validation.js';
import { REG_SOURCES, unitLabel } from '../enums.js';

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
// VAL_KIND_LABEL 也从 validation.js 来：权威译名在后端 `VALIDATION_KINDS`
// 并随记录下发 `kind_label`，逐行渲染优先读那个字段，这份只兜"没有行"的场合。
const VAL_STATE = {
  ok: ['green', '通过'], missing: ['red', '缺东西'], unknown: ['amber', '未检查'],
  unconfigured: ['gray', '未配置'], unregistered: ['red', '未登记'],
  failed: ['red', '失败'], never: ['gray', '没有记录'],
};

const norm = (s) => String(s || '').trim().toLowerCase();

// 一次 render 把四份数据都缓存下来；mount 里的交互不重新发请求，
// 免得点一下筛选就把整页的读请求打一遍
let cache = { catalog: null, manifest: null, providers: null, policies: null,
              stats: null, worker: null, engines: null,
              // 验证历史是{ items, total }：分页读，总量由响应头给。
              validations: { items: [], total: 0 },
              // 翻页状态：`before` 是下一页的游标（当前页最后一条的 id），
              // 空表示"从头开始"。存在模块变量里，翻页不重画整页。
              validationPage: { before: null, pages: 0 } };

/**
 * 四个子页各要哪些数据。
 *
 * 为什么按子页取而不是一次全取：切一次标签页就打七个接口，其中三个只有别的
 * 子页会读（验证历史只给引擎页与监控页用、worker 状态只给监控页用）。
 * 目录与清单是公共底座，两页都要——它们已经合并成一条接口 + 一次 stats。
 *
 * `providers` 四页都要：页头那颗「检查引擎连接」在任何子页都可用，
 * 它要读十条能力轨的现状，缺了就只能弹出"没有可检查的对象"这种假话。
 */
const SUB_NEEDS = [
  { providers: true },
  { providers: true, validations: true, engines: true },
  { providers: true, policies: true },
  { providers: true, worker: true, validations: true },
];

function stateChip(state) {
  const [tone, label] = VAL_STATE[state] || ['gray', state || '未知'];
  return `<span class="chip ${tone}">${escapeHtml(label)}</span>`;
}

/**
 * 健康徽章。「待现查」与「未检查」不是同一档，必须分开：
 *
 * `unknown` 是"平台检查不了，需要人确认"——那是结论；
 * `probe_deferred` 是"平台会查，只是这条读路径不代你连网"——那是一句待办。
 * 前者人去做别的判断，后者人只要点一下「探测」。两件事共用一个词，
 * 结果就是"点了没反应"或"以为坏了"。
 */
function healthChip(health) {
  if (health?.probe_deferred) {
    return `<span class="chip amber" title="${escapeHtml(health?.detail || '')}">待现查</span>`;
  }
  const [tone, label] = HEALTH[health?.state] || ['gray', '未知'];
  return `<span class="chip ${tone}" title="${escapeHtml(health?.detail || '')}">${label}</span>`;
}

/** 登记层的运行位置 → 清单层的轴。判据由后端下发（`manifest.axis`）。
 *
 * 这一段原来写死成 `backend === "remote" ? "local" : backend`，与后端
 * `adapters.MANIFEST_AXIS` 是同一句话的第二份抄写——后端哪天多折一档，
 * 界面会安静地按老口径算，症状是"清单里有、下拉里不显示"。
 * 接口没给（老后端）就原样返回：宁可对不上，也不自己造一个折法。 */
function manifestAxis(backend) {
  return (cache.manifest && cache.manifest.axis || {})[backend] || backend;
}

function adapterOf(manifest, cap, backend, provider) {
  if (!manifest || !provider) return null;
  const be = (manifest.axis || {})[backend] || backend;
  return (manifest.adapters || []).find((a) => a.capability === cap
    && a.backend === be
    && (norm(a.code) === norm(provider)
      || (a.aliases || []).some((x) => norm(x) === norm(provider))));
}

/** 一条 option 的写法，两个键共用。 */
const pickOpt = (value, label) => `<option value="${escapeHtml(value)}">${
  escapeHtml(label)}</option>`;

const sameCode = (a, provider) => {
  const p = norm(provider);
  return !!p && (norm(a.code) === p || (a.aliases || []).some((x) => norm(x) === p));
};

/**
 * 清单里能对上「能力 × 运行位置」的 provider。
 * remote 折回 local 再比——折回判据来自 `manifest.axis`（后端那一份），
 * 这里不自己写 `=== "remote" ? ...`：两边各写一份就会出现
 * 「接口认、界面不给选」或反之的档。
 */
function manifestProviders(cap, backend) {
  const be = manifestAxis(backend);
  return (cache.manifest?.adapters || [])
    .filter((a) => a.capability === cap && a.backend === be);
}

/** 模型标识的当前值：下拉与手填框两个控件只有一个可见。 */
function pickedModel(modal) {
  const free = modal.querySelector('#mf-model-free');
  return (free.hidden
    ? modal.querySelector('#mf-model') : free).value.trim();
}

/**
 * 重填 Provider／模型标识这两格。选项只来自 GET /api/providers/manifest，
 * 不再让人自由填——登记一档 provider 的前提是平台知道它的参数与运行方式。
 * 三种边界各留一句可见的说法，而不是把人堵在空下拉前面：
 * 1. 这一「能力 × 运行位置」清单里没有档 → 下拉显示「清单里没有这一档」并禁用，提交挡下；
 * 2. provider 只登记了名字、清单没枚举模型 → 模型 ID 退回手填，旁边写明为什么；
 * 3. keepForeign（编辑一条既有登记）→ 值对不上清单也照样列出来并标「清单未登记」，
 *    否则下拉把它显示成空白，人会以为键丢了。
 */
function fillPicks(modal, cap, backend, provider, model, keepForeign) {
  const provSel = modal.querySelector('#mf-prov');
  const modelSel = modal.querySelector('#mf-model');
  const modelFree = modal.querySelector('#mf-model-free');
  const modelNote = modal.querySelector('#mf-model-note');
  const modelLabel = modal.querySelector('label[for="mf-model"], label[for="mf-model-free"]');
  // 编辑态这两格是登记行的键，改键＝换一档：由 openForm 打在 data 上，
  // 重填时一起算，否则改完能力再回来会把该禁的框放开了。
  const locked = modal.dataset.keysLocked === '1';

  const list = manifestProviders(cap, backend);
  // 目标 provider 对不上清单时怎么办，分两种：
  // 编辑（keepForeign）要把原值留在下拉上标「清单未登记」；
  // 新增或刚换了能力／运行位置，就落到新清单的第一档——停在空白上
  // 会让人以为清单坏了，而其实只是这一格跟着换了内容。
  let hit = list.find((a) => sameCode(a, provider));
  if (!hit && provider && !keepForeign) hit = list[0];
  if (!hit && !provider) hit = list[0];
  const opts = list.map((a) => pickOpt(a.code, a.label));
  if (keepForeign && provider && !hit) opts.push(pickOpt(provider, `${provider}（清单未登记）`));
  provSel.innerHTML = opts.length ? opts.join('') : pickOpt('', '清单里没有这一档');
  provSel.value = hit ? hit.code : (provider || '');
  // 清单里没有这一档时上面那句会把值停在一个不存在的 option 上，
  // selectedIndex 就成 -1：人看到的是空白下拉，读代码的人以为清单挂了。
  if (provSel.selectedIndex < 0) provSel.selectedIndex = 0;
  provSel.disabled = locked || !opts.length;

  const tiers = hit?.models || [];
  const codes = tiers.map((m) => m.code);
  const inList = !!model && codes.some((c) => norm(c) === norm(model));
  const foreign = !!(keepForeign && model && !inList && codes.length);
  const tierOpts = tiers.map((m) => pickOpt(m.code, m.label || m.code));
  if (foreign) tierOpts.push(pickOpt(model, `${model}（清单未登记）`));
  // 清单没枚举模型（只登记了名字的 provider，或清单外的 provider）就退回手填：
  // 前者是供应商自己的命名，后者连参数都不该被猜。
  const byHand = !codes.length;
  modelSel.hidden = byHand;
  modelFree.hidden = !byHand;
  modelNote.hidden = !byHand;
  if (modelLabel) modelLabel.setAttribute('for', byHand ? 'mf-model-free' : 'mf-model');
  if (byHand) {
    modelFree.value = keepForeign ? (model || '') : '';
    modelFree.disabled = locked || !list.length;
    modelNote.textContent = hit
      ? '这一档的清单只登记了 provider 名，没有模型档：模型 ID 按供应商那边的叫法手填。'
      : (list.length
          ? 'provider 不在清单里：参数与运行方式都不会被猜，提交侧会被拒。'
          : '没有可登记的档：清单里没有这个「能力 × 运行位置」，界面上也不让你造一个键。');
  } else {
    modelSel.innerHTML = tierOpts.join('');
    modelSel.value = (inList || foreign) ? model : codes[0];
    modelSel.disabled = locked;
  }
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
  // caliber 缺了不许直接读：读 `s.caliber.x` 在降级兜底那一条路上会抛 TypeError，
  // 而视图 render 抛错的结果是整页停在上一屏（人只看到"点了没反应"）。
  // 这里退化成空串，让标题拦不到出处，也比白屏好。
  const cal = s.caliber || {};
  const degraded = s.degraded
    ? `<p class="note" style="margin:8px 0 0"><span class="chip amber">口径不全</span>
        <span class="hint">目录接口这次没读回来（${escapeHtml(String(s.degraded))}），
        下面这几个数不是真的 0，是空位。切到别的页再回来会重试。</span></p>`
    : '';
  return `
  <div class="card" style="margin-bottom:12px">
    <div class="mc-strip">
      <span class="st" title="${escapeHtml(cal.registered_total || '')}">已登记
        <b>${s.registered_total ?? 0}</b>
        <span class="hint">${escapeHtml(capText || '还没有登记')}</span></span>
      <span class="st" title="${escapeHtml(cal.available_total || '')}">可用
        <b>${s.available_total ?? 0}</b>
        ${s.needs_attention
          ? `<span class="chip amber mc-attention" title="点这里只看这些行">${s.needs_attention} 个要看一眼</span>`
          : (s.registered_total
              ? '<span class="chip green">都齐了</span>'
              /* 空机器的 needs_attention 恒为 0（登记−可用），绿色「都齐了」
                 会把"什么都没配"念成"全部就绪"。0 登记时这不是结论，是起点。 */
              : '<span class="chip amber" title="先在模型仓库登记一档，再谈可用">还没有登记</span>')}</span>
      <span class="st" title="${escapeHtml(cal.engine_total || '')}">引擎
        <b>${s.engine_total ?? 0}</b>
        <span class="hint">${s.engine_params_known || 0} 个参数已核验 ·
          ${(s.unintegrated_engines || []).length} 个未接入</span></span>
      <span class="st" title="与生成中心同一个口径：${escapeHtml(cal.queued || '')}">任务
        <b>${(s.running || 0) + (s.queued || 0)}</b>
        <span class="hint">跑 ${s.running || 0} · 等 ${s.queued || 0}</span></span>
      <span class="st-caliber">可用＝已登记 ∧ 已启用 ∧ 健康就绪 ∧ 这一档自己有过真实调用或产物验收。
        引擎在线不代表模型可用，文件在也不代表依赖齐。</span>
    </div>
    ${degraded}
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
    // 单位名从 enums.js 那一份取：这里原来自己抄了三档（second／million_token／image），
    // 抄出来的「百万 token」和后端「百万 Token」差一个大小写，而没抄的四档
    // （token／request／minute／character）会直接把英文 code 念在价格牌上。
    const unit = unitLabel(p.unit);
    const when = Object.entries(p.when || {}).map(([k, v]) => `${k}=${v}`).join(' ');
    return `<span class="chip plain" title="${escapeHtml(p.source)}">
      ${yuan} ${escapeHtml(p.currency)}/${escapeHtml(unit)}${when ? ' · ' + escapeHtml(when) : ''}</span>`;
  }).join('');
}

function statusChip(item, labels) {
  // 三个「缺东西」的档：灰＝本机没登记（点一下就解决）、红＝清单里没有（改代码）、
  // 琥珀＝清单只有名字（等规格补齐）。颜色不同的理由见后端 STATUS_LABEL 上面那段注解。
  const tone = { available: 'green', unverified: 'amber', manifest_only: 'gray',
                 unchecked: 'amber', disabled: 'gray', not_installed: 'gray',
                 dependency_missing: 'red', connection_failed: 'red',
                 registration_only: 'red', name_only: 'amber' }[item.status] || 'gray';
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
  // 待现查的那一档，状态与健康说的是两件事（"未检查"＋"待现查"），
  // 不能按文字撞车去重——按老规则它俩不撞车，会并排出现两个黄徽章，
  // 看着像两条不同的毛病。这里只留状态那一枚，理由写在下面。
  if (item.health?.probe_deferred) return s;
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
        data-rid="${it.registration_id || ''}"
        ${it.registered && it.status !== 'available' ? 'data-attention="1"' : ''}>
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

  // 「全部类型」与「全部来源」两个下拉的选项都来自接口（capabilities／reg_sources），
  // 不在这里硬写。原来来源那三个 option 有两个毛病：写的是英文取值
  // （local／cloud／remote，而同一张表「运行位置」那一列念的是中文），
  // 而且后端加第四档时界面永远不会知道。登记弹窗里那份（见 #mf-be）早就是生成的，
  // 两处口径现在一致。
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
          ${Object.entries(sourceLabels())
            .map(([k, v]) => `<option value="${k}">${escapeHtml(v)}</option>`).join('')}
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
      <p class="note"><b>三种"缺东西"缺的不是同一侧，处置也相反</b>：
        「本机未登记」＝清单里有、这台机器没登记（点一下「登记」就解决）；
        「清单无此档」＝本机登记了、清单里没有这一档（点多少次都没用，要去 <code>adapters.py</code> 补规格或撤掉这条登记）；
        「清单只有名字」＝清单只知道这一家的名字、没枚举模型档（等规格补齐，先手填模型标识）。
        只列已登记的档，"装了不等于配了"就看不见；所以第二种照样列出来，但用红徽章跟第一种分开。
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
  if (!adapter) return '<span class="hint">清单里没有这一家 provider；参数不猜，运行方式也不猜</span>';
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
  const [tone, label] = health?.probe_deferred
    ? ['amber', '待现查']
    : (HEALTH[health?.state] || ['gray', '未知']);
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
  (cache.validations?.items || []).forEach((v) => {
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
  </div>

  ${engineInstancesCard()}`;
}

/* ================= 工作区 1b · 引擎实例（0040） ================= */
/**
 * 这一张卡是"端点住在哪"的那一格。后端 W-B 就建好了表与五个接口，
 * 但界面一直没接——于是 `engines.probe()` 的提示「在「引擎管理 · 引擎实例」里
 * 加一条端点」指向一个不存在的界面，而人只能去改库。
 *
 * 三条与后端同一口径的边界，卡片里必须写出来：
 *   * engine_code 必须在清单里（实例不能凭空发明 provider）；
 *   * 健康是**现查**的，库里没有那一列，所以"停用的实例不探测"；
 *   * 密钥只到"变量名 + 有没有值"，值永不显示。
 */
/**
 * 「上次探测」那一行（0042）。
 *
 * 为什么卡面上要有它：`GET /api/engines` 的健康是**这一次**现查的，而
 * 「上次是什么时候探的」是另一件事——engine 那一族以前不落任何痕，所以
 * "这台上周还连不连得上"谁也答不上来。现在有了留痕，就得把最近的结论摆出来，
 * 否则人还是会以为"我看到的这个绿色是刚才测的"。
 *
 * 三档写法，各有各的必要：
 *   从没探过 → 明说"从没探测过"，不假装刚查过；
 *   建实例时顺手探的 → 标明"建实例时"，别让人以为是自己点过；
 *   人按过「探测」    → 给时间（相对时间，全站同一定义）与耗时。
 */
function probeHistoryLine(it) {
  if (!it.last_probe_at) {
    return '<div class="hint">还没有探测记录</div>';
  }
  const when = String(it.last_probe_at).slice(0, 16);
  const rel = agoText(it.last_probe_age_seconds);
  const via = it.last_probe_source === 'create' ? '建实例时顺手探的' : '手动探测';
  const ms = it.last_probe_latency_ms != null ? ` · ${it.last_probe_latency_ms} ms` : '';
  return `<div class="hint" title="${escapeHtml(it.last_probe_detail || '')}">
    上次 ${escapeHtml(when)}${rel ? `（${escapeHtml(rel)}）` : ''}
    · ${escapeHtml(via)}${escapeHtml(ms)}</div>`;
}

/**
 * 一条实例的探测历史（0042）。
 *
 * 为什么用弹窗而不是卡面上多一行：卡面那一行只回答「上次什么时候探的」，
 * 而「这台是不是三天两头掉线」要看几条才答得上来。20 条塞进表格一行会把
 * 整张表撑成三层楼，所以另开一屏。
 *
 * 三条要知道的事都写在这一屏里：
 *  1. 历史**只回答过去**——现在通不通仍看卡面上那枚现查的徽章；
 *  2. `create` 那条是「建实例时顺手探的」，不是人点过；
 *  3. 每行都给了耗时与距上一次的间隔，掉线有没有规律一眼看得出来。
 */
function openProbeHistory(data) {
  const items = data.items || [];
  const rows = items.map((it, i) => {
    const next = items[i + 1];
    // 与更早那条之间隔了多久：同一个端点连着探两次都说连不上，
    // 和「隔了三天才探一次」，说的不是一回事。
    const gap = next && it.age_seconds != null && next.age_seconds != null
      ? fmtGap(it.age_seconds - next.age_seconds) : '';
    return `<tr>
      <td class="t-dim">${escapeHtml(String(it.created_at || '').slice(0, 16))}</td>
      <td>${healthChip({ state: it.state, detail: it.detail })}</td>
      <td class="hint">${it.source === 'create' ? '建实例时顺手探的' : '手动探测'}</td>
      <td class="hint mono">${escapeHtml(it.engine_model || '—')}</td>
      <td class="num t-dim">${it.latency_ms != null ? it.latency_ms + ' ms' : '—'}</td>
      <td class="hint">${escapeHtml(gap || '')}</td>
    </tr>`;
  }).join('');
  const body = items.length
    ? `<div class="table-wrap"><table><thead><tr>
        <th>什么时候</th><th>结果</th><th>怎么探的</th><th>引擎自述的模型</th>
        <th class="num">耗时</th><th>距上一次</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
    : '<p class="hint">这条实例还没有探测记录。点「探测」会留下第一条。</p>';
  const host = document.createElement('div');
  host.className = 'modal';
  host.innerHTML = `<div class="modal-box wide">
      <div class="modal-head neutral">探测历史</div>
      <div class="modal-body">${body}
        <p class="note" style="margin:12px 0 0"><b>这一屏只讲过去。</b>
          现在通不通，看引擎实例卡上那枚状态徽章——它是每次现查的，
          和这里的任何一条都没关系。${escapeHtml((data && data.note) || '')}</p>
      </div>
      <div class="modal-actions"><button class="btn" data-close-probes>关闭</button></div>
    </div>`;
  document.body.appendChild(host);
  const close = () => host.remove();
  host.addEventListener('click', (e) => {
    if (e.target === host || e.target.closest('[data-close-probes]')) close();
  });
  host.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
}

/** 两条探测之间隔了多久。数字都是秒；负值（同一秒内）当 0。 */
function fmtGap(seconds) {
  const n = Math.max(0, Number(seconds) || 0);
  if (n < 60) return `${n} 秒`;
  if (n < 3600) return `${Math.floor(n / 60)} 分钟`;
  if (n < 86400) return `${Math.floor(n / 3600)} 小时`;
  return `${Math.floor(n / 86400)} 天`;
}

/** 引擎实例那一张卡（0040 端点表 ＋ 0042 探测留痕）。 */
function engineInstancesCard() {
  const inst = cache.engines
    || { items: [], connection_types: ['local_http'], service_engines: [] };
  const items = inst.items || [];
  const service = inst.service_engines || [];

  // 可选的引擎 code：清单里出现过的都算（后端 `_adapter_for` 只认清单），
  // 但服务型排前面并标出来——只有它们会真发 HTTP 去问端点。
  const seen = new Set();
  const choices = [];
  (cache.manifest?.adapters || []).forEach((a) => {
    if (seen.has(a.code)) return;
    seen.add(a.code);
    choices.push({ code: a.code, label: a.label, served: service.includes(a.code) });
  });
  choices.sort((a, b) => (b.served - a.served) || a.code.localeCompare(b.code));
  const codeOpts = choices.map((c) => `<option value="${escapeHtml(c.code)}">${
    escapeHtml(c.label)}（${escapeHtml(c.code)}）${c.served ? '' : ' · 非服务型'}</option>`).join('');

  const rows = items.map((it) => {
    const engine = choices.find((c) => c.code === it.engine_code);
    const name = it.display_name || it.engine_label || it.engine_code;
    const secret = it.shared_secret_env
      ? `<code>${escapeHtml(it.shared_secret_env)}</code>
         ${it.secret_present ? '<span class="chip green">有值</span>'
                            : '<span class="chip amber">变量没值</span>'}`
      : '<span class="hint">—</span>';
    return `<tr class="engine-inst" data-inst="${it.id}">
      <td class="t-strong">${escapeHtml(name)}
        <div class="hint mono">${escapeHtml(it.engine_code)}${
          engine && !engine.served ? ' · 非服务型' : ''}</div></td>
      <td><code>${escapeHtml(it.endpoint)}</code></td>
      <td><span class="hint">${escapeHtml(it.connection_type)}</span> ${secret}</td>
      <td>${it.enabled
          ? (it.probe_unsupported
              ? '<span class="chip amber">探测未实现</span>'
              : healthChip({ state: it.health_state, detail: it.health_detail }))
          : '<span class="chip gray">已停用</span>'}
        ${it.engine_model ? `<div class="hint mono">${escapeHtml(it.engine_model)}</div>` : ''}
        ${it.health_detail ? `<div class="hint">${escapeHtml(it.health_detail)}</div>` : ''}
        ${probeHistoryLine(it)}</td>
      <td class="t-dim">${escapeHtml(it.note || '')}</td>
      <td class="c" style="white-space:nowrap">
        <button class="btn xs" data-act-probe="${it.id}">探测</button>
        <button class="btn xs" data-act-probes="${it.id}">看历史</button>
        <button class="btn xs" data-act-toggle="${it.id}" data-to="${it.enabled ? 0 : 1}">${
          it.enabled ? '停用' : '启用'}</button>
        <button class="btn xs danger" data-act-uninst="${it.id}">撤实例</button>
      </td>
    </tr>`;
  }).join('');

  const empty = items.length ? '' : `<tr><td colspan="6"><span class="hint">
    还没有启用中的引擎实例。Draw Things、Ollama 这类"跑成一台常驻服务"的引擎，
    要先在这里写一条端点（例如 <code>http://127.0.0.1:7860</code>），
    模型仓库那边才查得到它的健康状况。</span></td></tr>`;

  return `
  <div class="card">
    <div class="card-head"><h2>引擎实例</h2>
      <span class="sub">这一档引擎在哪台机器、哪个端口；健康是每次现查，不落库</span></div>
    <div class="table-wrap"><table>
      <thead><tr><th>实例</th><th>端点</th><th>连接方式（密钥变量）</th>
        <th>状态</th><th>备注</th><th class="c">操作</th></tr></thead>
      <tbody>${rows}${empty}
        <tr class="mc-add-inst">
          <td><select class="ctl sm" id="eng-code">${codeOpts || '<option value="">清单里还没有引擎</option>'}</select></td>
          <td><input class="ctl sm mono" id="eng-endpoint" maxlength="200"
                     placeholder="http://127.0.0.1:7860"></td>
          <td><input class="ctl sm" id="eng-name" maxlength="80" placeholder="显示名（可空）">
            <input class="ctl sm mono" id="eng-secret" maxlength="60" style="margin-top:4px"
                   placeholder="密钥变量名（可空，值只从进程环境读）"></td>
          <td colspan="2"><input class="ctl sm" id="eng-note" maxlength="300"
                   placeholder="备注：这台是谁的、为什么换机（可空）"></td>
          <td class="c"><button class="btn xs primary" id="eng-add">加实例</button></td>
        </tr>
      </tbody>
    </table></div>
    <div class="card-body" style="border-top:1px solid var(--line)">
      <p class="note" style="margin:0"><b>端点改不了</b>：端点连同引擎 code 是这一行的键，
        要换机器请新建一条再停旧的——否则历史作业的「当时打的是哪台」就说不清了。
        「非服务型」那几档也能登记（给远程执行器留的位置），但现在没有探测分支，
        加进来只会显示"不是本机服务型引擎，平台不猜它的状态"。</p>
      <p class="note"><b>连接方式今天只有 local_http 一档能选</b>：迁移 0040 的 CHECK 还收
        <code>grpc</code> 与 <code>cli</code>，但探测与执行都只写了 HTTP 那一条路。
        把它们摆进下拉，等于给一个选进去看不到任何行为的选项——所以接口直接拒，
        并明说"要先写探测与执行分支"。库里若有历史行用了别的值，这一屏照样列出来，
        状态写成「探测未实现」，不拿一次 HTTP 的失败冒充它的结论。</p>
    </div>
  </div>`;
}

/* ================= 工作区 2 · 调用策略 ================= */
/**
 * 场景那一格的徽章：**「配不出来」与「待配置」不是一件事**。
 *
 * 前者是清单里这一轴压根没有能产出那项能力的来源（例如 image 轴没有任何一档
 * 登记过图片输入参数，而某个场景要「图生图」）——下一步是写适配器，点界面没用。
 * 后者只是还没人登记、还没人点探测——下一步是去配。
 * 两种都写成"待配置"，人就会对着一个永远点不亮的场景反复点（2026-11-07 审查里
 * 4 个场景正是这样）。判据来自后端（`impossible_required`），界面不自己推。
 */
function policyStateChip(p) {
  if (p.required_producible === false) {
    const missing = (p.impossible_required || []).join('、');
    return `<span class="chip red" title="这些必需能力在清单里没有任何来源：${
      escapeHtml(missing)}；要让它有解得先写适配器，或者把这一项从「必需」降下来">
      清单缺能力</span>`;
  }
  return p.enabled ? '<span class="chip green">已配置</span>'
                   : '<span class="chip gray">待配置</span>';
}
function pagePolicies() {
  const items = cache.policies.items;
  const candidatesOf = (capability) => cache.catalog.items
    .filter((i) => i.capability === capability && i.registered)
    .map((i) => `<option value="${escapeHtml(i.provider)}|${escapeHtml(i.model_code)}">
        ${escapeHtml(i.display_name || i.model_label || i.model_code)}</option>`).join('');

  /* 必需能力从只读 chip 放开成勾选：《使用说明书》§5.4 本来就教人
     "把这一项从「必需」降下来"，服务端 save_policies 也一直在写 required_json——
     规则齐了，入口没开，于是 character_variant（清单缺「参考图」来源）
     只能挂一枚红 chip 让人反复配。勾选项来自后端算好的标签全集
     （producible，与派生标签同源），界面不自己发明第四个标签名；
     清单产不出的那一项仍然显示，但划灰并写明"清单里没有任何来源"。 */
  const requiredPicker = (p) => (p.producible || []).map((tag) => {
    const on = p.required.includes(tag);
    const impossible = (p.impossible_required || []).includes(tag);
    return `<label class="pf-check mc-req" title="${escapeHtml(
      impossible ? '清单里没有任何一档能产出它；勾了也不会有候选' : '作为这一场景的必需能力')}">
      <input type="checkbox" data-req="${escapeHtml(tag)}" ${on ? 'checked' : ''}>
      <span ${impossible ? 'style="text-decoration:line-through;opacity:.55"' : ''}>${
        escapeHtml(tag)}${impossible ? '（清单缺来源）' : ''}</span></label>`;
  }).join('') || '<span class="hint">这一轴清单里还没有任何可声明的标签</span>';

  const rows = items.map((p) => {
    const cur = p.slot_provider ? `${p.slot_provider}|${p.slot_model}` : '';
    const fb = p.fallback_provider ? `${p.fallback_provider}|${p.fallback_model}` : '';
    return `<tr class="policy" data-key="${escapeHtml(p.scene_key)}">
      <td><b>${escapeHtml(p.scene_label || p.scene_key)}</b>
        <div class="hint mono">${escapeHtml(p.scene_key)}</div></td>
      <td>${escapeHtml(p.capability_label)}</td>
      <td><div class="param-chips" style="flex-direction:column;align-items:flex-start;max-width:220px">
        ${requiredPicker(p)}</div></td>
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
      <td>${policyStateChip(p)}
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
function validationPager() {
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
        <td class="t-desc">${escapeHtml(v.detail || '')}</td>
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

/* ================= 弹窗 ================= */
const MODALS = `
  <div class="modal" id="mc-detail" hidden role="dialog" aria-modal="true"
       aria-labelledby="mc-detail-title">
    <div class="modal-box wide">
      <div class="modal-head neutral" id="mc-detail-title">模型详情</div>
      <div class="modal-body"><div id="mc-detail-body"></div></div>
      <div class="modal-actions">
        <span id="mc-detail-acts" style="margin-right:auto"></span>
        <button class="btn" data-close="mc-detail">关闭</button>
      </div>
    </div>
  </div>

  <div class="modal" id="mc-form" hidden role="dialog" aria-modal="true"
       aria-labelledby="mc-form-title">
    <div class="modal-box wide">
      <div class="modal-head neutral" id="mc-form-title">登记模型</div>
      <div class="modal-body">
        <div class="form-grid">
          <label for="mf-cap" class="top">能力</label>
          <select id="mf-cap" class="ctl"></select>
          <label for="mf-be" class="top">运行位置</label>
          <select id="mf-be" class="ctl"></select>
          <label for="mf-prov">Provider</label>
          <select id="mf-prov" class="ctl"></select>
          <label for="mf-model">模型标识</label>
          <!-- 模型标识跟着 provider 走：清单枚举了档位就是下拉，
               只登记了名字的档没有可选项，退回手填并在下面写清为什么。 -->
          <div>
            <select id="mf-model" class="ctl"></select>
            <input id="mf-model-free" class="ctl" maxlength="80" hidden
                   placeholder="供应商那边叫什么就填什么">
            <p class="hint" id="mf-model-note" hidden></p>
          </div>
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
        <p class="hint" id="mf-lock-note" hidden></p>
        <p class="modal-err" id="mf-error"></p>
        <p class="modal-meta">登记不会下载权重、不会启动推理、也不会改默认轨。
          Provider 与模型标识只列清单里有的档（规格住代码，界面不猜没登记过的 provider）；
          清单只登记了 provider 名的那一档没有模型可列，模型 ID 仍由你手填。</p>
      </div>
      <div class="modal-actions">
        <button class="btn" data-close="mc-form">取消</button>
        <button class="btn primary" id="mf-submit">保存登记</button>
      </div>
    </div>
  </div>

  <div class="modal" id="mc-resolve" hidden role="dialog" aria-modal="true"
       aria-labelledby="mc-resolve-title">
    <div class="modal-box wide">
      <div class="modal-head neutral" id="mc-resolve-title">场景候选</div>
      <div class="modal-body"><div id="mc-resolve-body"></div></div>
      <div class="modal-actions"><button class="btn" data-close="mc-resolve">关闭</button></div>
    </div>
  </div>

  <div class="modal" id="val-modal" hidden role="dialog" aria-modal="true"
       aria-labelledby="val-title">
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

    const load = await loadAll(sub);
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
        // 先请后端**现查**一次服务型引擎（fresh=1 绕过缓存）：打开页面时那一格是
        // "待现查"，只有显式动作才去连网——这一颗按钮就是那个显式动作。
        // 它只读、不建 Job、不改配置，结论随整张目录一起回来。
        await api.probeModels(true);
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
async function loadAll(sub) {
  try {
    // 顶部四数跟着 `GET /api/models` 一起回来（后端 `stats(rows)` 用的就是同一份 rows）。
    // 所以这里不再单独要 `/api/models/stats`：那一次请求会把整张目录连同每一行的
    // 引擎探测重算一遍，而算出来的数字必须与下面这张表逐行对得上。
    // 老地址还在（命令行与验收脚本按它取数），只是这一页不走它了。
    //
    // 引擎实例只在「引擎管理」那一屏取：`GET /api/engines` 会**逐台真发 HTTP 探测**，
    // 四个子页都取等于把没人看的那一屏也探一遍。切到别的屏时它是 null，
    // 那一屏根本不读它。
    // **按子页取**：四个子页各需要的东西不一样，一次全取等于每切一次标签页
    // 都打七个接口（其中三个只有别的子页会读）。桌面端看不出来，本机接口
    // 一慢就能感觉到"点一下标签要等一秒"。
    //
    // 公共底座（两个下拉的取值、状态译名、顶部四数）只有目录一条接口——
    // 它已经带 stats，不再单独要 `/api/models/stats`（那一次会把整张目录重算一遍）。
    //
    // 目录**不出网**（后端 2026-11-07 起）：服务型引擎那一格在列表里是"待现查"，
    // 要现查请按页头的「检查引擎连接」或那一行的「探测」。
    const need = SUB_NEEDS[sub] || SUB_NEEDS[0];
    const [catalog, manifest] = await Promise.all([api.models(), api.providerManifest()]);
    const [providers, policies, worker, validations, engines] = await Promise.all([
      need.providers ? api.providers() : null,
      need.policies ? api.modelPolicies() : null,
      need.worker ? api.workerStatus() : null,
      need.validations ? api.providerValidations(50) : null,
      need.engines ? api.engines() : null,
    ]);
    // 兼容两种响应：后端给了 stats 就用它；万一部署的是老版本（没有这个键），
    // 再退回单独取一次——宁可多一次请求，也不能让顶部四数空着。
    const stats = catalog.stats || await api.modelStats();
    cache = { catalog, manifest,
              providers: providers || cache.providers || [],
              policies: policies || cache.policies || { items: [], preference_labels: {}, no_auto_fallback: true },
              worker: worker || cache.worker || {},
              // 验证历史是分页读的（一页 + 总量），缓存里存整个对象而不是数组：
              // 只存数组会把总量丢掉，监控页就只能说"这里有 20 条"，说不出"共多少条"。
              validations: validations || cache.validations || { items: [], total: 0 },
              // 整页重载＝回到第一页。翻页是模块内状态，不该跟着刷新一起漂。
              validationPage: validations ? { before: null, pages: 0 }
                                           : cache.validationPage,
              stats, engines };
    return { error: '' };
  } catch (err) {
    // 部分失败不整页挡死：登记读不到时至少还能看能力轨。真读不到才给空态。
    //
    // 但**降级必须说出来**：上一版在这里造了一份"全 0"的 stats 悄悄顶上，
    // 顶部四数就变成"已登记 0 / 可用 0 / 引擎 0"，看着像一台干净机器，
    // 而真相是目录接口挂了。现在两条都改：造出来的 stats 带上 caliber
    // （overviewCard 要读它，缺了会抛 TypeError 把整页钉在上一屏），
    // 并且打上 degraded 标记，由概览卡自己挂一条黄条。
    try {
      const [providers, manifest] = await Promise.all([api.providers(), api.providerManifest()]);
      cache.providers = providers; cache.manifest = manifest;
      cache.catalog = { items: [], capabilities: {}, status_labels: {},
                        unintegrated_engines: [] };
      // 这几个 0 是"空位"不是"事实"：口径那一句必须跟着数字一起给，
      // 否则界面只能猜哪一格是降级值。形状与真响应一致（键一个不少），
      // 少一个键就会把 "undefined" 印在顶上。
      // 这几个 0 是"空位"不是"事实"：每格的口径那一句必须跟着数字一起给。
      // 形状与真响应逐键一致——少一个键，概览卡就会把 undefined 加到数字上。
      const caliber = {
        registered_total: '目录接口没读回来，这个 0 不是"没有登记"',
        available_total: '同上：这一格现在是空位，不是真的 0',
        engine_total: '同上：引擎数来自目录，目录没回就是 0',
      };
      caliber.queued = '同上：队列数来自目录，目录没回就是 0';
      cache.stats = {
        registered_total: 0, available_total: 0, engine_total: 0,
        engine_params_known: 0, unintegrated_engines: [],
        needs_attention: 0, running: 0, registered_by_capability: {},
        degraded: err.message || String(err), caliber,
      };
      cache.stats.queued = 0;
      cache.policies = { items: [], preference_labels: {}, no_auto_fallback: true };
      cache.engines = null;
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
  /* 状态筛选是**临时视图**：概览那枚「N 个要看一眼」点进来时置位，
     人再动搜索或换筛选就清掉——它不是长期挂着的第四个下拉，
     挂上去就要回答"为什么刷新后还在"。 */
  let statusFilter = '';
  const apply = () => {
    const q = (root.querySelector('#repo-search').value || '').trim().toLowerCase();
    const cap = root.querySelector('#repo-cap').value;
    const src = root.querySelector('#repo-source').value;
    let n = 0;
    rows.forEach((tr) => {
      const ok = (!q || tr.dataset.name.includes(q))
        && (!cap || tr.dataset.cap === cap) && (!src || tr.dataset.source === src)
        && (!statusFilter || tr.dataset.attention === '1');
      tr.style.display = ok ? '' : 'none';
      if (ok) n += 1;
    });
    count.textContent = `当前筛选 ${n} / ${rows.length} 档`;
  };
  ['repo-search', 'repo-cap', 'repo-source'].forEach((id) => {
    root.querySelector('#' + id).addEventListener(
      id === 'repo-search' ? 'input' : 'change', () => {
        // 人一旦自己动筛选，概览点进来的那个临时视图就交还给搜索框。
        statusFilter = '';
        apply();
      });
  });
  // 概览那枚「N 个要看一眼」：点进来直接把"没到可用"的行筛出来。
  // needs_attention 的口径是「已登记但没到 available」，与 data-attention 同一把尺子。
  root.querySelector('.mc-attention')?.addEventListener('click', () => {
    statusFilter = 'attention';
    root.querySelector('#repo-search').value = '';
    root.querySelector('#repo-cap').value = '';
    root.querySelector('#repo-source').value = '';
    apply();
    root.querySelector('#repo-rows')?.scrollIntoView({ block: 'nearest' });
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
  const beSel = modal.querySelector('#mf-be');
  const provSel = modal.querySelector('#mf-prov');
  const modelFree = modal.querySelector('#mf-model-free');
  capSel.value = preset?.capability || 'image';
  beSel.value = preset?.backend || 'local';
  // 这两格的选项跟着能力／运行位置走，所以先打「键已锁」标记再填：
  // 编辑态键是登记行的主键，重填时也得保持灰的。
  modal.dataset.keysLocked = regId ? '1' : '';
  fillPicks(modal, capSel.value, beSel.value, preset?.provider,
            preset?.model_code, !!regId);
  // onchange 用赋值而不是 addEventListener：弹窗 DOM 是常驻的那一份，
  // 每次 openForm 再 add 一遍就会在人改一次能力时跑 N 遍重填。
  const rePick = () => fillPicks(modal, capSel.value, beSel.value,
                                 provSel.value, pickedModel(modal), false);
  capSel.onchange = rePick;
  beSel.onchange = rePick;
  provSel.onchange = rePick;
  set('#mf-name', preset?.display_name); set('#mf-ver', preset?.version);
  set('#mf-path', preset?.artifact_path); set('#mf-remote', preset?.remote_model_id);
  set('#mf-caps', (preset?.tags || []).filter((t) => t.source === 'manual')
    .map((t) => t.tag).join(', '));
  set('#mf-note', preset?.note);
  modal.querySelector('#mf-enabled').checked = preset ? !!preset.enabled : true;
  // 编辑时能力／运行位置／provider／模型标识是登记行的键，改键等于换一档——
  // 那应该是撤了再登记。只禁用而不在提交里删字段是不够的：用户会以为改了，
  // 保存后却什么都没变。后端 PATCH 也不收 backend/capability，口径两边一致。
  provSel.disabled = !!regId;
  modal.querySelector('#mf-model').disabled = !!regId;
  modelFree.disabled = !!regId;
  capSel.disabled = !!regId;
  beSel.disabled = !!regId;
  // 光把控件 disabled 不够：`.ctl` 原来没有禁用态样式，点不动会被当成页面坏了。
  // 这里把「为什么点不动、想改该怎么办」写在表单里，并在四个键上挂同一句 title。
  const lockNote = modal.querySelector('#mf-lock-note');
  if (lockNote) {
    lockNote.hidden = !regId;
    lockNote.textContent = regId
      ? '编辑只改显示名、版本、权重路径、能力声明与备注；'
        + '要换能力／运行位置／provider／模型标识，请先撤登记再重新登记（改键等于换一档）。'
      : '';
  }
  if (regId) {
    const why = '编辑登记时这一格锁住了：改键等于换一档，请先撤登记再重新登记。';
    for (const el of [capSel, beSel, provSel, modal.querySelector('#mf-model')]) {
      if (el) el.title = why;
    }
  }
  modal.hidden = false;

  const submit = modal.querySelector('#mf-submit');
  submit.onclick = async () => {
    err.textContent = '';
    const provider = provSel.value.trim();
    const modelCode = pickedModel(modal);
    if (!provider || !modelCode) {
      // 两种空法说的话不一样：下拉压根没得选＝清单里没有这一档，
      // 只漏了模型 ID＝那一档的清单只登记了名字、要人手填。
      err.textContent = provider
        ? '模型标识不能空——provider 与模型标识这两个键决定登记的是哪一档。'
        : `清单里没有「${cache.catalog.capabilities[capSel.value] || capSel.value}`
          + ` × ${sourceLabels()[beSel.value] || beSel.value}」这一档，界面登记不了。`
          + '要加 provider 得先把适配器写进 adapters.py（规格住代码）。';
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
    try {
      if (regId) {
        const patch = { ...body };
        delete patch.provider; delete patch.model_code;
        delete patch.capability; delete patch.backend;
        // source 跟着这四个键一起固定：界面没有独立的"人话运行位置"输入，
        // 放它进 patch 会把接口侧手工标的 remote 静默改回本地。
        delete patch.source;
        await api.patchModel(regId, patch);
      } else {
        await api.createModel(body);
      }
    } catch (e) {
      // 表单里的错就写在表单里。全局 `run()` 用 alert，headless 下会被自动
      // 关掉、人只看到"点了没反应"；而 422 的文案本来就是写给填表人看的
      // （例如「这一档已经登记过了，要改就编辑那一条」），原样贴进来最有用。
      // 401 不贴：那是"会话过期"，由统一闸门去弹登录，不该混进表单错误。
      if (!(e && e.status === 401)) {
        err.textContent = (e && e.message) ? e.message : String(e);
      }
      submit.disabled = false;
      return;
    }
    submit.disabled = false;
    modal.hidden = true;
    window.dispatchEvent(new CustomEvent('app:refresh'));
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
          // 折叠行也要带令牌：这一行同样可能被别人改过，漏掉它就等于
          // 只给"看得见的那三行"上闸，另外七行照样能被静默盖掉。
          seen_version: saved.version || '',
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
        // 弱版本闸：带回"我打开时看到的那一版"。服务端一比就知道有没有人插进来改过——
        // 这一页是整批覆盖，没这道闸时后保存的人会把前一个人改的那一行静默盖回旧值。
        seen_version: saved.version || '',
      };
    });
    saveBtn.disabled = true;
    let ok = true;
    try {
      await api.saveProviders(rows);
    } catch (err) {
      ok = false;
      if (err.status === 401) return;
      // 409 ＝ 这十行里有人被改过（弱版本闸）。不能当成普通失败弹一句就完：
      // 人手上还留着刚填的东西，得给一条出路。再点一次「保存」仍然会 409
      // （令牌没变），所以唯一的出路是回读最新、丢失格之后重新对一遍。
      if (err.status === 409) {
        const yes = await confirmDanger({
          title: '保存被挡下了',
          body: err.message + '。这次整批没有写入。重新读一遍会把最新的读进来，'
                + '你现在填的内容会丢——需要对着变化重新对一遍。',
          okText: '重新读一遍',
        });
        if (yes) window.dispatchEvent(new CustomEvent('app:refresh'));
        return;
      }
      // 走统一出口（`run` 只认抛出的错）：这一页原来不许直接 alert，
      // 全站错误出口只有 util.js 那一处——守卫用例会红。
      await run('保存', () => { throw err; });
      return;
    } finally {
      saveBtn.disabled = false;
    }
    if (!ok) return;      // 上面每一条失败分支都已经说过话，这里只负责成功那一路
    saveStateChip.hidden = false;
    setTimeout(() => { saveStateChip.hidden = true; }, 2000);
    try {
      // 回读一份最新：**新令牌必须跟着刷新**。不刷新的话，这一页从此带着一个
      // 过期令牌，用户接着改第二次就必然撞 409——一次成功之后整页再也存不进去，
      // 那比没有闸还糟。
      const fresh = await api.providers();
      rowsCache = fresh;
      cache.providers = fresh;
      fresh.forEach(paintRow);
      bindValidationActions(root);
    } catch { /* 回读失败不影响保存本身 */ }
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
      const { items: all } = await api.providerValidations(100);
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
      const { items: all } = await api.providerValidations(100);
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

  mountEngineInstances(root);
}

/* ================= 工作区 1b 交互 · 引擎实例 ================= */
/**
 * 加、停启用、探测、撤实例。四个动作全部走 `app:refresh` 整页重绘，
 * 不在这里手改 DOM：探测结果来自服务端现查，本地拼一个"就绪"就是在撒谎。
 *
 * 撤实例要二次确认（`confirmDanger`），而且确认文案必须说清它**不动什么**——
 * 这一格最容易被理解成"把 Draw Things 删了"。
 */
function mountEngineInstances(root) {
  const repaint = () => window.dispatchEvent(new CustomEvent('app:refresh'));
  const addBtn = root.querySelector('#eng-add');
  addBtn?.addEventListener('click', async () => {
    const body = {
      engine_code: root.querySelector('#eng-code').value,
      endpoint: root.querySelector('#eng-endpoint').value.trim(),
      display_name: root.querySelector('#eng-name').value.trim(),
      shared_secret_env: root.querySelector('#eng-secret').value.trim(),
      note: root.querySelector('#eng-note').value.trim(),
    };
    if (!body.engine_code) { alertNotify('先选一个引擎：清单里现在没有可选的一档。'); return; }
    if (!body.endpoint) { alertNotify('端点不能空——这一行的全部意义就是「这一档引擎在哪个地址」。'); return; }
    addBtn.disabled = true;
    let out = null;
    const ok = await run('加实例', async () => { out = await api.createEngine(body); });
    addBtn.disabled = false;
    if (!ok) return;
    // 新建时顺手探过一次；连不上不回 4xx（登记一个"还没启动的服务"是合法动作），
    // 那句 warning 必须让人看到，否则人以为加完就通了。
    if (out && out.warning) alertNotify(out.warning);
    repaint();
  });

  root.querySelectorAll('[data-act-probe]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      let out = null;
      const ok = await run('探测', async () => { out = await api.probeEngine(btn.dataset.actProbe); });
      btn.disabled = false;
      if (!ok || !out) return;
      alertNotify(out.state === 'ok'
        ? `连上了：${out.model || '（服务没报模型名）'} @ ${out.endpoint}`
        : `没连上：${out.detail}`);
      repaint();
    });
  });

  // 看历史：这一台最近几次探测。为什么不塞进卡面——20 条历史塞进一行会把
  // 那张表撑成三层楼；而「这台是不是三天两头掉线」是要停下来看的判断，
  // 顺手扫一眼能过的场合（上次什么时候探的、结果如何）卡面上那一行已经够了。
  root.querySelectorAll('[data-act-probes]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      let data = null;
      const ok = await run('读探测历史', async () => {
        data = await api.engineProbes(btn.dataset.actProbes);
      });
      btn.disabled = false;
      if (ok && data) openProbeHistory(data);
    });
  });

  root.querySelectorAll('[data-act-toggle]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const on = btn.dataset.to === '1';
      btn.disabled = true;
      const ok = await run(on ? '启用' : '停用',
        () => api.patchEngine(btn.dataset.actToggle, { enabled: on }));
      btn.disabled = false;
      if (ok) repaint();
    });
  });

  root.querySelectorAll('[data-act-uninst]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.actUninst;
      const yes = await confirmDanger({
        title: '撤这一条实例登记',
        body: '只删端点这一行：应用、权重与历史作业一律不动，旧作业仍按当时冻结的快照可查。'
          + '要换机器就撤了再登记——端点改不了，它连同引擎 code 是这一行的键。',
        okText: '撤实例',
        danger: true,
      });
      if (!yes) return;
      const ok = await run('撤实例', () => api.removeEngine(id));
      if (ok) repaint();
    });
  });
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
          <td class="t-desc">${escapeHtml(v.detail || '')}</td>
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
        // 必需能力读勾选框：那一格从只读 chip 放开成可编辑（§5.4 一直教人
        // "从「必需」降下来"，服务端也早会写 required_json，缺的只是入口）。
        required: [...tr.querySelectorAll('[data-req]:checked')].map((c) => c.dataset.req),
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
