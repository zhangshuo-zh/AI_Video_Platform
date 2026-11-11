import { store } from '../store.js';
import { api } from '../api.js';
import { bindSubChips, confirmDanger, escapeHtml, formatMoney, run, safeOpenUrl, subChipRow } from '../util.js';
import { JOB_ACTIVE, jobTone, publicStatus, validationText } from '../validation.js';
import { ASSET_KIND_LABELS, unitLabel } from '../enums.js';
import { WS_LABELS, setWsIndex, takeStudioTarget, workerChip, wsIndex } from './asset-workspaces.js';

/**
 * 资产创作工作区：文字设定 → 图像候选 → 存进资产。
 *
 * 三类资产共用这一条流程，差别只有"文字设定要填哪几格"和"形态叫什么名"。
 * 分成三个页面就会把轨道选择、云端确认、轮询、导入这四件事写三遍——
 * 而它们恰恰是最容易走偏的部分。
 *
 * 五条纪律：
 * 1. **模型与计价只从 `providers/available` 来**。页面不写死供应商、模型名、
 *    参数范围或单价；那一档没登记价格，云端就明说"没登记计价，不能提交"，
 *    不让人勾了确认之后才发现钱花不出去也结不了账。
 * 2. **生成结果先进草稿，不直接写库**。回稿填进表单之后必须人点「保存」才落库——
 *    模型说什么就改什么，等于让模型成为资产的第二个写入方。
 * 3. **状态以服务端为准，页面不演进度**。排队／运行／成功／失败／待核对都读作业；
 *    轮询绑在本视图的节点上，切走就停，不绑在常驻的 `#view-root` 上。
 * 4. **健康与验证是两件事，都用原词显示**。`health_state` 回答"环境齐不齐"，
 *    `validation_state` 回答"凭什么认为真能产出"——只报前者会把"从没跑过"
 *    的槽位摆成"可以用"，那是本轮任务书 §5.3 明令禁止的那种冒充。
 * 5. **轮询只改该改的那几格**（v0.99）。整页重绘会把人正在敲的那一格连光标一起
 *    换掉：上一轮靠"值不丢"挡住了数据事故，但打断输入本身就算断点。
 *    值不丢是底线，不打断是这一条。
 */

// 资产类型的中文名统一从 `enums.js` 取（2026-11-10 裁决：统一为「角色」）。
// 原来这张本地表写「人物」，而 `assets.js`／`writing.js`／`asset-detail.js`
// 写「角色」、服务端 `ASSET_KINDS` 也是「角色」——同一个 character 在两屏两个名字。
const KIND_LABEL = ASSET_KIND_LABELS;
const OPERATION = {
  character: 'character_profile', scene: 'scene_setting', prop: 'prop_setting',
};
const VARIANT_TYPE = { character: 'outfit', scene: 'scene_view', prop: 'prop_form' };
const VARIANT_LABEL = { outfit: '服饰', scene_view: '视角', prop_form: '形态', other: '备选方案' };
// 一个类型可选的形态种类：主类型＋通用备选。与服务层 VARIANT_TYPES_BY_KIND 同一套，
// 这里只是把不该出现的选项藏起来，真正的门在服务端（选错回 422 并列出可选项）。
const VARIANT_CHOICES = {
  character: ['outfit', 'other'], scene: ['scene_view', 'other'], prop: ['prop_form', 'other'],
};

// 文字设定要填的格子。scene/prop 的"说明"就是 asset.summary——
// 不再另建一列，那等于给同一个字段两个家。
const TEXT_FIELDS = {
  character: [
    ['biography', '履历', 3, '成长背景、教育或职业经历、关键人生事件'],
    ['personality', '性格', 3, '性格核心、优缺点、行为习惯、动机与价值观'],
    ['growth_arc', '成长过程', 3, '起点、关键转折、主要冲突、阶段变化与终点'],
  ],
  scene: [['summary', '场景说明', 3, '空间结构、建筑与地形、时间与天气、光线与材质']],
  prop: [['summary', '道具说明', 3, '轮廓、比例、材质、颜色、磨损与关键识别特征']],
};
// "文字设定提示词"这一格只有人物有；场景与道具的说明本身就当提示词素材用
const HAS_TEXT_PROMPT = { character: true, scene: false, prop: false };

// 任务模式与 `asset_studio.TEXT_MODES` 一一对应。**语义在服务端**：
// 这里只负责显示选项与把选中的 code 交出去，"这一次要模型写哪几段"由
// `sections_for` 按库里的实际内容算——页面自己算就会和服务端算出两个结果。
const TEXT_MODES = [
  ['full', '生成完整设定', '把这几段一次写齐'],
  ['fill_missing', '只补空着的字段', '已有内容一个字不动'],
  ['polish', '优化已有设定', '只收紧文字，不新增设定'],
  ['expand', '扩写指定字段', '只动你挑的那一段'],
  ['from_source', '依据原文片段写设定', '只照给的原文写，不编'],
];
// 「扩写哪一段」的可选项，取自各操作的 OUTPUT_SPEC 段落（与服务端同序）。
const EXPAND_FIELDS = {
  character: ['biography', 'personality', 'growth_arc', 'consistency_prompt'],
  scene: ['summary', 'consistency_prompt'],
  prop: ['summary', 'consistency_prompt'],
};
const FIELD_LABEL = {
  biography: '履历', personality: '性格', growth_arc: '成长过程',
  summary: '说明', consistency_prompt: '一致性提示词', text_prompt: '文字设定提示词',
};

/* 模块级草稿：切页回来还在同一个目标上，也保住没保存的文字。
   换项目就作废——旧项目里的资产 id 到新项目里是另一条东西。 */
let draft = null;
let draftProjectId = null;
let reqSeq = 0;          // 渲染与轮询的序号：迟到的响应不许盖新页面
let pollTimer = null;
// 回前台补一拍用的闭包（与 generate.js 同一套纪律）。挂 document 的监听
// 不随视图回收——#view-root 换页时它仍在 body 里，isConnected 挡不住，
// 所以必须记住这一次挂的那个函数，下次 startPoll 先解掉上一份。
let visHandler = null;
let lastOptions = [];    // 本次渲染拿到的槽位清单，提交时用它查单价

function newDraft(kind = 'character') {
  return {
    targetId: 0, kind, loadedAsset: 0,
    fields: Object.fromEntries(TEXT_FIELDS[kind].map(([k]) => [k, ''])),
    textPrompt: '', consistency: '', assetName: '',
    textTrack: 'local', textProvider: '', textModel: '', textInstruction: '',
    textMode: 'full', textTargetField: '', sourceExcerpt: '', textParams: {},
    imageTrack: 'local', imageProvider: '', imageModel: '',
    imagePrompt: '', imageVariantId: 0, imageParams: {},
    cloudConfirm: false,
    textJob: null, imageJob: null,
    preview: null, candidates: null, jobMeta: null, variants: [],
    sections: [], reused: false, editing: null,
    notice: '', conflict: '', error: '', dirty: false,
    // 两把弱版本令牌：一把对资产、一把对人物档案（服务端各算各的，
    // 合成一把的话"先存资产再存档案"会自己撞自己）
    baseVersion: '', baseProfileVersion: '', busyText: false, busyImage: false,
  };
}

function ensureDraft() {
  if (draftProjectId !== store.currentId || !draft) {
    draft = newDraft();
    draftProjectId = store.currentId;
  }
  return draft;
}

/** 换类型：保留轨道选择与草稿文字，清掉不属于这个类型的字段。 */
function reKind(kind) {
  const old = draft || newDraft(kind);
  const next = newDraft(kind);
  next.textTrack = old.textTrack;
  next.imageTrack = old.imageTrack;
  if (old.targetId === 0) {
    next.assetName = old.assetName;
    next.consistency = old.consistency;
    next.imagePrompt = old.imagePrompt;
    next.textInstruction = old.textInstruction;
  }
  draft = next;
}

/* ---------- 槽位、能力与价格 ---------- */

function optionsFor(capability, backend) {
  return lastOptions
    .filter((o) => o.capability === capability && o.backend === backend)
    .map((o) => ({
      provider: o.provider, model: o.model, adapterLabel: o.adapter_label,
      label: `${o.adapter_label} · ${o.model}`,
      state: o.health_state, detail: o.health_detail, models: o.models || [],
      validation: o.validation_state || 'never', validationAt: o.validation_at || '',
    }));
}

function chosenOption(which, d) {
  const capability = which === 'text' ? 'text' : 'image';
  return optionsFor(capability, d[`${which}Track`])
    .find((o) => o.provider === d[`${which}Provider`] && o.model === d[`${which}Model`]) || null;
}

/** 单价从清单取。没登记价格的档位返回 null，由界面与提交两头都说清楚。 */
function priceOf(option, modelCode) {
  const model = (option && option.models || []).find((m) => m.code === modelCode);
  return (model && model.prices && model.prices[0]) || null;
}

function paramsOf(option, modelCode) {
  const model = (option && option.models || []).find((m) => m.code === modelCode);
  return (model && model.params) || [];
}

