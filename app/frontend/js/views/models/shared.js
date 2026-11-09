/**
 * 模型中心 · 共享底座。
 *
 * 这一份只放"四个子页都要用的东西"：缓存、常量、跨子页的纯函数。
 * 拆分的理由见 ../../README 那类模块说明——models.js 原来 2277 行，
 * 四个子页的渲染、交互、弹窗全挤在一个文件里，改一处要滚很远，
 * 而且搜索函数名会撞到同名片段。
 *
 * **这里不放任何子页的渲染函数**：谁的东西回谁那儿，shared 只当公共层。
 */
import { api } from '../../api.js';
import { store } from '../../store.js';
import { escapeHtml } from '../../util.js';
import { getSub, setSub } from '../../navstate.js';
import { HEALTH, VAL_BADGE, VAL_KIND_LABEL } from '../../validation.js';
import { REG_SOURCES, unitLabel } from '../../enums.js';

/** 归一化比较：provider 与 model_code 在清单里有大小写与空白的差异。 */
/* 验证状态的中文与色调：四个子页的徽章共用这一份。
   这一页以前自己写过一份 HEALTH，与生成中心那份并存——同一个 ok 在两屏上叫两个
   名字，就是外部评审点过的那类口径事故。 */
export const VAL_STATE = {
  ok: ['green', '通过'], missing: ['red', '缺东西'], unknown: ['amber', '未检查'],
  unconfigured: ['gray', '未配置'], unregistered: ['red', '未登记'],
  failed: ['red', '失败'], never: ['gray', '没有记录'],
};

/* 验证历史一页多少条：翻页与「共 N 条」都读它。 */
export const HISTORY_PAGE = 50;

export const norm = (s) => String(s || '').trim().toLowerCase();

export const SUBS = ['模型仓库', '引擎管理', '调用策略', '运行监控'];

/* ---------------- 跨子页的可变状态 ----------------
 * 拆分之后这几份状态被两个以上的模块读写，所以必须住在 shared：
 *   pendingFilter  在别的子页点了「已登记 N」时先把意图存一下（入口写、repo 读）
 *   viewFilter     repo 把自己的"就地筛"实现挂上来，入口那层调用
 *   resolveSeq     策略页的请求序号（只认最后一次点击）
 *   showWorkerHistory  监控页是否展开离线 worker
 * 放在这里而不是各自模块里，是因为"谁声明谁专有"在跨模块共享时不成立。
 */
export let pendingFilter = '';
export let viewFilter = null;
export let resolveSeq = 0;
export let showWorkerHistory = false;

/** 跨模块赋值要走 setter：ESM 的 import 绑定是只读的，直接赋会 TypeError。 */
export const setPendingFilter = (v) => { pendingFilter = v; };
export const setViewFilter = (v) => { viewFilter = v; };
export const bumpResolveSeq = () => ++resolveSeq;
export const getResolveSeq = () => resolveSeq;
export const toggleWorkerHistory = () => (showWorkerHistory = !showWorkerHistory);