/**
 * 这一档**能不能吃参考图**：只看清单登记过的输入参数（`image_inputs`，
 * 由 adapters 里 `type=="image"` 的参数数出来）。
 * 不按模型名字猜——"I2V"三个字不足以证明 mflux 那一档也能收参考图，它不能。
 * 认不出来就报"清单没登记"，而不是默认"应该支持"。
 */
function refInputsOf(option, modelCode) {
  const model = (option && option.models || []).find((m) => m.code === modelCode);
  return (model && model.image_inputs) || [];
}

function fmtMoney(micros, currency) {
  // 原来这一格自己写死 `¥${…toFixed(4)}`：清单里换成美元计价时，界面照样发一个 ¥，
  // 而那笔钱到底按什么币种扣是供应商那边说了算——对账的数不能靠界面猜币种。
  // util.formatMoney 才是唯一出口：币种从数据里来，小数位按金额大小自适应。
  return formatMoney(micros, currency);
}

/** 云端确认单：数量与单价都取清单默认那一档，页面不自己算价。 */
function estimateFor(which, d) {
  if (d[`${which}Track`] !== 'cloud') return null;
  const option = chosenOption(which, d);
  const price = priceOf(option, d[`${which}Model`]);
  if (!price) return null;
  return { quantity: price.default_quantity || 1, unit: price.unit,
           unit_price_micros: price.amount_micros, currency: price.currency,
           source: price.source };
}

/** 只带清单登记过的参数；没填的与空字符串都不发，让服务端按默认走。 */
function paramsFor(which, d) {
  const out = {};
  Object.entries(d[`${which}Params`] || {}).forEach(([k, v]) => {
    if (v === '' || v === null || v === undefined) return;
    out[k] = v;
  });
  return out;
}

// 在飞的判定以前在这里另列了一份：['pending','queued','starting','running']。
// 两个问题：`pending` 不是 job.status 的取值（0015 的 CHECK 只有八个值，
// 没有它），多写一条没人发现；而 **cancel_requested 漏了**——一条"取消中"的作业
// 还在进程里跑，这一页却当它已经结束：轮询停掉、按钮放开，人接着提第二条，
// 服务端回一句"这一槽位还有作业在跑"，看起来像是平台在刁难人。
// 收敛到 validation.js 的 JOB_ACTIVE（＝后端 queued + JOB_RUNNING 的口径）。
const inflight = (job) => Boolean(job && JOB_ACTIVE.includes(job.status));

/**
 * 这一轨有没有选到**具体某一档**。没选到就不能提交。
 *
 * 2026-11-04 真浏览器实测踩到的：页面以前无条件把 `execution_mode` 发出去，
 * 供应商与模型却是空的，服务端按"三件要么都不传、要么一起给"回一句 422——
 * 那句话是内部载荷规则，不是人能照着做的下一步；而轨道面板明明写着
 * "配好之前提交钮是灰的"。现在这颗钮真的会灰，理由由面板那句"还没选槽位"给。
 */
const slotMissing = (d, which) => !d[`${which}Provider`] || !d[`${which}Model`];

/* ---------- 片段 ---------- */

function jobChip(label, job) {
  if (!job) return '';
  // 配色走 validation.js 的唯一那份（原来这里第五份，还多认了一个
  // 不存在的 pending、少认了 cancel_requested）
  // 对外只显示五档（§12.3）；细粒度原词留在 title 里，不用换页也能看见
  return `<span class="chip ${jobTone(job.status)}" title="作业 #${job.job_id}｜${escapeHtml(
    job.status_label || job.status)}｜状态与成本在生成中心">
    ${escapeHtml(label)}·${escapeHtml(publicStatus(job.status))}</span>`;
}

/** 顶部那条"这一页现在能不能干活"。数据全是接口回的，一个数字都不写死。 */
function statusBar() {
  // worker 那一格用 asset-workspaces.js 的公共写法：资产库与这一页必须说同一句话
  const bits = [workerChip()];
  const text = optionsFor('text', 'local').length + optionsFor('text', 'cloud').length;
  const image = optionsFor('image', 'local').length + optionsFor('image', 'cloud').length;
  bits.push(`<span class="t-dim">可用槽位：文本 ${text} · 图像 ${image}`
    + `${text && image ? '' : '（缺哪一类就配哪一轨，页面不替你猜）'}</span>`);
  return `<div class="as-status">${bits.join(' ')}</div>`;
}

function trackPanel(d, which) {
  const capability = which === 'text' ? 'text' : 'image';
  const list = optionsFor(capability, d[`${which}Track`]);
  const chosen = chosenOption(which, d);
  const price = chosen ? priceOf(chosen, d[`${which}Model`]) : null;
  const title = capability === 'text' ? '文本模型' : '图像模型';
  const opts = list.map((o) => {
    const mark = o.state === 'ok' ? '' : `（${o.state === 'unconfigured' ? '未配置'
      : o.state === 'unregistered' ? '未登记适配器' : o.state === 'missing' ? '缺条件' : o.state}）`;
    // 健康 ok 但只是"环境齐"，验证徽章必须同屏显示：只看前者会把
    // 从没跑过的槽位摆成"可以用"，那正是任务书 §5.3 禁止的冒充。
    const val = `〔${validationText(o.validation)}〕`;
    return `<option value="${escapeHtml(o.provider)}|${escapeHtml(o.model)}"
      ${o.state === 'ok' ? '' : 'disabled'}
      ${o.provider === d[`${which}Provider`] && o.model === d[`${which}Model`] ? 'selected' : ''}
      >${escapeHtml(o.label)}${escapeHtml(val)}${mark}</option>`;
  }).join('');
  const hint = list.length === 0
    ? `<p class="t-desc">${d[`${which}Track`] === 'cloud' ? '云端' : '本地'}这一轨没有${
        title}槽位。到模型中心 · 引擎管理里配一条；配好之前提交钮是灰的，
        平台不会替你换一条轨发出去。</p>`
    : `<p class="t-desc">${d[`${which}Track`] === 'cloud'
        ? '云端：提交前展示清单登记的单价，并要单独勾一次计费确认。'
        : '本地：不计云端费用；运行时缺条件时这条作业会被明确拒绝，不会自己转云端。'}</p>`;
  // 选中那档的两句话：验证到什么程度、健康为什么不行。
  // 只讲事实与下一步，不解释状态码。
  const why = chosen
    ? `<p class="t-dim">这一档：健康 ${chosen.state}｜验证 ${validationText(chosen.validation)}`
      + `${chosen.validationAt ? `（${escapeHtml(chosen.validationAt)}）` : ''}`
      + `${chosen.validation === 'never' || chosen.validation === 'env_check'
        ? '——还没有真实产出过东西，第一次跑建议先拿最短的一条试试。' : ''}</p>`
    : (list.length ? '<p class="t-dim">还没选槽位；上面灰掉的那些不可选，原因写在括号里。</p>' : '');
  const cost = d[`${which}Track`] === 'cloud'
    ? (price
      ? `<p class="t-dim">单价 ${fmtMoney(price.amount_micros, price.currency)}`
        + ` / ${escapeHtml(unitLabel(price.unit))}`
        + `｜预计 ${price.default_quantity ?? '—'} ${escapeHtml(unitLabel(price.unit))}`
        + ` ≈ ${fmtMoney((price.default_quantity || 0) * price.amount_micros, price.currency)}`
        + `｜出处：${escapeHtml(price.source)}</p>`
      : `<p class="t-desc">这一档没登记计价，不能提交云端计费作业。</p>`)
    : '';
  const confirm = d[`${which}Track`] === 'cloud'
    ? `<label class="chk"><input type="checkbox" data-cloud-confirm="${which}"
        ${d.cloudConfirm ? 'checked' : ''}>
      <span>我已知晓云端模型调用可能产生费用，并确认本次提交</span></label>`
    : '';
  return `
    <div class="as-track">
      <div class="as-actions" style="gap:6px">
        <button type="button" class="chip ${d[`${which}Track`] === 'local' ? 'blue' : 'plain'}"
                data-track="${which}|local">本地</button>
        <button type="button" class="chip ${d[`${which}Track`] === 'cloud' ? 'blue' : 'plain'}"
                data-track="${which}|cloud">云端</button>
        <span class="t-dim">${title}轨道</span>
      </div>
      <select class="ctl" data-slot="${which}" ${list.length ? '' : 'disabled'}>
        <option value="">${list.length ? '选择已配置的槽位' : '这一轨没有槽位'}</option>${opts}
      </select>
      ${hint}${why}${cost}${confirm}
    </div>`;
}