// 一次 render 把四份数据都缓存下来；mount 里的交互不重新发请求，
// 免得点一下筛选就把整页的读请求打一遍
export let cache = { catalog: null, manifest: null, providers: null, policies: null,
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
export const SUB_NEEDS = [
  { providers: true },
  { providers: true, validations: true, engines: true },
  { providers: true, policies: true },
  { providers: true, worker: true, validations: true },
];

export function stateChip(state) {
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
export function healthChip(health) {
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
export function manifestAxis(backend) {
  return (cache.manifest && cache.manifest.axis || {})[backend] || backend;
}

export function adapterOf(manifest, cap, backend, provider) {
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
export function manifestProviders(cap, backend) {
  const be = manifestAxis(backend);
  return (cache.manifest?.adapters || [])
    .filter((a) => a.capability === cap && a.backend === be);
}

/** 模型标识的当前值：下拉与手填框两个控件只有一个可见。 */
export function pickedModel(modal) {
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
export function fillPicks(modal, cap, backend, provider, model, keepForeign) {
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
export function sourceLabels() {
  const fromApi = cache.catalog && cache.catalog.reg_sources;
  return fromApi && Object.keys(fromApi).length ? fromApi : REG_SOURCES;
}

/** 一行的运行位置说法：接口那份优先，认不出来就原样念 code（不猜近义档）。 */
export function sourceLabel(code) {
  return sourceLabels()[code] || String(code || '');
}



/** 顶部四数的可点声明：只在**真点得出东西**时挂 data-stlink。
 *  0 登记还让「已登记 0」可点，筛出来一张空表——那不是联动，是恶作剧。 */
export const stLink = (kind, enabled) => (enabled ? ' data-stlink="' + kind + '"' : '');
export const stTip = (enabled) => (enabled ? ' · 点这里只看这些行' : '');

/* ================= 顶部概览 ================= */
/**
 * 顶部四数收成一条带。
 *
 * 数字仍然全部来自 `GET /api/models/stats`（后端一处算，口径随返回值一起给），
 * 界面只念不算——模型数、可用数、引擎数一旦在两处各算一遍，迟早说出两句不一样的话。
 * 口径那一行不再摊开成一段说明，压成带子末尾的一句小字；
 * 每个数自己另带 title，鼠标停上去就是它自己的口径。
 */
export function overviewCard(s) {
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
  const queued = (s.running || 0) + (s.queued || 0);
  return `
  <div class="card mc-overview" style="margin-bottom:12px">
    <div class="mc-strip">
      <!-- 四个数全部点得下去，但**分成两类动作**，因为它们回答的不是同一件事：
          已登记／可用／要看一眼的"行"就在这张表里 → 就地筛，筛出的行数等于那个数；
          引擎是 adapter 轴、任务是队列，本表没有对应的行，硬筛会筛出一张空表骗人
          → 跳到真能看见它们的那个子页。承诺"点得下去"，不承诺"一定是筛"。 -->
      <span class="st"${stLink('registered', (s.registered_total ?? 0) > 0)}
        title="${escapeHtml(cal.registered_total || '')}${stTip((s.registered_total ?? 0) > 0)}">已登记
        <b>${s.registered_total ?? 0}</b>
        <span class="hint">${escapeHtml(capText || '还没有登记')}</span></span>
      <span class="st"${stLink('available', (s.available_total ?? 0) > 0)}
        title="${escapeHtml(cal.available_total || '')}${stTip((s.available_total ?? 0) > 0)}">可用
        <b>${s.available_total ?? 0}</b>
        ${s.needs_attention
          ? `<span class="chip amber mc-attention" data-stlink="attention" title="点这里只看这些行">${s.needs_attention} 个要看一眼</span>`
          : (s.registered_total
              ? '<span class="chip green">都齐了</span>'
              /* 空机器的 needs_attention 恒为 0（登记−可用），绿色「都齐了」
                 会把"什么都没配"念成"全部就绪"。0 登记时这不是结论，是起点。 */
              : '<span class="chip amber" title="先在模型仓库登记一档，再谈可用">还没有登记</span>')}</span>
      <span class="st" data-stlink="engines"
        title="${escapeHtml(cal.engine_total || '')} · 点这里去引擎管理">引擎
        <b>${s.engine_total ?? 0}</b>
        <span class="hint">${s.engine_params_known || 0} 个参数已核验 ·
          ${(s.unintegrated_engines || []).length} 个未接入</span></span>
      <!-- 「任务」这一格兼差原来的页头按钮「查看运行异常」：那颗按钮做的事
           其实就是 setSub(3)，等于一个跳转链接。跳转不该占页头一等位置，
           挂在这枚可点数字上，语义反而更紧（看到"跑 N"想追就点它）。 -->
      <span class="st" data-stlink="tasks" title="与生成中心同一个口径：${escapeHtml(cal.queued || '')} · 点这里去运行监控">任务
        <b>${queued}</b>
        <span class="hint">跑 ${s.running || 0} · 等 ${s.queued || 0}</span>
        ${queued ? '<button type="button" class="mc-inline-link" id="mc-goto-monitor">看异常 →</button>'
                  : '<button type="button" class="mc-inline-link" id="mc-goto-monitor">运行监控 →</button>'}</span>
      <!-- 口径说明原来是一整行 71 字的小字，横贯整张卡，和上面四个数抢注意力。
           收进一枚 ⓘ：默认不占版面，hover / 点开才读。内容一字未删。 -->
      <details class="mc-caliber">
        <summary title="这一行四个数各自怎么算">口径</summary>
        <p>可用＝已登记 ∧ 已启用 ∧ 健康就绪 ∧ 这一档自己有过真实调用或产物验收。
          引擎在线不代表模型可用，文件在也不代表依赖齐。</p>
        <p class="hint">每个数还有自己的口径：鼠标停在那一格上就能看到。</p>
      </details>
    </div>
    ${degraded}
  </div>`;
}

/* ================= 四个子页共用的弹窗 HTML ================= */
/* ================= 弹窗 ================= */
export const MODALS = `
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

/* 轻提示：三个模块都会用（入口的「检查引擎连接」、引擎实例的校验、监控页翻页）。
   为什么不用 util 里的 run()：那一个是"包住异步动作、失败弹错误"的口袋，
   这一句是"操作成功但有一句话要说"，两者语义不同，不能混。 */
export function alertNotify(text) {
  const el = document.createElement('div');
  el.className = 'chip amber';
  el.textContent = text;
  el.style.cssText = 'position:fixed;right:24px;bottom:24px;z-index:40;padding:10px 14px';
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}

/* ================= 验证记录弹窗（引擎页与模型仓库共用） =================
 * 原来这套控制器（摘要、留痕表、提交）只住在 engines.js 的 mountEngines 闭包里，
 * 模型仓库详情看得见状态却没有「验证记录」入口——要从一个模型记 real_call，
 * 得绕去引擎管理找对应能力轨。抽到 shared 后两页各调一次 setupValidationModal，
 * 弹窗 DOM（#val-modal）本就是四个子页共用的那一份。
 *
 * 一条边界贯穿：**「生产可用」绝不前端重算**。引擎页传后端算好的 slot.validation；
 * 仓库页没有那份摘要，就传后端算好的状态徽章（statusMeta）顶上，五类最新留痕从
 * 历史记录里取来只是"摆记录"，不是判定。判定规则只有一份（validations.summary_for）。
 */
const VAL_KINDS = ['env_check', 'mock', 'real_call', 'target_machine', 'artifact_review'];

/** 五类 provenance 摘要 ＋ 一句口径。metaLine 由调用方给（引擎＝生产可用徽章，
 *  仓库＝后端状态徽章），这里不自己拼判定。 */
export function summaryHtml(v, metaLine) {
  const rows = VAL_KINDS.map((k) => {
    const item = v?.[k] || {};
    const at = item.at ? String(item.at).slice(0, 16) : '—';
    const job = item.job_id ? ` · 作业 #${item.job_id}` : '';
    return `<div class="kv"><dt>${escapeHtml(VAL_KIND_LABEL[k])}</dt>
      <dd>${stateChip(item.state || 'never')}
        <span class="t-dim">${escapeHtml(at)}${escapeHtml(job)}</span>
        ${item.detail ? `<span class="t-desc">${escapeHtml(item.detail)}</span>` : ''}</dd></div>`;
  }).join('');
  return `<div class="kv-grid">${rows}</div><p class="modal-meta">${metaLine}</p>`;
}

/** 验证留痕表。引擎页弹窗与仓库页弹窗共用，不再各抄一份。 */
export function valListHtml(records) {
  if (!records.length) return '<p class="hint">这一档还没有验证记录。</p>';
  return `<div class="table-wrap"><table><thead><tr>
    <th>出处</th><th>结果</th><th>作业</th><th>说明</th><th>登记人</th><th>时间</th>
    </tr></thead><tbody>${records.map((r) => `<tr>
      <td><span class="chip plain">${escapeHtml(r.kind_label || VAL_KIND_LABEL[r.kind] || r.kind)}</span></td>
      <td>${stateChip(r.state)}</td>
      <td>${r.job_id ? `#${r.job_id}` : '—'}</td>
      <td><span class="t-desc">${escapeHtml(r.detail || '')}</span></td>
      <td>${escapeHtml(r.created_by_name || '—')}</td>
      <td class="t-dim">${escapeHtml(String(r.created_at || '').slice(0, 16))}</td>
    </tr>`).join('')}</tbody></table></div>`;
}

/** 没有后端摘要时（仓库页），从留痕里取每类最新一条摆出来。records 按新到旧，
 *  find 第一条即最新。这只是摆记录，production_ready 那种判定不从这里推。 */
function summaryFromRecords(records) {
  const v = {};
  for (const k of VAL_KINDS) {
    const rec = (records || []).find((r) => r.kind === k);
    v[k] = rec ? { state: rec.state, at: rec.created_at, detail: rec.detail, job_id: rec.job_id }
               : { state: 'never', at: '', detail: '没有记录' };
  }
  return v;
}

/** 摘要下面那句口径：引擎页用后端 production_ready，仓库页用调用方给的状态徽章。 */
function readyMeta(target) {
  if (target.statusMeta) return target.statusMeta;
  const v = target.validation || {};
  return `${v.production_ready ? '<span class="chip green">生产可用</span>'
                              : '<span class="chip gray">未达生产可用</span>'}
    ${v.target_machine_ready ? '<span class="chip green">目标机已验证</span>' : ''}
    <span class="hint">生产可用＝环境齐＋真实调用过＋产物验收通过，由服务端判定；界面不自己拼。</span>`;
}

/**
 * 绑好 #val-modal 并返回 { open(target) }。target 至少带
 * capability/backend/provider/model 与两个 label；引擎页另带 validation（后端摘要）
 * 与 id，仓库页另带 statusMeta（后端状态徽章）。hooks.afterSubmit(target) 在提交成功
 * 后调用：返回 { validation?, statusMeta?, keepOpen:true } 就刷新弹窗留着，
 * 返回 { keepOpen:false }（仓库页派 app:refresh）就关掉弹窗。
 * 三个常驻按钮用 onclick 赋值（重绑不叠加），与引擎页原来的纪律一致。
 */
export function setupValidationModal(root, hooks) {
  const modal = root.querySelector('#val-modal');
  let current = null;

  const syncPassedRow = () => {
    const isReview = root.querySelector('#val-kind').value === 'artifact_review';
    root.querySelector('#val-passed-label').hidden = !isReview;
    root.querySelector('#val-passed').hidden = !isReview;
  };
  // 按四元组筛：仓库页同一 provider 可能登记多档，必须连 model 一起对上，
  // 不然会把别的档的留痕摆到这一档名下。
  const filterRecords = (all, t) => all.filter((r) =>
    r.capability === t.capability && r.backend === t.backend
    && r.provider === t.provider && (!t.model || r.model === t.model));

  async function paintHistory(t) {
    root.querySelector('#val-list').innerHTML = '<p class="hint">正在读验证历史……</p>';
    try {
      const { items: all } = await api.providerValidations(100);
      const records = filterRecords(all, t);
      root.querySelector('#val-list').innerHTML = valListHtml(records);
      return records;
    } catch (err) {
      root.querySelector('#val-list').innerHTML =
        `<p class="t-bad">验证历史读不到：${escapeHtml(err.message)}</p>`;
      return null;
    }
  }
  function paintSummary(t, records) {
    const v = t.validation || summaryFromRecords(records);
    root.querySelector('#val-summary').innerHTML = summaryHtml(v, readyMeta(t));
  }

  async function open(t) {
    current = t;
    modal.hidden = false;
    root.querySelector('#val-title').textContent =
      `验证记录 · ${t.capability_label} · ${t.backend_label}`;
    root.querySelector('#val-job').value = '';
    root.querySelector('#val-kind').value = 'real_call';
    root.querySelector('#val-detail').value = '';
    root.querySelector('#val-paid').checked = false;
    root.querySelector('#val-error').textContent = '';
    syncPassedRow();
    // 有后端摘要（引擎页）先摆上；没有（仓库页）等留痕回来再拼。
    if (t.validation) paintSummary(t, null);
    else root.querySelector('#val-summary').innerHTML = '<p class="hint">正在读这一档的验证摘要……</p>';
    const records = await paintHistory(t);
    if (!t.validation) paintSummary(t, records);
    root.querySelector('#val-job').focus();
  }

  async function submit() {
    const errEl = root.querySelector('#val-error');
    errEl.textContent = '';
    if (!current) return;
    const jobId = Number(root.querySelector('#val-job').value);
    if (!jobId) { errEl.textContent = '填一条已成功的作业号。'; return; }
    const kind = root.querySelector('#val-kind').value;
    const body = {
      capability: current.capability, backend: current.backend,
      provider: current.provider, model: current.model,
      kind, job_id: jobId,
      paid_confirmed: root.querySelector('#val-paid').checked,
      detail: root.querySelector('#val-detail').value.trim(),
    };
    if (kind === 'artifact_review') body.passed = root.querySelector('#val-passed').value === 'pass';
    const btn = root.querySelector('#val-submit');
    btn.disabled = true;
    try {
      await api.validateJob(jobId, body);
      const res = await hooks.afterSubmit(current);
      if (res && res.keepOpen) {
        current = { ...current, validation: res.validation, statusMeta: res.statusMeta };
        const records = await paintHistory(current);
        paintSummary(current, records);
        root.querySelector('#val-job').value = '';
        root.querySelector('#val-detail').value = '';
      } else {
        modal.hidden = true;
      }
    } catch (err) {
      errEl.textContent = err.message || '登记失败';
    } finally {
      btn.disabled = false;
    }
  }

  root.querySelector('#val-close').onclick = () => { modal.hidden = true; };
  root.querySelector('#val-submit').onclick = submit;
  root.querySelector('#val-kind').onchange = syncPassedRow;
  return { open };
}