function previewPanel(d) {
  const p = d.preview || {};
  const rows = Object.entries(p.fields || {}).map(([k, v]) => `
    <label class="top">${escapeHtml(FIELD_LABEL[k] || k)}</label>
    <textarea class="ctl" rows="2" readonly>${escapeHtml(v)}</textarea>`).join('');
  return `
    <div class="card inner as-preview">
      <div class="card-head"><h3>模型给的回稿</h3>
        <span class="sub">${p.can_fill ? '填进表单后自己核对，保存才落库' : '没有可填的段落'}</span></div>
      <div class="card-body">
        ${p.text_mode_label
          ? `<p class="t-dim">这一条是按「${escapeHtml(p.text_mode_label)}」跑的。</p>` : ''}
        ${rows ? `<div class="form-grid">${rows}</div>` : ''}
        ${!Object.keys(p.fields || {}).length && p.raw
          ? `<p class="t-desc">原文：${escapeHtml(p.raw.slice(0, 400))}</p>` : ''}
        <p class="note">${escapeHtml(p.note || '')}</p>
        <div class="as-actions">
          <button class="btn" data-fill-preview ${p.can_fill ? '' : 'disabled'}>填进表单</button>
          <button class="btn ghost" data-drop-preview>丢掉这份回稿</button>
        </div>
      </div>
    </div>`;
}

/** 这一段只有文字面板用：模式选择＋模式要吃的那份额外输入。 */
function modePanel(d) {
  const mode = d.textMode;
  const options = TEXT_MODES.map(([code, label]) => `<option value="${code}"
    ${code === mode ? 'selected' : ''}>${escapeHtml(label)}</option>`).join('');
  const hint = (TEXT_MODES.find(([code]) => code === mode) || [])[2] || '';
  const expand = mode === 'expand' ? `
    <label for="as-target-field">扩写哪一段</label>
    <select id="as-target-field" class="ctl" data-k="textTargetField">
      ${EXPAND_FIELDS[d.kind].map((f) => `<option value="${f}"
        ${f === d.textTargetField ? 'selected' : ''}>${escapeHtml(FIELD_LABEL[f])}</option>`).join('')}
    </select>` : '';
  const excerpt = mode === 'from_source' ? `
    <label class="top" for="as-excerpt">原文片段</label>
    <textarea id="as-excerpt" class="ctl" rows="4" data-k="sourceExcerpt"
      placeholder="贴一段正文。模型只照这段写；它没有的会被标成「建议」，不会变成设定事实"
      >${escapeHtml(d.sourceExcerpt)}</textarea>
    <span class="pf-label"></span>
    <p class="pf-hint">这是"给这一条资产补设定"，不是整篇抽取：整篇抽取归创作中心，
      那边有逐字证据与候选分诊（本轮边界）。</p>` : '';
  const sections = d.sections.length ? `
    <span class="pf-label"></span>
    <p class="t-dim">上一次提交实际请求了这几段：${escapeHtml(d.sections.join('／'))}
      ${d.reused ? '（命中已有作业，没有重复排队）' : ''}</p>` : '';
  return `
    <div class="form-grid" style="margin-top:8px">
      <label for="as-mode">这次做什么</label>
      <select id="as-mode" class="ctl" data-mode>${options}</select>
      <span class="pf-label"></span>
      <p class="pf-hint">${escapeHtml(hint)}。空着的与已有的怎么算，由服务端按库里的内容判，
        页面只负责把你选的那一句交出去。</p>
      ${expand}${excerpt}${sections}
    </div>`;
}

function textPanel(d) {
  const fields = TEXT_FIELDS[d.kind].map(([key, label, rows, ph]) => `
    <label class="top" for="as-f-${key}">${label}</label>
    <textarea id="as-f-${key}" class="ctl" rows="${rows}" data-field="${key}"
              placeholder="${escapeHtml(ph)}">${escapeHtml(d.fields[key] || '')}</textarea>`).join('');
  const promptField = HAS_TEXT_PROMPT[d.kind] ? `
    <label class="top" for="as-text-prompt">文字设定提示词</label>
    <textarea id="as-text-prompt" class="ctl" rows="2" data-k="textPrompt"
              placeholder="让模型怎么写这份设定；它与下面的视觉一致性提示词是两回事"
      >${escapeHtml(d.textPrompt)}</textarea>` : '';
  const busy = inflight(d.textJob) || d.busyText;
  const blocked = busy || slotMissing(d, 'text');
  return `
    <div class="card">
      <div class="card-head"><h2>文字设定</h2>
        <span class="sub">先生成，改完再保存；模型不会自己写库</span>
        <span id="as-jobchip-text">${jobChip('文字', d.textJob)}</span></div>
      <div class="card-body">
        <div class="form-grid">
          ${fields}${promptField}
          <label class="top" for="as-consistency">一致性提示词</label>
          <textarea id="as-consistency" class="ctl" rows="3" data-k="consistency"
                    placeholder="跨镜头必须长一样的细节">${escapeHtml(d.consistency)}</textarea>
          <p class="pf-hint">这一条是喂给图像模型的稳定描述。生成文字设定时会带上它，
            但两份内容各存各的——改履历不该顺手改掉定妆图的依据。</p>
          <label for="as-instruction">本次要求</label>
          <input id="as-instruction" class="ctl" data-k="textInstruction"
                 placeholder="例如：写得克制一点，别加超能力" value="${escapeHtml(d.textInstruction)}">
        </div>
        ${modePanel(d)}
        ${trackPanel(d, 'text')}
        ${paramRows(d, 'text')}
        <div id="as-preview-slot">${d.preview ? previewPanel(d) : ''}</div>
        <div class="as-actions" style="margin-top:10px">
          <button class="btn primary" id="as-gen-text" data-gen-text ${blocked ? 'disabled' : ''}>
            ${busy ? '排队中…' : '生成文字设定'}</button>
          <button class="btn" id="as-again-text" data-gen-text
                  ${blocked ? 'disabled' : ''}
                  title="同样输入再跑一批：带一个新的幂等键，所以是真的第二条作业">换一批</button>
          <button class="btn" data-save-text>保存这份设定</button>
          <button class="btn ghost" data-clear-draft>清空草稿</button>
        </div>
      </div>
    </div>`;
}

/** 参数只从清单来：这一档登记了什么就摆什么，页面不自己造旋钮。
    文字与图像两条轨共用这一段——文本档同样有登记参数（温度、上限…），
    只给图像做等于把清单的一半丢掉。
    本轮按 §5.2 收进「高级参数」：默认折叠，选中档位的关键价与验证状态留在外面。 */
function paramRows(d, which) {
  const option = chosenOption(which, d);
  if (!option) return '';
  const params = paramsOf(option, d[`${which}Model`]);
  if (!params.length) {
    return '<p class="t-desc">这一档没有登记可调参数；提交时只带提示词。</p>';
  }
  const store_ = d[`${which}Params`] || {};
  const cells = params.map((p) => {
    const val = store_[p.name] ?? p.default ?? '';
    const id = `as-p-${which}-${p.name}`;
    const key = `${which}|${p.name}`;
    if (p.choices && p.choices.length) {
      return `<label for="${id}">${escapeHtml(p.name)}</label>
        <select id="${id}" class="ctl" data-param="${escapeHtml(key)}">
          ${p.choices.map((c) => `<option value="${escapeHtml(String(c))}"
            ${String(c) === String(val) ? 'selected' : ''}>${escapeHtml(String(c))}</option>`).join('')}
        </select>`;
    }
    if (p.type === 'bool') {
      return `<span class="pf-label">${escapeHtml(p.name)}</span>
        <label class="chk"><input type="checkbox" data-param="${escapeHtml(key)}"
          ${val ? 'checked' : ''}><span>${escapeHtml(p.note || '')}</span></label>`;
    }
    const range = p.minimum != null || p.maximum != null
      ? `（${p.minimum ?? '不限'}—${p.maximum ?? '不限'}）` : '';
    if (p.type === 'str') {
      // 字符串参数给 textarea：单行 input 装不下换行，而"写进结果文件的文本"
      // 这类参数靠的正是一行一个标记的换行。数字与布尔仍按原样给控件。
      return `<label for="${id}">${escapeHtml(p.name)}${range}</label>
        <textarea id="${id}" class="ctl" rows="3" data-param="${escapeHtml(key)}"
                  placeholder="${escapeHtml(p.note || '')}">${escapeHtml(String(val))}</textarea>`;
    }
    return `<label for="${id}">${escapeHtml(p.name)}${range}</label>
      <input id="${id}" class="ctl"
             type="${p.type === 'int' || p.type === 'float' ? 'number' : 'text'}"
             data-param="${escapeHtml(key)}" value="${escapeHtml(String(val))}"
             placeholder="${escapeHtml(p.note || '')}">`;
  }).join('');
  return `<details class="as-advanced">
    <summary>高级参数（这一档登记的 ${params.length} 项）</summary>
    <div class="form-grid as-params">${cells}</div>
  </details>`;
}

/**
 * 候选网格。抽出来是因为轮询只该重画这一格：
 * 整页重绘会把人正在敲的提示词框连光标一起换掉（纪律 5）。
 * 绑事件也一起抽到 `bindCandidates`，两处共用同一份，不会出现"渲染改了、绑定没改"。
 */
function candidatesHtml(d) {
  const cand = d.candidates;
  if (!cand) {
    return '<p class="t-desc">还没有候选。选好轨道、写提示词，点「生成图像候选」。</p>';
  }
  const items = cand.items || [];
  if (!items.length) {
    return '<p class="t-desc">这条作业成功了，但产物清单是空的。</p>';
  }
  const meta = d.jobMeta || {};
  const head = `<p class="t-dim">作业 #${cand.job_id}｜${escapeHtml(
    `${meta.execution_mode_label || ''} ${meta.provider_code || ''}/${meta.model_code || ''}`.trim())
    || '档位未知'}｜完成 ${escapeHtml(meta.finished_at || '—')}`
    + `${meta.cost_micros ? `｜成本 ${fmtMoney(meta.cost_micros, meta.currency)}` : ''}`
    + `｜<a href="#generate/0" class="plain">到生成中心看这条作业</a></p>`;
  const canSave = d.targetId > 0;
  const grid = items.map((a, i) => `
      <figure class="as-cand">
        <img src="${escapeHtml(`/api/jobs/${cand.job_id}/artifacts/${i}`)}" alt="候选 ${i + 1}"
             data-preview-img="${escapeHtml(`/api/jobs/${cand.job_id}/artifacts/${i}`)}"
             ${a.exists ? '' : 'style="opacity:.35"'}>
        <figcaption>
          <span class="t-dim">#${i + 1}${a.exists ? ` · ${Math.round((a.size || 0) / 1024)}KB`
            : ' · 文件不在'}</span>
          ${a.exists && canSave ? `
            <select class="ctl xs" data-save-target="${i}">
              <option value="">存进哪儿…</option>
              <option value="base">人物／场景基础参考图（追加为最后一张）</option>
              ${d.variants.map((v) => `<option value="${v.id}">形态：${escapeHtml(v.name)}</option>`).join('')}
            </select>
            <button class="btn xs" data-confirm-img="${i}">存入</button>` : ''}
        </figcaption>
      </figure>`).join('');
  const note = canSave ? ''
    : '<p class="t-desc">还没保存成资产，所以先存不了：产物不会自动写进任何资产。</p>';
  return `${head}${note}
    <div class="as-cands">${grid}</div>
    <p class="t-dim">存进去是「追加」：已有参考图一张都不会被顶掉，封面仍是原来那张
      （要换封面去详情页点那张图的「设为封面」）。这一屏收起只是不显示，
      产物本身还在生成中心，随时能再来存。</p>`;
}

function imagePanel(d) {
  const option = chosenOption('image', d);
  const refs = option ? refInputsOf(option, d.imageModel) : [];
  const refNote = !option ? ''
    : (refs.length
      ? `<p class="t-desc">这一档登记了参考图输入参数（${escapeHtml(refs.join('／'))}）。`
        + '把基础参考图当作条件图喂进去要在那条作业里显式给路径，本页只负责把提示词与归属记准。</p>'
      : '<p class="t-desc">这一档的清单里没有参考图输入参数，所以人物基础图不会被当作条件图喂进去；'
        + '一致性只能靠下面这段文字。平台不会把"选了一张图"说成"已经应用了参考图"。</p>');
  const busy = inflight(d.imageJob) || d.busyImage;
  const variantOpts = d.variants.length ? `
    <div class="form-grid" style="margin-top:8px">
      <label for="as-img-variant">为哪个形态生成</label>
      <select id="as-img-variant" class="ctl" data-img-variant>
        <option value="0"${d.imageVariantId === 0 ? ' selected' : ''}>人物／场景本体（不是形态）</option>
        ${d.variants.map((v) => `<option value="${v.id}"
          ${v.id === d.imageVariantId ? 'selected' : ''}>${escapeHtml(v.name)}（${escapeHtml(v.variant_type_label)}）</option>`).join('')}
      </select>
      ${d.imageVariantId ? `
        <span class="pf-label"></span>
        <p class="pf-hint">作业会记住"这是给哪个形态跑的"，产物也才存得进那个形态。
          形态的图永远不进基础参考图——两边混在一组，下游图生视频会把互相矛盾的服装
          当成同一个人的同一套衣服。</p>` : ''}
    </div>` : '';
  return `
    <div class="card">
      <div class="card-head"><h2>图像候选</h2>
        <span class="sub">人物／场景／道具都走这一条；每张都要预览后再存</span>
        <span id="as-jobchip-image">${jobChip('图像', d.imageJob)}</span></div>
      <div class="card-body">
        <div class="form-grid">
          <label class="top" for="as-img-prompt">图像提示词</label>
          <textarea id="as-img-prompt" class="ctl" rows="4" data-k="imagePrompt"
                    placeholder="默认继承一致性提示词，可以单独改">${escapeHtml(d.imagePrompt)}</textarea>
        </div>
        <div class="as-actions" style="margin-top:6px">
          <button class="btn xs" data-sync-consistency>从一致性提示词同步</button>
          <button class="btn xs" data-compose-variant ${d.imageVariantId ? '' : 'disabled'}
                  title="身份锚点＋一致性＋这个形态的描述，拼成一段可以改的文字">按形态拼好</button>
          <span class="t-dim">参数按所选档位登记的给，页面不自己造</span>
        </div>
        ${refNote}
        ${trackPanel(d, 'image')}
        ${paramRows(d, 'image')}
        ${variantOpts}
        <div class="as-actions" style="margin-top:10px">
          <button class="btn primary" id="as-gen-image" data-gen-image
                  ${busy || slotMissing(d, 'image') ? 'disabled' : ''}>
            ${busy ? '排队中…' : '生成图像候选'}</button>
          <button class="btn" id="as-again-image" data-gen-image
                  ${busy || slotMissing(d, 'image') ? 'disabled' : ''}
                  title="同样输入再跑一批：带一个新的幂等键">换一批</button>
          <button class="btn ghost" data-drop-cands>收起这一屏候选</button>
        </div>
        <div id="as-cands">${candidatesHtml(d)}</div>
      </div>
    </div>`;
}

function variantPanel(d) {
  const rows = d.variants.map((v, idx) => {
    const editing = d.editing === v.id;
    return `
    <div class="as-variant" data-variant="${v.id}">
      <div class="as-variant-head">
        <b>${escapeHtml(v.name)}</b>
        <span class="chip plain">${escapeHtml(v.variant_type_label)}</span>
        <span class="t-dim">${v.image_count} 图${v.source_job_id ? ` · 来源作业 #${v.source_job_id}` : ''}</span>
        <button class="btn xs" data-edit-variant="${v.id}">${editing ? '收起' : '改名与提示词'}</button>
        <button class="btn xs ghost" data-move-variant="${v.id}" data-dir="-1"
                ${idx === 0 ? 'disabled' : ''} title="往前排">↑</button>
        <button class="btn xs ghost" data-move-variant="${v.id}" data-dir="1"
                ${idx === d.variants.length - 1 ? 'disabled' : ''} title="往后排">↓</button>
        <button class="btn xs danger" data-del-variant="${v.id}">删除</button>
      </div>
      ${editing ? `
        <div class="form-grid" style="margin-top:8px">
          <label for="as-vn-${v.id}">名称</label>
          <input id="as-vn-${v.id}" class="ctl" data-v="name" value="${escapeHtml(v.name)}">
          <label class="top" for="as-vp-${v.id}">这个形态的图像提示词</label>
          <textarea id="as-vp-${v.id}" class="ctl" rows="2" data-v="prompt"
                    placeholder="这套衣服／视角特有的细节">${escapeHtml(v.prompt)}</textarea>
          <span class="pf-label"></span>
          <div class="as-actions">
            <button class="btn sm primary" data-save-variant="${v.id}">保存形态</button>
          </div>
        </div>` : ''}
      ${v.ref_images.length ? `<div class="ad-imgs">${v.ref_images.map((p) => `
        <figure class="ad-img"><img src="${escapeHtml(`/files/${p}`)}" alt="${escapeHtml(v.name)} 的图"
          data-preview-img="${escapeHtml(`/files/${p}`)}">
          <figcaption><button class="btn xs danger" data-vid="${escapeHtml(v.id)}" data-path="${escapeHtml(p)}">删这张</button></figcaption>
        </figure>`).join('')}</div>`
        : '<p class="t-desc">这个形态还没有图。生成候选后在图下面选「形态：这个名字」再存入。</p>'}
    </div>`;
  }).join('');
  return `
    <div class="card">
      <div class="card-head"><h2>${escapeHtml(VARIANT_LABEL[VARIANT_TYPE[d.kind]] || '形态')}列表</h2>
        <span class="sub">服饰／视角／形态的图跟着形态走，不混进基础参考图</span></div>
      <div class="card-body">
        <p class="note" title="日常服、正式服、战斗服、年轻时期、受伤状态都算同一个人，不该各建一条人物资产。但图必须分开存：把互相矛盾的服装塞进同一组参考图，下游图生视频会把它们当成同一套衣服去生。">
          同一个人的不同形态（服装／时期／状态）不算新资产，但<b>图要分开存</b>——
          混在一组参考图里，下游会当成同一套衣服去生。</p>
        ${d.targetId > 0 ? `
          <div class="as-actions">
            <input id="as-new-variant" class="ctl" placeholder="新形态名称，例如 雨夜外套"
                   style="max-width:220px">
            <select id="as-new-variant-type" class="ctl" style="max-width:150px">
              ${VARIANT_CHOICES[d.kind].map((t) => `<option value="${t}"
                ${t === VARIANT_TYPE[d.kind] ? 'selected' : ''}>${escapeHtml(VARIANT_LABEL[t])}</option>`).join('')}
            </select>
            <button class="btn" data-add-variant>新建形态</button>
          </div>` : '<p class="t-desc">先保存成资产，才能给它建形态。</p>'}
        ${rows || '<p class="t-dim">还没有形态。</p>'}
      </div>
    </div>`;
}

function targetPanel(d, list) {
  const opts = list.map((a) => `<option value="${a.id}"
    ${a.id === d.targetId ? 'selected' : ''}>${escapeHtml(a.name)}（${escapeHtml(a.kind_label)}）</option>`).join('');
  return `
    <div class="card">
      <div class="card-head"><h2>这次做谁</h2>
        <span class="sub">选一条已有资产，或先做草稿再保存</span></div>
      <div class="card-body">
        <div class="as-actions" style="gap:6px">
          ${Object.entries(KIND_LABEL).map(([k, label]) => `
            <button type="button" class="chip ${d.kind === k ? 'blue' : 'plain'}"
                    data-kind="${k}">${label}</button>`).join('')}
          <span class="t-dim">类型只决定新草稿长成什么样；下面那串下拉是全项目的资产</span>
        </div>
        <div class="form-grid" style="margin-top:10px">
          <label for="as-target">目标资产</label>
          <select id="as-target" class="ctl" data-target>
            <option value="0"${d.targetId === 0 ? ' selected' : ''}>还没保存的新${
              escapeHtml(KIND_LABEL[d.kind])}（草稿）</option>
            ${opts}
          </select>
          ${d.targetId === 0 ? `
            <label for="as-draft-name">名称</label>
            <input id="as-draft-name" class="ctl" data-k="assetName"
                   placeholder="例如 掌灯人" value="${escapeHtml(d.assetName)}">` : ''}
        </div>
      </div>
    </div>`;
}

/** 顶部那一横条提示：冲突、通知、错误三选一，轮询只动这里。 */
function noticeHtml(d) {
  if (d.conflict) {
    return `<div class="bar bad">${escapeHtml(d.conflict)}
      <button class="btn xs ghost" data-dismiss-conflict>知道了</button></div>`;
  }
  if (d.error) return `<div class="bar bad">${escapeHtml(d.error)}</div>`;
  if (d.notice) return `<div class="bar">${escapeHtml(d.notice)}</div>`;
  return '';
}

/* ---------- 局部重绘 ---------- */

/**
 * 只更新"会变的那几格"，不整页重绘（纪律 5）。
 * 每格都是本视图自己渲染出来的节点，改不到人正在敲的那一格。
 */
function patchStatus(root, d) {
  const put = (sel, html) => {
    const el = root.querySelector(sel);
    if (el) el.innerHTML = html;
  };
  put('#as-notice', noticeHtml(d));
  put('#as-jobchip-text', jobChip('文字', d.textJob));
  put('#as-jobchip-image', jobChip('图像', d.imageJob));
  put('#as-preview-slot', d.preview ? previewPanel(d) : '');
  put('#as-cands', candidatesHtml(d));
  // 每一项是 [钮, 哪一轨, 作业, 忙]：判"能不能点"要同时看**这一轨选到档没有**——
  // 只 busy 会把"没选槽位"这种情况放开，点下去吃的是服务端那句载荷规则 422（见 slotMissing）
  for (const [id, which, job, busy] of [
    ['#as-gen-text', 'text', d.textJob, d.busyText],
    ['#as-again-text', 'text', d.textJob, d.busyText],
    ['#as-gen-image', 'image', d.imageJob, d.busyImage],
    ['#as-again-image', 'image', d.imageJob, d.busyImage]]) {
    const el = root.querySelector(id);
    if (!el) continue;
    el.disabled = inflight(job) || busy || slotMissing(d, which);
  }
  bindLive(root, d);
}

/* ---------- 视图 ---------- */

export default {
  async render() {
    const project = store.current;
    if (!project) {
      return `<section class="view on"><div class="card"><div class="empty">
        <div class="empty-t">先选一个项目</div><p>资产创作按项目组织。</p></div></div></section>`;
    }
    const d = ensureDraft();
    // 详情页/分诊页交棒过来的"要改哪一条"：只认一次，且只认同项目
    const handed = takeStudioTarget(project.id);
    if (handed && handed !== d.targetId) {
      d.targetId = handed;
      d.loadedAsset = 0;          // 强制按服务端这一份重新灌一次表单
    }
    const wanted = ++reqSeq;
    let list = [];
    let err = '';
    try {
      // 目标清单**不按类型过滤**：下拉里出现的是"这个项目建设者与造型的全部资产"，
      // 选中谁就把 d.kind 跟到谁（下面 asset 分支里那一行）。
      // 以前这里按当前类型筛，于是"从下拉里挑另一类的资产"这条路是断的：
      // 选项里根本没有它，选中值被浏览器吞成空 → 安静地跳回草稿，
      // 人以为界面卡住了，其实是自己把自己切走了。
      [list, lastOptions] = await Promise.all([
        api.assets(project.id), api.providerOptions()]);
    } catch (e) {
      err = e.message || String(e);
    }
    if (wanted !== reqSeq) return '<section class="view on"></section>';
    if (err) {
      return `<section class="view on">
        <div class="page-head"><h1>资产创作</h1>
          <div class="right">${subChipRow(WS_LABELS, wsIndex())}</div></div>
        ${statusBar()}
        <div class="card"><div class="empty err" style="padding:26px 20px">
          <div class="empty-t">读不到这一页</div><p>${escapeHtml(err)}</p>
          <p><button class="btn" data-retry>重试</button></p></div></div></section>`;
    }

    const asset = d.targetId > 0 ? list.find((a) => a.id === d.targetId) : null;
    if (asset) {
      d.kind = asset.kind;
      d.assetName = asset.name;
      d.baseVersion = asset.version || '';
      // 表单里的字只在这一条资产"第一次被读进来"时从服务端灌一次。
      // 每次刷新都重灌＝把人刚敲进去、还没保存的内容擦掉（上一轮验收里
      // 「填进表单」之后一格都不剩就是这个原因）。
      const hydrate = d.loadedAsset !== asset.id;
      if (hydrate) {
        d.consistency = asset.consistency_prompt || '';
        d.fields.summary = asset.summary || '';
        if (!d.imagePrompt) d.imagePrompt = asset.consistency_prompt || '';
      }
      try {
        const [profile, variants] = await Promise.all([
          d.kind === 'character' ? api.assetProfile(asset.id) : Promise.resolve(null),
          api.assetVariants(asset.id)]);
        if (wanted !== reqSeq) return '<section class="view on"></section>';
        d.baseProfileVersion = (profile && profile.version) || '';
        if (hydrate && profile) {
          d.fields.biography = profile.biography;
          d.fields.personality = profile.personality;
          d.fields.growth_arc = profile.growth_arc;
          d.textPrompt = profile.text_prompt;
        }
        // 形态是服务端的清单，不是人正在敲的文字——它跟着刷新才对
        d.variants = variants;
        d.loadedAsset = asset.id;
      } catch (e) {
        d.notice = `这条资产的档案读不出来：${e.message || e}`;
      }
      // 刷新或切走再回来，"我刚生成的那批"不该消失：状态一律从作业表读，
      // 页面只挑出属于这条资产、每种能力最新的那一条。
      if (!d.textJob || !d.imageJob) {
        try {
          const recent = await api.assetGenerations(project.id, asset.id);
          if (wanted !== reqSeq) return '<section class="view on"></section>';
          const mine = recent.filter((j) => j.target_asset_id === asset.id);
          if (!d.textJob) d.textJob = mine.find((j) => j.operation !== 'asset_image') || null;
          if (!d.imageJob) d.imageJob = mine.find((j) => j.operation === 'asset_image') || null;
        } catch (e) {
          d.notice = `最近的生成记录读不到：${e.message || e}`;
        }
      }
      for (const [which, job] of [['text', d.textJob], ['image', d.imageJob]]) {
        if (!job || job.status !== 'succeeded') continue;
        try {
          if (which === 'text') {
            if (!d.preview) d.preview = await api.assetGeneration(project.id, job.job_id);
          } else if (!d.candidates) {
            const [items, meta] = await Promise.all([
              api.jobArtifacts(job.job_id), api.job(job.job_id).catch(() => null)]);
            if (wanted !== reqSeq) return '<section class="view on"></section>';
            d.candidates = { job_id: job.job_id, items };
            d.jobMeta = meta;
          }
        } catch (e) {
          d.error = `${which === 'text' ? '回稿' : '候选'}读不到：${e.message || e}`;
        }
        if (wanted !== reqSeq) return '<section class="view on"></section>';
      }
    }

    return `
    <section class="view on">
      <div class="page-head">
        <h1>资产创作</h1>
        <div class="right">${subChipRow(WS_LABELS, wsIndex())}</div>
        <p>${escapeHtml(project.name)} · 文字与图像都排进生成中心那一个队列，成本与日志不在这里</p>
      </div>
      ${statusBar()}
      <div id="as-notice">${noticeHtml(d)}</div>
      ${targetPanel(d, list)}
      <div class="as-cols">${textPanel(d)}${imagePanel(d)}</div>
      ${variantPanel(d)}
    </section>`;
  },

  mount(root) {
    const project = store.current;
    if (!project) return;
    const d = ensureDraft();
    const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
    // 判活问的是"本视图自己渲染出来的那一格还在不在"，不是 body 里有没有 root
    const alive = (sel) => Boolean(root.querySelector(sel));

    stopPoll();
    root.querySelector('[data-retry]')?.addEventListener('click', refresh);
    // 页头那一排工作区 chip 必须在这里绑：≤860px 时侧栏二级导航整块隐藏，
    // 而这三个工作区各有各的页头——以前只有「资产库」自己画了切换器，
    // 人从资产库切进「资产创作」之后，窄屏上就再也回不去了（2026-11-04 实测）。
    bindSubChips(root, (i) => { setWsIndex(i); refresh(); });
    // 「知道了」不在这里绑：那颗钮在 #as-notice 里，而 #as-notice 每 tick 被重画一次，
    // 绑在 mount 就等于"作业在飞时关不掉"。它住在 bindLive() 里（见那个函数末尾）。

    root.querySelectorAll('[data-kind]').forEach((el) => {
      el.addEventListener('click', () => { reKind(el.dataset.kind); refresh(); });
    });
    root.querySelectorAll('[data-track]').forEach((el) => {
      el.addEventListener('click', () => {
        const [which, backend] = el.dataset.track.split('|');
        d[`${which}Track`] = backend;
        d[`${which}Provider`] = '';
        d[`${which}Model`] = '';
        d[`${which}Params`] = {};
        refresh();
      });
    });
    root.querySelector('[data-target]')?.addEventListener('change', async (e) => {
      const id = Number(e.target.value);
      if (id === d.targetId) return;
      // 切换目标前先问一句（任务书 §4.3）。为什么"问"而不是"存"：未保存的字
      // 只住在这份模块级草稿里，换目标必然把它换掉——那是设计，不是丢数据；
      // 但人必须知道自己放弃了什么，否则下一次他只会看到"咦，没了"。
      if (d.dirty) {
        const go = await confirmDanger({
          title: '换目标资产', danger: false, okText: '还是要换',
          body: '这一页还有<span class="t-strong">没保存的修改</span>。换目标后这些字会被丢掉'
              + '（不会写进任何资产）；要保住它，先点「保存这份设定」，'
              + '或先把文字复制到别处。',
        });
        if (!go) { e.target.value = String(d.targetId); return; }
      }
      // 上一目标的文字**不带过去**。留着就会出"把甲的履历存进乙"那种事故：
      // 档案读失败时服务端那份盖不掉它，界面显示的是甲、保存写的是乙。
      // 轨道与任务模式是"怎么干"而不是"内容"，留下合理。
      const next = newDraft(d.kind);
      next.textTrack = d.textTrack;
      next.imageTrack = d.imageTrack;
      next.textMode = d.textMode;
      next.targetId = id;
      draft = next;
      refresh();
    });
    root.querySelectorAll('[data-slot]').forEach((el) => {
      el.addEventListener('change', () => {
        const which = el.dataset.slot;
        const [provider, model] = (el.value || '|').split('|');
        d[`${which}Provider`] = provider;
        d[`${which}Model`] = model;
        d[`${which}Params`] = {};
        refresh();
      });
    });
    root.querySelectorAll('[data-field]').forEach((el) => {
      el.addEventListener('input', () => { d.fields[el.dataset.field] = el.value; d.dirty = true; });
    });
    root.querySelectorAll('[data-k]').forEach((el) => {
      const key = el.dataset.k;
      const write = () => {
        d[key] = el.value;
        if (key !== 'assetName') d.dirty = true;
        // 形态提示词与一致性提示词都会进"按形态拼好"那句话；改了就把上一次的
        // 幂等键作废由服务端重算（键派生自最终提示词，不在这里拼）。
      };
      el.addEventListener('input', write);
      // select（扩写哪一段）不走 input 事件里的 textTargetField 分支时也一样要记
      if (key === 'textTargetField') el.addEventListener('change', write);
    });
    root.querySelector('[data-mode]')?.addEventListener('change', (e) => {
      d.textMode = e.target.value;
      // 换模式时把上一次的模式回显清掉：留着会让人以为"这几段还是刚才那次要的"
      d.sections = [];
      refresh();
    });
    root.querySelector('[data-img-variant]')?.addEventListener('change', (e) => {
      d.imageVariantId = Number(e.target.value) || 0;
      refresh();
    });
    root.querySelectorAll('[data-param]').forEach((el) => {
      el.addEventListener('change', () => {
        // data-param 带轨名前缀（text|temperature、image|width）：两条轨都可能有
        // 同名参数，混在一个对象里就成了"改图像的宽度顺手改了文字"
        const [which, name] = el.dataset.param.split('|');
        const bag = d[`${which}Params`] ||= {};
        if (el.type === 'checkbox') {
          bag[name] = el.checked;
        } else if (el.type === 'number') {
          // 数字档必须按数字发：清单的类型检查认的是 float/int，
          // 把 "640" 当字符串发过去会被判成"类型应为 int"，人看到的是参数报错而不是自己填错了
          const n = Number(el.value);
          bag[name] = el.value === '' || Number.isNaN(n) ? el.value : n;
        } else {
          bag[name] = el.value;
        }
      });
    });
    root.querySelectorAll('[data-cloud-confirm]').forEach((el) => {
      el.addEventListener('change', () => { d.cloudConfirm = el.checked; });
    });
    root.querySelector('[data-sync-consistency]')?.addEventListener('click', () => {
      d.imagePrompt = d.consistency;
      d.dirty = true;
      refresh();
    });
    root.querySelector('[data-compose-variant]')?.addEventListener('click', () => {
      const v = d.variants.find((x) => x.id === d.imageVariantId);
      if (!v) return;
      // §6.3 那三样拼在**明面上**：拼完就是提示词框里的文字，人可以接着改。
      // 服务端不背地里再拼一遍——那会让人提交的东西和看到的东西不是同一份。
      d.imagePrompt = [
        d.assetName ? `身份锚点：${d.assetName}` : '',
        d.consistency ? `外形一致性：${d.consistency}` : '',
        `形态：${v.name}${v.prompt ? `——${v.prompt}` : ''}`,
      ].filter(Boolean).join('；');
      d.dirty = true;
      refresh();
    });
    root.querySelector('[data-clear-draft]')?.addEventListener('click', () => {
      draft = newDraft(d.kind);
      refresh();
    });
    root.querySelector('[data-drop-cands]')?.addEventListener('click', () => {
      // 只是这一屏不显示。产物在生成中心、那条作业还在，不删任何东西。
      d.candidates = null;
      d.jobMeta = null;
      refresh();
    });

    /* ---------- 提交生成 ---------- */
    const submit = async (which, body) => {
      if (d[which === 'text' ? 'busyText' : 'busyImage']) return;
      d[which === 'text' ? 'busyText' : 'busyImage'] = true;
      patchStatus(root, d);
      let out = null;
      const ok = await run(which === 'text' ? '生成文字设定' : '生成图像候选',
        () => api.assetGenerationSubmit(project.id, body).then((row) => { out = row; }));
      d[which === 'text' ? 'busyText' : 'busyImage'] = false;
      if (!ok || !out) { patchStatus(root, d); return; }
      const job = { job_id: out.job_id, status: out.status, status_label: out.status_label,
                    operation: out.operation, kind: which };
      if (which === 'text') {
        d.textJob = job; d.preview = null;
        d.sections = out.sections || []; d.reused = !!out.reused;
      } else {
        d.imageJob = job; d.candidates = null; d.jobMeta = null;
        d.reused = !!out.reused;
      }
      d.notice = out.reused
        ? `这次的内容和已在队列里的作业 #${out.job_id} 完全一样，平台没有重复排队。`
            + '要再来一批请点「换一批」或改提示词。'
        : `已排进队列：作业 #${out.job_id}（${publicStatus(out.status)}）`;
      // 确认是一次性的：下一次云端提交要重新勾一次，不然就成了"一直有效"的授权
      d.cloudConfirm = false;
      refresh();      // 新作业要换的不只是状态：幂等回显与按钮文案也跟着变
    };

    const textBody = (nonce) => {
      const mode = d.textMode;
      const head = {
        operation: OPERATION[d.kind],
        target_asset_id: d.targetId > 0 ? d.targetId : null,
        text_mode: mode,
        target_field: mode === 'expand' ? d.textTargetField : '',
        source_excerpt: mode === 'from_source' ? d.sourceExcerpt : '',
        instruction: [d.textPrompt, d.textInstruction].filter(Boolean).join('；'),
        draft_name: d.targetId > 0 ? '' : d.assetName,
        draft_summary: d.targetId > 0 ? '' : (d.fields.summary || ''),
        draft_consistency_prompt: d.targetId > 0 ? '' : d.consistency,
        execution_mode: d.textTrack, provider_code: d.textProvider,
        model_code: d.textModel, params: paramsFor('text', d),
        estimate: estimateFor('text', d), user_confirmed_cloud: d.cloudConfirm,
      };
      if (nonce) head.idempotency_key = nonce;
      return head;
    };
    // 「生成」与「换一批」是两件事：前者把幂等交给服务端按内容派（手滑双击只有一条作业），
    // 后者显式给一个新键——同一句话再跑一批是人的决定，不该由页面替他做，
    // 也不该因为键一样就被挡回去。
    root.querySelectorAll('[data-gen-text]').forEach((btn) => {
      btn.addEventListener('click', () => submit('text', textBody(
        btn.id === 'as-again-text' ? againKey('t') : null)));
    });

    const imageBody = (nonce) => {
      const body = {
        operation: 'asset_image',
        target_asset_id: d.targetId > 0 ? d.targetId : null,
        target_variant_id: d.imageVariantId || null,
        image_prompt: d.imagePrompt,
        execution_mode: d.imageTrack, provider_code: d.imageProvider,
        model_code: d.imageModel, params: paramsFor('image', d),
        estimate: estimateFor('image', d), user_confirmed_cloud: d.cloudConfirm,
      };
      if (nonce) body.idempotency_key = nonce;
      return body;
    };
    root.querySelectorAll('[data-gen-image]').forEach((btn) => {
      btn.addEventListener('click', () => {
        if (!d.imagePrompt.trim()) {
          d.notice = '图像提示词是空的；可以先从一致性提示词同步过来再改。';
          refresh();
          return;
        }
        submit('image', imageBody(btn.id === 'as-again-image' ? againKey('i') : null));
      });
    });

    /* ---------- 保存：显式两步，模型不写库 ---------- */
    root.querySelector('[data-save-text]')?.addEventListener('click', async () => {
      const id = await ensureAsset(d, project.id, refresh);
      if (!id) return;
      const body = { kind: d.kind, name: d.assetName || '未命名',
                     summary: d.fields.summary || '',
                     consistency_prompt: d.consistency, aliases: [], tags: [],
                     scope: 'project', seen_version: d.baseVersion };
      // 409 是"别人也改了"，不是"操作失败"：用页面里的一条横条说清哪一份没保存，
      // 而不是弹一句就完事（任务书 §12 要的那种回答）。
      const stale = (err) => {
        if (!err || err.status !== 409) return false;
        d.conflict = err.message;
        refresh();
        return true;
      };
      let saved;
      try {
        saved = await api.updateAsset(id, body);
      } catch (err) {
        if (stale(err)) return;
        // 不弹 alert：这个仓库里"话要说在页面上"是一条硬纪律（test_infra 钉着），
        // 而且 alert 一响，人正敲的那一格的焦点就没了。
        d.error = `基本信息没存上：${err.message}`;
        refresh();
        return;
      }
      d.baseVersion = saved.version || '';
      if (d.kind === 'character') {
        // 基本信息与档案是两次写：一次失败不会把另一次半路留下，
        // 但界面必须说清"哪一步成了哪一步没成"，不能只报一句"保存失败"。
        // 两把令牌各管各的：档案的令牌不含资产时间，所以前面那次 PUT 资产
        // 不会把这次 PUT 档案判成冲突（服务端 `profile_version` 写了这条理由）。
        let profile = null;
        try {
          profile = await api.saveAssetProfile(id, {
            biography: d.fields.biography || '', personality: d.fields.personality || '',
            growth_arc: d.fields.growth_arc || '', text_prompt: d.textPrompt || '',
            seen_version: d.baseProfileVersion });
        } catch (err) {
          if (stale(err)) return;
          d.notice = `基本信息已存好，人物档案没存上：${err.message}`;
          refresh();
          return;
        }
        if (profile) d.baseProfileVersion = profile.version || '';
      }
      d.dirty = false;
      d.notice = '已保存到库里。';
      refresh();
    });

    /* ---------- 形态 ---------- */
    root.querySelector('[data-add-variant]')?.addEventListener('click', async () => {
      const name = (root.querySelector('#as-new-variant')?.value || '').trim();
      const type = root.querySelector('#as-new-variant-type')?.value || VARIANT_TYPE[d.kind] || 'other';
      if (!name) { d.notice = '先给这个形态起个名字。'; refresh(); return; }
      const ok = await run('新建形态', () => api.createAssetVariant(d.targetId, {
        variant_type: type, name }));
      if (ok) refresh();
    });
    root.querySelectorAll('[data-edit-variant]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const id = Number(btn.dataset.editVariant);
        d.editing = d.editing === id ? null : id;
        refresh();
      });
    });
    root.querySelectorAll('[data-save-variant]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const v = d.variants.find((x) => x.id === Number(btn.dataset.saveVariant));
        if (!v) return;
        const host = root.querySelector(`[data-variant="${v.id}"]`);
        const name = (host?.querySelector('[data-v="name"]')?.value || '').trim() || v.name;
        const prompt = host?.querySelector('[data-v="prompt"]')?.value || '';
        const ok = await run('改形态', () => api.updateAssetVariant(v.id, {
          variant_type: v.variant_type, name, prompt, sort_order: v.sort_order }));
        if (ok) { d.editing = null; refresh(); }
      });
    });
    // 排序：与相邻的形态换 sort_order。两个都整体 PUT，服务端在同一事务里各写各的；
    // 不做"前端先排好再整批提交"，那等于在这里再造一份形态清单的真相。
    root.querySelectorAll('[data-move-variant]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const id = Number(btn.dataset.moveVariant);
        const dir = Number(btn.dataset.dir);
        const idx = d.variants.findIndex((x) => x.id === id);
        const other = d.variants[idx + dir];
        if (!other || idx < 0) return;
        const here = d.variants[idx];
        const a = run('换位', () => api.updateAssetVariant(here.id, {
          variant_type: here.variant_type, name: here.name, prompt: here.prompt,
          sort_order: other.sort_order }));
        const b = run('换位', () => api.updateAssetVariant(other.id, {
          variant_type: other.variant_type, name: other.name, prompt: other.prompt,
          sort_order: here.sort_order }));
        const ok = (await a) && (await b);
        if (ok) refresh();
      });
    });
    root.querySelectorAll('[data-del-variant]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const v = d.variants.find((x) => x.id === Number(btn.dataset.delVariant));
        if (!v) return;
        if (!(await confirmDanger({
          title: '删除这个形态',
          body: `「${escapeHtml(v.name)}」的记录与它登记过的 ${v.image_count} 张图都会清掉；`
              + '基础参考图不受影响。',
          okText: '删除形态',
        }))) return;
        const ok = await run('删除形态', () => api.deleteAssetVariant(v.id));
        if (ok) {
          if (d.imageVariantId === v.id) d.imageVariantId = 0;
          refresh();
        }
      });
    });

    bindLive(root, d);
    startPoll(d, project.id, () => patchStatus(root, d), alive);
  },
};

/**
 * 绑定"会随候选/回稿重绘而出现的那些控件"。
 * mount 与 patchStatus 都调它，所以候选网格与回稿面板换过一次 HTML 之后
 * 按钮不会变哑——上一轮就是漏了这一层：回稿是轮询写进 `#as-preview-slot` 的，
 * 而「填进表单」的监听只在 mount 里绑过一次，于是**结果落地那一刻冒出来的一颗
 * 新按钮根本没人听**，人点它什么也不会发生（不报错过、也不填表）。
 * 凡是"会被 patch 重画出来的交互控件"，绑定必须住在 bindLive 里。
 */
function bindLive(root, d) {
  const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
  root.querySelectorAll('[data-confirm-img]').forEach((btn) => {
    // bindLive 每 tick 都跑一遍，所以这里必须问一句"这颗绑过没有"。
    // 被 patch 重画出来的节点是新对象，加了守卫照样会绑；**没被重画的那几格
    // 是同一批节点**，不守卫就会按轮询次数累加——跑一条 60 秒的图像作业≈20 个
    // tick，之后点一次「删这张」会连发 20 次 DELETE（后十几次 404 变成报错风暴）。
    // 这一条与下面 dropBtn／fillBtn 用的是同一个写法，本轮补齐漏掉的两处。
    if (btn.dataset.bound) return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', async () => {
      const i = Number(btn.dataset.confirmImg);
      const sel = root.querySelector(`[data-save-target="${i}"]`);
      if (!sel || !sel.value) {
        d.notice = '先选存进哪儿：基础参考图，还是某一个形态。';
        refresh();
        return;
      }
      const variantId = sel.value === 'base' ? null : Number(sel.value);
      const v = variantId ? d.variants.find((x) => x.id === variantId) : null;
      btn.disabled = true;
      let out = null;
      const ok = await run('存入资产', () => api.assetImportImage(d.targetId, {
        job_id: d.candidates.job_id, artifact_index: i, variant_id: variantId,
      }).then((row) => { out = row; }));
      btn.disabled = false;
      if (!ok) return;
      d.notice = variantId
        ? `第 ${i + 1} 张已存进形态「${v ? v.name : `#${variantId}`}」，`
            + `现在它名下 ${out.image_count} 张图。基础参考图没动。`
        : `第 ${i + 1} 张已追加进基础参考图（第 ${out.image_count} 张），`
            + '封面没有变；原产物还在生成中心。';
      refresh();
    });
  });
  root.querySelectorAll('[data-vid][data-path]').forEach((btn) => {
    if (btn.dataset.bound) return;          // 同上：形态卡不在 patch 的槽里，节点不换
    btn.dataset.bound = '1';
    btn.addEventListener('click', async () => {
      // 原来这里读的是 `data-rm-vimg` 里用 `|` 拼起来的 `vid|path` 再 split。
      // 复合字段的毛病是路径里**只要出现一个 `|`** 就被切成两段——路径来自后端
      // 生成的 UUID 文件名，眼下不含 `|`，但这等于「靠上游不变量维持正确性」；
      // 而且 `|` 不是任何规范里的分隔符，读代码的人得先去查生成端才知道能切。
      // 拆成两个属性后，split 这一步连同它的隐含契约一起没了。
      const vid = btn.dataset.vid;
      const path = btn.dataset.path;
      if (!(await confirmDanger({
        title: '删掉这张形态图',
        body: `路径 ${escapeHtml(path)} 会从这个形态的清单里摘掉，盘上那份文件也一起删。`
            + '形态本身与它的其他图不动。',
        okText: '删这张',
      }))) return;
      const ok = await run('删这张图', () => api.deleteAssetVariantImage(Number(vid), path));
      if (ok) refresh();
    });
  });
  // 回稿面板是轮询写进来的，所以它的两颗按钮也只能在这里绑（见函数上面那段）
  const dropBtn = root.querySelector('[data-drop-preview]');
  if (dropBtn && !dropBtn.dataset.bound) {
    dropBtn.dataset.bound = '1';
    dropBtn.addEventListener('click', () => { d.preview = null; refresh(); });
  }
  const fillBtn = root.querySelector('[data-fill-preview]');
  if (fillBtn && !fillBtn.dataset.bound) {
    fillBtn.dataset.bound = '1';
    fillBtn.addEventListener('click', () => {
      const f = (d.preview && d.preview.fields) || {};
      Object.entries(f).forEach(([k, v]) => {
        if (!v) return;
        if (k === 'consistency_prompt') d.consistency = v;
        else if (k in d.fields) d.fields[k] = v;
      });
      d.preview = null;
      d.dirty = true;
      d.notice = '回稿已填进表单，还没保存。核对完点「保存这份设定」。';
      refresh();
    });
  }
  root.querySelectorAll('[data-preview-img]').forEach((img) => {
    if (img.dataset.bound) return;          // 候选区会被重画（新节点照绑），形态区不会（防累加）
    img.dataset.bound = '1';
    // dataset 读回来的是解转义后的值，交给 window.open 之前必须再过一次
    // 同源白名单（util.safeOpenUrl 是唯一出处，别在视图里另写一份）。
    img.addEventListener('click', () => {
      const url = safeOpenUrl(img.dataset.previewImg);
      if (url) window.open(url, '_blank', 'noopener,noreferrer');
    });
  });
  // 冲突横条住在 #as-notice 里，每 tick 被 put() 重画一次——所以它的关闭钮也只能
  // 在这里绑。绑在 mount() 里等于"有作业在飞时冲突条关不掉"（本轮修的第二条）。
  const dismissBtn = root.querySelector('[data-dismiss-conflict]');
  if (dismissBtn && !dismissBtn.dataset.bound) {
    dismissBtn.dataset.bound = '1';
    dismissBtn.addEventListener('click', () => { d.conflict = ''; refresh(); });
  }
}

/** 「换一批」用的显式新键：只在这一次点击有效，内容由服务端照原样记着。 */
function againKey(which) {
  const rand = (window.crypto && window.crypto.getRandomValues)
    ? (() => { const a = new Uint8Array(6); window.crypto.getRandomValues(a);
        return Array.from(a).map((b) => b.toString(16).padStart(2, '0')).join(''); })()
    : String(Math.floor(Math.random() * 1e12));
  return `asset-again-${which}-${Date.now().toString(36)}-${rand}`.slice(0, 120);
}

/* ---------- 轮询与草稿保存 ---------- */

function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  // 计时器与 visibilitychange 是一对：只清计时器会留下一个拿着旧 patch/alive
  // 闭包的监听，每次切标签页都白跑一遍（切十个来回叠十份）。
  if (visHandler) { document.removeEventListener('visibilitychange', visHandler); visHandler = null; }
}

/**
 * 轮询在飞的作业。判活问的是本视图自己渲染出来的 `#as-cands` 还在不在——
 * 切走之后 innerHTML 被整个换掉，那一格就没了，计时器立刻停。
 * 生成中心那台节拍器当年就是拿 `document.body.contains(root)` 判活，
 * 而 root 是常驻的 `#view-root`，于是切走之后它还在每 3 秒往不存在的节点上写内容。
 *
 * 拿到结果之后只调 `patch()`（局部重绘），不派 app:refresh：
 * 整页重绘会连人正在敲的那一格一起换掉——值靠模块级草稿是保住了，光标和输入焦点保不住。
 */
function startPoll(d, projectId, patch, alive) {
  const inflightNow = () => [d.textJob, d.imageJob].filter((j) => inflight(j));
  if (!inflightNow().length) return;
  stopPoll();
  // 一轮轮询做的事抽成函数：计时器与"回前台补一拍"共用同一份，
  // 免得两条路径的判活/竞态判据慢慢长歪。
  const tick = async () => {
    if (!alive('#as-cands')) { stopPoll(); return; }
    const wanted = reqSeq;
    let changed = false;
    for (const j of inflightNow()) {
      let next = null;
      try {
        next = await api.assetGeneration(projectId, j.job_id);
      } catch (e) {
        d.error = `作业状态读不到了：${e.message || e}`;
        stopPoll();
        patch();
        return;
      }
      if (wanted !== reqSeq || !alive('#as-cands')) return;   // 已经切走／切目标
      changed = true;
      j.status = next.status;
      j.status_label = next.status_label;
      if (next.status === 'succeeded') {
        if (j.kind === 'text') d.preview = next;
        else {
          try {
            const [items, meta] = await Promise.all([
              api.jobArtifacts(j.job_id), api.job(j.job_id).catch(() => null)]);
            if (wanted !== reqSeq || !alive('#as-cands')) return;
            d.candidates = { job_id: j.job_id, items };
            d.jobMeta = meta;
            d.error = '';
          } catch (e) { d.error = `候选读不到：${e.message || e}`; }
        }
      } else if (['failed', 'cancelled', 'needs_attention'].includes(next.status)) {
        d.error = next.error_message || `作业${publicStatus(next.status)}`;
      }
    }
    if (!changed) return;
    patch();
    if (!inflightNow().length) stopPoll();
  };

  pollTimer = setInterval(() => {
    // 后台标签页不发请求（2026-11-11 审计）。全站 5 处轮询里这里是唯一漏掉的
    // 一处：generate.js:2076 / monitor.js:664 / notifications.js:201 /
    // app.js:797 都有这道早退，只有资产工坊照跑。真浏览器实测：把标签页挤到
    // 后台后 3 秒内，本写法心跳 3 次、monitor 写法 0 次。
    // 判活放在 hidden 之前没有意义（隐藏时连判活都不必做），但**顺序上先判
    // hidden 再判活**会让切走后的计时器多活一轮；这里保持先判活（tick 内第一
    // 行），再判 hidden。
    if (!alive('#as-cands')) { stopPoll(); return; }
    if (document.hidden) return;
    tick();
  }, 3000);

  // 回到前台补一拍：光跳过不够——人切回来时看到的是**离开那一刻**的作业状态，
  // 而本地视频可能早跑完了。补这一下才让"回来看到的就是现在"。
  // visibilitychange 只在 document 上派发、不冒泡到普通元素，所以只能挂
  // document；成对解绑见 stopPoll（test_视图里的_window_监听必须成对解绑）。
  if (visHandler) document.removeEventListener('visibilitychange', visHandler);
  visHandler = () => {
    if (!alive('#as-cands') || document.hidden) return;
    tick();
  };
  document.addEventListener('visibilitychange', visHandler);
}

/**
 * 草稿保存成资产，返回资产 id（失败返回 0）。
 * 名称查重由服务端判（同范围同类型唯一），这里不自动改名——
 * 悄悄改完再回一句"已保存"，人拿到的就不是他打的那个名字。
 */
async function ensureAsset(d, projectId, refresh) {
  if (d.targetId > 0) return d.targetId;
  const name = (d.assetName || '').trim();
  if (!name) {
    d.notice = '草稿还没有名字；先填名称再保存。';
    refresh();
    return 0;
  }
  let created = null;
  const ok = await run('登记资产', () => api.createAsset(projectId, {
    kind: d.kind, name, summary: d.fields.summary || '',
    consistency_prompt: d.consistency, aliases: [], tags: [], scope: 'project',
  }).then((row) => { created = row; }));
  if (!ok || !created) return 0;
  d.targetId = created.id;
  d.baseVersion = created.version || '';
  return created.id;
}
