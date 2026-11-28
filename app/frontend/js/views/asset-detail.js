import { openCharacterAppearances } from './asset-appearances.js';
import { openReferenceUsage } from './asset-reference-usage.js';
import { referenceActions, bindReferenceActions } from './asset-reference-actions.js';
import { askFixReferenceImage, askReversePromptApply, assignReferenceSlots, evaluateReferenceImage, prepareSlotUpload, slotsForKind } from './asset-reference-slots.js';
import { store } from '../store.js';
import { api } from '../api.js';
import {
  checkpointOptionsMarkup, instanceOptionsMarkup, loadComfyCheckpoints,
  loadComfyInstances, rememberCheckpoint, rememberInstanceId, rememberedCheckpoint,
  rememberedInstanceId,
} from '../comfyui.js';
import { bindModalDismiss, confirmDanger, escapeHtml, formatMoney, run, safeOpenUrl } from '../util.js';
import { attachEngineFilter } from '../engine-model-picker.js';
import { HEALTH_LABEL, jobTone, publicStatus } from '../validation.js';
import { setParam, setSub } from '../navstate.js';
import { closeDetail, openDetail } from '../router.js';
import { setStudioTarget, missingOf } from './asset-workspaces.js';
import { ASSET_KIND_LABELS } from '../enums.js';

/**
 * 资产详情。由 assets.js 在地址带 `a<id>` 时调起来——它不是一个独立模块，
 * 所以不往 views/index.js 里注册，也不进侧栏。
 *
 * 五条口径：
 * - 基本信息一次整体提交（PUT），同一个字段不在两处各给一个编辑入口。
 * - 全局资产人人可读，只有管理员可改可删；界面上的按钮按这个显隐，
 *   真正的门在服务端（access.ensure_asset）。
 * - 关联分镜只读＋跳转，不在这里改镜头。
 * - 被引用的资产不给删。这里不藏这个事实：删除弹窗直接把引用数摆出来。
 * - **这一页全是已保存的正式资料**。生成中的作业与还没导入的候选只**显示**，
 *   要改设定、要存图，都跳到资产创作那一页——同一个字段不在两个地方各写一次。
 */
let cache = null;   // 本次渲染拿到的资产、引用、档案与形态，mount 里复用，不再跑第二趟
let refs = [];
let newAssetKind = 'character';
let pendingImages = [];
const TURNAROUND_STORAGE_KEY = 'avp.turnaround.pending';
function loadTurnaroundPending() {
  try {
    const raw = JSON.parse(window.localStorage.getItem(TURNAROUND_STORAGE_KEY) || '[]');
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}
function saveTurnaroundPending(list) {
  pendingTurnaroundJobs = list;
  try {
    if (list.length) window.localStorage.setItem(TURNAROUND_STORAGE_KEY, JSON.stringify(list));
    else window.localStorage.removeItem(TURNAROUND_STORAGE_KEY);
  } catch { /* ignore storage restrictions */ }
}
let pendingTurnaroundJobs = loadTurnaroundPending(); // [{ assetId, jobId, purpose, label, status, imported, importError, error }] 创建角色后待自动挂入标准槽位的生成作业
let pendingTurnaroundNotice = null; // { assetId, kind, text } 创建后跳转到详情页时展示的三视图提交反馈
let activeSection = 'basic';
let activeSectionAssetKey = null;
let nextSection = null;
let keepDrawCardAssetId = null; // 生成角色设定集后保持抽卡面板展开，方便连续重抽或再抽新角色

export function setNewAssetKind(kind) {
  newAssetKind = ['character', 'scene', 'prop'].includes(kind) ? kind : 'character';
}

export function setAssetDetailSection(sec) {
  nextSection = sec;
}

const split = (v) => v.split(/[,，]/).map((x) => x.trim()).filter(Boolean);

async function load(id) {
  let asset = null;
  try {
    asset = await api.asset(id);
  } catch (err) {
    return { error: err.message || '读不到这条资产', status: err.status };
  }
  const [shotsRes, profileRes, variantsRes, gensRes, impactRes, optsRes] = await Promise.allSettled([
    api.assetShots(id),
    asset.kind === 'character' ? api.assetProfile(id) : Promise.resolve(null),
    api.assetVariants(id),
    store.currentId ? api.assetGenerations(store.currentId, id) : Promise.resolve([]),
    asset.ref_shot_count > 0 ? api.assetImpact(id) : Promise.resolve(null),
    store.can('job:submit') ? api.providerOptions({ includeUnregistered: true }) : Promise.resolve([]),
  ]);
  // 读失败不演成"没有镜头引用它"：那会让人以为这条资产可以放心删
  const shots = shotsRes.status === 'fulfilled' ? shotsRes.value : [];
  const refsError = shotsRes.status === 'rejected' ? (shotsRes.reason?.message || String(shotsRes.reason)) : '';
  // 人物档案与命名形态是附属信息：读不到不挡住这一页，但原因要说出来
  const profile = profileRes.status === 'fulfilled' ? profileRes.value : null;
  const profileError = profileRes.status === 'rejected' ? (profileRes.reason?.message || String(profileRes.reason)) : '';
  const variants = variantsRes.status === 'fulfilled' ? variantsRes.value : [];
  const variantsError = variantsRes.status === 'rejected' ? (variantsRes.reason?.message || String(variantsRes.reason)) : '';
  // 最近生成记录：刷新后"我刚跑的那两条"还要看得见，读的是 job 表而不是浏览器记忆。
  const gens = gensRes.status === 'fulfilled'
    ? (gensRes.value || []).filter((j) => j.target_asset_id === id)
    : [];
  const gensError = gensRes.status === 'rejected' ? (gensRes.reason?.message || String(gensRes.reason)) : '';
  const impact = impactRes.status === 'fulfilled' ? impactRes.value : null;
  const textOptions = optsRes.status === 'fulfilled'
    ? (optsRes.value || []).filter((o) => o.capability === 'text')
    : [];
  const imageOptions = optsRes.status === 'fulfilled'
    ? (optsRes.value || []).filter((o) => o.capability === 'image')
    : [];
  return { asset, shots, refsError, profile, profileError, variants, variantsError,
           gens, gensError, impact, textOptions, imageOptions };
}

export async function renderDetail(id) {
  const isNew = id === 'new';
  const requestedSection = nextSection;
  nextSection = null;
  if (isNew) {
    pendingImages.forEach((p) => { if (p.url) URL.revokeObjectURL(p.url); });
    pendingImages = [];
  }
  const newProviderOptions = isNew && store.can('job:submit')
    ? await api.providerOptions({ includeUnregistered: true }).catch(() => [])
    : [];
  const newTextOptions = newProviderOptions.filter((o) => o.capability === 'text');
  const newImageOptions = newProviderOptions.filter((o) => o.capability === 'image');
  const data = isNew ? {
    isNew: true,
    asset: {
      id: null, project_id: store.currentId, kind: newAssetKind,
      kind_label: ASSET_KIND_LABELS[newAssetKind] || '角色',
      name: '', summary: '', consistency_prompt: '', tags: [], aliases: [],
      ref_images: [], ref_shot_count: 0, image_purposes: {}, version: '',
      created_at: '', updated_at: '',
    },
    shots: [], refsError: '', profile: null, profileError: '',
    variants: [], variantsError: '', gens: [], gensError: '', impact: null,
    textOptions: newTextOptions, imageOptions: newImageOptions,
  } : await load(id);
  cache = data;
  refs = data.shots || [];
  const variants = data.variants || [];

  if (data.error) {
    return `
    <section class="view on">
      <div class="page-head"><h1>资产详情</h1></div>
      <div class="card"><div class="empty">
        <div class="empty-t">打不开这条资产</div>
        <p>${escapeHtml(data.error)}${data.status === 404 ? '（可能已被删除）' : ''}</p>
        <p><button class="btn" data-back>返回资产列表</button></p>
      </div></div>
    </section>`;
  }

  const a = data.asset;
  const isGlobal = a.project_id === null;
  const impactShots = (data.impact && data.impact.shots) || [];
  const impactMap = new Map(impactShots.map((item) => [Number(item.shot_id), item]));
  const impactCounts = data.impact?.counts || null;
  const signedCount = impactShots.filter((item) => item.signed).length;
  // 能不能改：全局要管理员，项目要 asset:write（服务端同样判一遍）
  const canEdit = isNew ? store.can('asset:write') : (store.can('asset:write') && (!isGlobal || store.isManager));
  // 译名从 enums.js 取（2026-11-10 统一为「角色」）：这里原来又抄了一份，
  // 三处副本迟早会和 `ASSET_KINDS` 分叉。
  const kindOptions = Object.entries(ASSET_KIND_LABELS)
    .map(([v, l]) => `<option value="${v}"${a.kind === v ? ' selected' : ''}>${l}</option>`).join('');

  const sectionAssetKey = isNew ? 'new' : String(id);
  if (activeSectionAssetKey !== sectionAssetKey) {
    activeSection = 'basic';
    activeSectionAssetKey = sectionAssetKey;
  }
  activeSection = requestedSection ?? activeSection;
  const validSections = isNew ? ['basic', 'refs'] : ['basic', 'refs', 'profile', 'usage'];
  if (!validSections.includes(activeSection)) activeSection = 'basic';
  const refCount = isNew ? pendingImages.length : (a.ref_images || []).length;
  const usageCount = (data.gens || []).length + refs.length;
  const secTabs = isNew
    ? [
      { key: 'basic', label: '基本信息' },
      { key: 'refs', label: '参考图', count: refCount },
    ]
    : [
      { key: 'basic', label: '基本信息' },
      { key: 'refs', label: '参考图', count: refCount },
      { key: 'profile', label: a.kind === 'character' ? '档案与形态' : '命名形态', count: variants.length },
      { key: 'usage', label: '生成与引用', count: usageCount },
    ];

  return `
    <section class="view on">
      <div class="page-head ad-page-head">
        <h1><button class="crumb" data-back>资产中心</button>
          <span class="crumb-sep">›</span> ${isNew ? '新建资产' : escapeHtml(a.name)}</h1>
        <p>${isNew
          ? '填写基本信息并可预先挑选参考图，点击右上角「保存」创建资产'
          : `${a.kind_label} · ${isGlobal ? '全局资产池（各项目共享引用，不属于单个项目）'
             : '本项目资产'} · ${a.ref_images.length} 张参考图 · 被 ${a.ref_shot_count} 条镜头引用`}</p>
        <div class="right">
          ${!isNew && a.kind === 'character' && canEdit ? '<button type="button" class="btn" id="ad-new-draw" title="保留当前角色，直接开启一张新角色抽卡">✨ 再抽新角色</button>' : ''}
          ${!isNew && canEdit ? '<button class="btn danger" id="ad-del">删除</button>' : ''}
          ${canEdit ? `<button class="btn primary" id="ad-save">${isNew ? '保存并创建' : '保存'}</button>` : ''}
        </div>
      </div>

      <div class="ad-sec-tabs" role="tablist" aria-label="切换展示板块">
        ${secTabs.map((t) => `<button type="button" class="ad-sec-tab${activeSection === t.key ? ' on' : ''}" role="tab" aria-selected="${activeSection === t.key ? 'true' : 'false'}" data-ad-tab="${t.key}">${escapeHtml(t.label)}${typeof t.count === 'number' ? ` <span class="ad-sec-cnt">${t.count}</span>` : ''}</button>`).join('')}
      </div>

      <div class="ad-panel" data-ad-panel="basic"${activeSection === 'basic' ? '' : ' hidden'}>
        <div class="card">
            <div class="card-head"><h2>基本信息</h2>
              <span class="sub">名称在同一范围、同一类型里唯一</span>
              ${!isNew && a.kind === 'character' && canEdit && store.can('job:submit') && store.currentId
                ? `<div class="right"><button type="button" class="btn sm" id="ad-toggle-draw">${keepDrawCardAssetId === a.id ? '收起抽卡面板' : '🎲 抽卡 / 重生设定集'}</button></div>`
                : ''}
            </div>
            <div class="card-body">
              ${(isNew || a.kind === 'character') && canEdit && store.can('job:submit') && store.currentId ? (() => {
                const drawCardHidden = isNew ? (a.kind !== 'character') : (keepDrawCardAssetId !== a.id);
                const isBadHealth = (o) => ['missing', 'unconfigured', 'unregistered'].includes(o.health_state);
                const textOpts = data.textOptions || [];
                const textPref = textOpts.find((o) => o.backend === 'local' && !isBadHealth(o) && (o.health_state === 'ok' || o.validation_state === 'ok'))
                  || textOpts.find((o) => !isBadHealth(o) && (o.health_state === 'ok' || o.validation_state === 'ok'))
                  || textOpts.find((o) => !isBadHealth(o))
                  || textOpts[0];
                const textOptHtml = textOpts.map((o, idx) => {
                  const val = `${o.backend}|${o.provider}|${o.model}`;
                  const sel = textPref && o === textPref ? ' selected' : (!textPref && idx === 0 ? ' selected' : '');
                  const tag = o.backend === 'cloud' ? '云端' : '本地';
                  const warn = isBadHealth(o)
                    ? (o.backend === 'local' && o.provider === 'ollama' ? '（未启动，将自动拉起）' : `（${HEALTH_LABEL[o.health_state] || '不可用'}）`)
                    : (o.validation_state === 'ok' ? '（已验证）' : '');
                  return `<option value="${escapeHtml(val)}"${sel}>${tag} · ${escapeHtml(o.adapter_label || o.provider)} / ${escapeHtml(o.model)}${warn}</option>`;
                }).join('');
                const imgOpts = data.imageOptions || [];
                const imgPref = imgOpts.find((o) => o.backend === 'local' && !isBadHealth(o) && (o.health_state === 'ok' || o.validation_state === 'ok'))
                  || imgOpts.find((o) => !isBadHealth(o) && (o.health_state === 'ok' || o.validation_state === 'ok'))
                  || imgOpts.find((o) => !isBadHealth(o))
                  || imgOpts[0];
                const imgOptHtml = imgOpts.map((o, idx) => {
                  const val = `${o.backend}|${o.provider}|${o.model}`;
                  const sel = imgPref && o === imgPref ? ' selected' : (!imgPref && idx === 0 ? ' selected' : '');
                  const tag = o.backend === 'cloud' ? '云端' : '本地';
                  const warn = isBadHealth(o) ? `（${HEALTH_LABEL[o.health_state] || '不可用'}）`
                    : (o.validation_state === 'ok' ? '（已验证）' : '');
                  return `<option value="${escapeHtml(val)}"${sel}>${tag} · ${escapeHtml(o.adapter_label || o.provider)} / ${escapeHtml(o.model)}${warn}</option>`;
                }).join('');
                return `<div class="ad-random-card" id="ad-random-card"${drawCardHidden ? ' hidden' : ''}>
                  <div class="ad-random-row">
                    <div class="ad-random-title"><b>${isNew ? '🎲 随机角色抽卡（两步确认流）' : '🎲 角色抽卡与重抽（可继续微调或重新生成）'}</b>
                      <span class="t-dim">${isNew
                        ? '第一步选模型随机生成完整人设，可反复重抽微调；第二步保存角色并异步生成三视图'
                        : '可重骰人设或调整画风/模型后重新生成设定集，也可直接点击「再抽一个新角色」'}</span></div>
                  </div>
                  <div class="ad-random-row">
                    <label class="ad-tv-label">人设文本模型
                      <select class="ctl sm" id="ad-rand-model" aria-label="人设文本模型">
                        ${textOptHtml || '<option value="">暂无可用文本模型（请先到模型中心配置）</option>'}
                      </select>
                    </label>
                    <div class="ad-random-controls">
                      <input class="ctl sm" id="ad-rand-theme" maxlength="100"
                        placeholder="灵感词（选填，留空纯随机，如：赛博猫娘、白发异瞳）">
                      <button type="button" class="btn sm" id="ad-rand-btn">🎲 随机生成人设</button>
                      <button type="button" class="btn sm" id="ad-rand-cancel" hidden>取消</button>
                    </div>
                  </div>
                  <div class="ad-random-row ad-random-tv">
                    <label class="ad-tv-label">生图模型
                      <select class="ctl sm" id="ad-tv-model" aria-label="生图模型">
                        ${imgOptHtml || '<option value="">暂无可用生图模型（请先到模型中心配置）</option>'}
                      </select>
                    </label>
                    <label class="ad-tv-label" id="ad-tv-tpl-wrap" hidden>工作流模板
                      <select class="ctl sm" id="ad-tv-template" aria-label="生图参数模板">
                        <option value="">默认参数（不套模板）</option>
                      </select>
                    </label>
                    <label class="ad-tv-label" id="ad-tv-inst-wrap" hidden>节点实例
                      <select class="ctl sm" id="ad-tv-instance" aria-label="ComfyUI 节点实例">
                        <option value="">默认启用实例</option>
                      </select>
                    </label>
                    <label class="ad-tv-label" id="ad-tv-ckpt-wrap" hidden>Checkpoint
                      <select class="ctl sm" id="ad-tv-checkpoint" aria-label="ComfyUI Checkpoint">
                        <option value="">自动匹配（加载中…）</option>
                      </select>
                    </label>
                  </div>
                  <div class="ad-random-row ad-random-actions">
                    <div class="ad-random-opts">
                      <select class="ctl sm" id="ad-tv-style" aria-label="生成画风">
                        <option value="25d"${(store.current?.art_style || '2d') === '25d' ? ' selected' : ''}>2.5D 半写实厚涂</option>
                        <option value="3d"${(store.current?.art_style || '2d') === '3d' ? ' selected' : ''}>3D 国漫 CG 渲染</option>
                        <option value="2d"${(store.current?.art_style || '2d') === '2d' ? ' selected' : ''}>2D 日漫赛璐璐</option>
                      </select>
                      <select class="ctl sm" id="ad-tv-mode" aria-label="生成模式">
                        <option value="quad">4 格设定集（封面/三视图/特写/背面）</option>
                        <option value="single">仅三视图（1 张）</option>
                      </select>
                    </div>
                    <div class="ad-random-btns">
                      <button type="button" class="btn primary sm" id="ad-save-tv"${imgOpts.length ? '' : ' disabled'}>${isNew ? '🎨 保存并生成设定集' : '🎨 保存并重新生成设定集'}</button>
                      ${!isNew ? '<button type="button" class="btn sm" id="ad-draw-another" title="保留当前角色，直接新建下一张角色抽卡">✨ 再抽一个新角色</button>' : ''}
                      <button type="button" class="btn sm" id="ad-copy-gpt" title="复制适合直接导入平台 4 个标准槽位的 GPT 生图指令">📋 复制 GPT 4图提示词</button>
                    </div>
                  </div>
                  <p class="t-dim ad-random-status" id="ad-rand-status" role="status"></p>
                </div>`;
              })() : ''}
              <div class="ad-basic-form">
                <div class="ad-field">
                  <label for="ad-kind">资产类型</label>
                  <select id="ad-kind" class="ctl"${canEdit ? '' : ' disabled'}>${kindOptions}</select>
                </div>

                <div class="ad-field">
                  <label for="ad-name">名称</label>
                  <input id="ad-name" class="ctl" maxlength="60" value="${escapeHtml(a.name)}"
                         autocomplete="off"${canEdit ? '' : ' disabled'}>
                </div>

                <div class="ad-field">
                  <label for="ad-tags">标签</label>
                  <input id="ad-tags" class="ctl" value="${escapeHtml((a.tags || []).join(', '))}"
                         placeholder="逗号分隔，最多 10 个"${canEdit ? '' : ' disabled'}>
                </div>

                <div class="ad-field">
                  <label for="ad-aliases">别名</label>
                  <input id="ad-aliases" class="ctl" value="${escapeHtml((a.aliases || []).join(', '))}"
                         placeholder="逗号分隔"${canEdit ? '' : ' disabled'}>
                </div>

                <div class="ad-field ad-field-full">
                  <label for="ad-summary">简介</label>
                  <textarea id="ad-summary" class="ctl" rows="2" maxlength="500"
                            ${canEdit ? '' : 'disabled'}>${escapeHtml(a.summary)}</textarea>
                </div>

                <div class="ad-field ad-field-full">
                  <label for="ad-prompt">一致性提示词</label>
                  <div class="ad-prompt-col">
                    <textarea id="ad-prompt" class="ctl" rows="6" maxlength="1000"
                              placeholder="可先写几个关键特征词，再点下方「AI 完善提示词」扩写润色"${canEdit ? '' : ' disabled'}
                        >${escapeHtml(a.consistency_prompt)}</textarea>
                    ${canEdit && store.can('job:submit') && store.currentId ? (() => {
                      const textOpts = data.textOptions || [];
                      const isBadHealth = (o) => ['missing', 'unconfigured', 'unregistered'].includes(o.health_state);
                      const preferred = textOpts.find((o) => o.backend === 'local' && !isBadHealth(o) && (o.health_state === 'ok' || o.validation_state === 'ok'))
                        || textOpts.find((o) => !isBadHealth(o) && (o.health_state === 'ok' || o.validation_state === 'ok'))
                        || textOpts.find((o) => !isBadHealth(o))
                        || textOpts[0];
                      const optHtml = textOpts.map((o, idx) => {
                        const val = `${o.backend}|${o.provider}|${o.model}`;
                        const sel = preferred && o === preferred ? ' selected' : (!preferred && idx === 0 ? ' selected' : '');
                        const tag = o.backend === 'cloud' ? '云端' : '本地';
                        const warn = isBadHealth(o) ? `（${HEALTH_LABEL[o.health_state] || '不可用'}）`
                          : (o.validation_state === 'ok' ? '（已验证）' : '');
                        return `<option value="${escapeHtml(val)}"${sel}>${tag} · ${escapeHtml(o.adapter_label || o.provider)} / ${escapeHtml(o.model)}${warn}</option>`;
                      }).join('');
                      return `
                      <div class="ad-ai-bar">
                        <div class="ad-ai-group ad-ai-group-grow">
                          <select class="ctl sm ad-ai-model" id="ad-ai-model" aria-label="选择文本模型">
                            ${optHtml || '<option value="">暂无可用文本模型</option>'}
                          </select>
                          <button type="button" class="btn sm" id="ad-ai-refine">✨ AI 完善提示词</button>
                        </div>
                        <div class="ad-ai-group">
                          <select class="ctl sm ad-vision-provider" id="ad-vision-provider" aria-label="看图反推通道">
                            <option value="auto">自动通道（本地优先）</option>
                            <option value="ollama">本地 Ollama</option>
                            <option value="chatgpt">ChatGPT 套餐</option>
                            <option value="gemini">Google Gemini</option>
                            <option value="dashscope">通义千问 VL</option>
                          </select>
                          <select class="ctl sm ad-vision-lang" id="ad-vision-lang" aria-label="看图反推语言">
                            <option value="zh">中文提示词</option>
                            <option value="en">英文 Tag</option>
                            <option value="bilingual">中英双语</option>
                          </select>
                          <button type="button" class="btn sm" id="ad-ai-reverse"${isNew || !a.ref_images.length ? ' disabled' : ''} title="${isNew ? '请先保存资产并上传参考图' : (!a.ref_images.length ? '请先在「参考图」板块上传参考图' : '综合封面与三视图反向生成一致性提示词')}">📷 看图反推</button>
                        </div>
                      </div>
                      <div class="ad-ai-feedback">
                        <span class="t-dim" id="ad-ai-status" role="status"></span>
                        <button type="button" class="btn xs ghost" id="ad-ai-cancel" hidden>取消反推</button>
                        <button type="button" class="btn xs ghost" id="ad-ai-undo" hidden>撤销还原</button>
                      </div>`;
                    })() : ''}
                    <p class="ad-prompt-tip">写作要点：${a.kind === 'character'
                      ? '面部与识别特征、发型、服装、体型与总体风格、必须保持不变的细节。'
                      : a.kind === 'scene'
                        ? '空间结构、建筑风格、环境与光照氛围、需要保持一致的固定元素。'
                        : '形状、材质、颜色、尺寸与比例、独特标记。'}喂给生成模型的稳定描述，最终由你定稿。</p>
                  </div>
                </div>

                <div class="ad-field ad-field-full">
                  <span class="ad-field-label">归属</span>
                  ${isGlobal
                    ? '<span class="t-dim">全局资产池 —— 已属于所有项目，不能收回成单个项目资产'
                      + '（可能正被多个项目的镜头引用，收回会让引用悬空）</span>'
                    : (store.isManager
                        ? `<label class="chk"><input type="checkbox" id="ad-global">
                             <span>${isNew ? '放进全局资产池（各项目都能引用，不属于当前项目）' : '升为全局资产（id 不变，现有引用照旧有效）'}</span></label>`
                        : '<span class="t-dim">本项目资产（全局资产只有管理员能建）</span>')}
                </div>
              </div>
              ${isNew ? '<p class="modal-meta">同一范围、同一类型里名称唯一；重名说明该合并，不是新建。</p>'
                : `<p class="modal-meta">创建于 ${a.created_at}　最后修改 ${a.updated_at}${canEdit ? '' : '　· 你对此资产只读'}</p>`}
              ${a.ref_shot_count ? `<p class="note" style="margin-top:8px">当前被 <b>${a.ref_shot_count}</b> 条镜头引用${signedCount ? `（含 <b>${signedCount}</b> 条已签字正式产物）` : ''}${impactCounts?.stale ? `，其中 <b>${impactCounts.stale}</b> 镜源已过期` : ''}；修改一致性提示词或更换封面后，相关镜头产物将标记为源已变。</p>` : ''}
            </div>
          </div>

          ${isNew ? '' : (() => {
            const miss = missingOf(a, data.profile, variants);
            return `<div class="ad-miss-bar">
              <span class="ad-miss-label">资料完整度</span>
              ${miss.length
                ? `<div class="ad-miss-list">${miss.map((m) => `<span class="chip amber">缺 · ${escapeHtml(m)}</span>`).join('')}</div>
                   <button class="btn sm" data-goto-studio>去资产创作补齐</button>`
                : '<span class="t-dim">基础资料已齐备（实际效果以生成画面为准）</span>'}
            </div>`;
          })()}
      </div>

      <div class="ad-panel" data-ad-panel="refs"${activeSection === 'refs' ? '' : ' hidden'}>
        <div class="card">
            <div class="card-head"><h2>参考图</h2>
              <span class="sub">第一张是封面</span>
              <div class="right">
                ${!isNew && a.kind === 'character' && store.currentId && store.can('job:read') && (a.project_id == null || a.project_id === store.currentId)
                  ? '<button class="btn sm" data-appearance-open>角色出镜对照</button>' : ''}
                ${!isNew && a.kind === 'character'
                  ? '<button type="button" class="btn sm" data-copy-quad-prompt title="复制 4 格角色设定图提示词（可直接粘贴到 ChatGPT Plus GPT Image 或 Google AI Pro Nano Banana 出图）">📋 4图设定提示词</button><button type="button" class="btn sm" data-copy-expr-prompt title="为主角复制 6 种情绪表情差分提示词（建议附上面部特写图发给 GPT / Nano Banana 出图，再导入补充参考图）">🎭 表情差分提示词</button>' : ''}
                ${canEdit && isNew ? `<label class="btn sm ad-upload">预选补充参考图
                  <input type="file" id="ad-upload" accept="image/png,image/jpeg,image/webp" multiple hidden>
                </label>` : ''}
              </div></div>
            <div class="card-body">
              ${(() => {
                const activeJobs = !isNew
                  ? pendingTurnaroundJobs.filter((j) => j.assetId === a.id)
                  : [];
                if (activeJobs.length) {
                  const doneCount = activeJobs.filter((j) => j.status === 'succeeded').length;
                  const summaryText = activeJobs.map((j) => {
                    const tag = j.status === 'succeeded' ? '✓'
                      : (j.status === 'failed' || j.status === 'cancelled' ? '✗' : '生成中');
                    return `${j.label} ${tag}`;
                  }).join(' · ');
                  return `<div class="bar info ad-tv-watch" id="ad-tv-watch" role="status">🎨 正在生成角色设定图（已完成 ${doneCount}/${activeJobs.length}）：${escapeHtml(summaryText)} <a href="#generate/0">去生成中心查看</a>${canEdit ? ' <button type="button" class="btn sm" data-goto-draw>🎲 返回抽卡重抽</button> <button type="button" class="btn sm" data-new-draw>✨ 再抽新角色</button>' : ''}</div>`;
                }
                if (!isNew && pendingTurnaroundNotice && pendingTurnaroundNotice.assetId === a.id) {
                  const notice = pendingTurnaroundNotice;
                  pendingTurnaroundNotice = null;
                  return `<div class="bar ${notice.kind === 'bad' ? 'bad' : 'info'} ad-tv-watch" id="ad-tv-watch" role="status">${escapeHtml(notice.text)} <a href="#models">检查模型中心</a> · <a href="#asset-studio">去资产创作重试</a></div>`;
                }
                return '';
              })()}
              ${isNew ? '<div class="ad-slots" data-ad-slots></div><div class="ad-imgs" data-ad-imgs></div>' : (() => {
                const { slots, extra } = assignReferenceSlots(a.kind, a.ref_images, a.image_purposes);
                const canUse = Boolean(store.currentEpisodeId && store.can('job:submit'));
                const coverPath = a.ref_images[0] || '';
                const renderCard = (p, slotMeta = null) => {
                  const isCover = p === coverPath;
                  return `<figure class="ad-img${isCover ? ' cover' : ''}">
                    ${slotMeta ? `<div class="ad-slot-bar"><b class="ad-slot-title">${escapeHtml(slotMeta.label)}</b><span class="ad-slot-ratio">${escapeHtml(slotMeta.spec)}</span></div>` : ''}
                    <div class="ad-img-wrap">
                      <img src="${escapeHtml(`/files/${p}`)}" alt="${escapeHtml(slotMeta ? slotMeta.label : '参考图')}" loading="lazy" data-preview="${escapeHtml(`/files/${p}`)}" data-ref-path="${escapeHtml(p)}"${slotMeta ? ` data-slot-key="${escapeHtml(slotMeta.key)}"` : ''}>
                      <span class="ad-dim-badge" data-dim-badge hidden></span>
                    </div>
                    <div class="ad-check-msg" data-check-msg hidden></div>
                    <figcaption>
                      ${isCover ? '<span class="chip blue">封面</span>'
                        : (canEdit ? `<button class="btn xs" data-cover="${escapeHtml(p)}">设为封面</button>` : '')}
                      ${canEdit ? `<button class="btn xs danger" data-rm-img="${escapeHtml(p)}">删除</button>` : ''}
                    </figcaption>
                    ${referenceActions(p, a.image_purposes?.[p], canEdit, canUse, Boolean(slotMeta))}
                  </figure>`;
                };
                const slotsHtml = slots.map((s) => (s.path ? renderCard(s.path, s) : `
                  <div class="ad-slot-empty">
                    <div class="ad-slot-bar">
                      <b class="ad-slot-title">${escapeHtml(s.label)}</b>
                      <span class="ad-slot-ratio">${escapeHtml(s.spec)}</span>
                    </div>
                    <div class="ad-slot-drop">
                      <p class="ad-slot-hint">${escapeHtml(s.hint)}</p>
                      ${canEdit ? `<label class="btn xs primary ad-slot-upload">上传到此槽
                        <input type="file" data-slot-upload="${escapeHtml(s.key)}" accept="image/png,image/jpeg,image/webp" hidden>
                      </label>` : '<span class="t-dim">待补</span>'}
                    </div>
                  </div>`)).join('');
                const extraHtml = `
                  <div class="ad-extra-section">
                    <div class="ad-extra-bar">
                      <div class="ad-extra-info">
                        <b class="ad-extra-title">补充参考图（${extra.length}）</b>
                        <span class="t-dim">可上传表情差分、服饰拆解、武器与配饰特写，并逐张命名</span>
                      </div>
                      ${canEdit ? `<label class="btn sm">+ 上传补充参考图
                        <input type="file" id="ad-upload" accept="image/png,image/jpeg,image/webp" hidden>
                      </label>` : ''}
                    </div>
                    ${extra.length
                      ? `<div class="ad-imgs" data-ad-imgs>${extra.map((p) => renderCard(p, null)).join('')}</div>`
                      : '<div class="ad-extra-empty">暂无补充参考图，点击右上角「+ 上传补充参考图」即可添加（支持交互式裁剪与自定义用途名称）。</div>'}
                  </div>`;
                return `<div class="ad-slots">${slotsHtml}</div>${extraHtml}`;
              })()}
              ${canEdit ? `<p class="modal-meta">短边 ≥ 512px，支持 PNG / JPEG / WebP（≤20MB），按槽位上传可对齐推荐比例。</p>` : ''}
            </div>
          </div>
      </div>

      ${isNew ? '' : `
      <div class="ad-panel" data-ad-panel="profile"${activeSection === 'profile' ? '' : ' hidden'}>
        ${data.profileError ? `<div class="card"><div class="bar bad">
          人物档案读不到：${escapeHtml(data.profileError)}</div></div>` : ''}
        ${a.kind === 'character' && !data.profileError ? `
        <div class="card">
          <div class="card-head"><h2>人物档案</h2>
            <span class="sub">${data.profile && data.profile.exists ? '最后改于 ' + escapeHtml(data.profile.updated_at) : '还没写过'}</span>
            <div class="right"><button class="btn sm" data-goto-studio>去创作</button></div></div>
          <div class="card-body">
            <div class="ad-profile-grid">
              ${[['履历', 'biography'], ['性格', 'personality'], ['成长过程', 'growth_arc'],
                 ['文字设定提示词', 'text_prompt']].map(([label, key]) => `
                <div class="ad-profile-item">
                  <b>${label}</b>
                  <p>${escapeHtml((data.profile && data.profile[key]) || '—')}</p>
                </div>`).join('')}
            </div>
            ${data.profile ? `<p class="note" style="margin-top:12px"><b>专属 TTS 配音绑定：</b>
              声线 ${escapeHtml(data.profile.tts_voice || '默认')} ·
              语速 ${escapeHtml(String(data.profile.tts_rate ?? 1.0))}x ·
              情绪／造声 ${escapeHtml(data.profile.tts_mood || '默认')}</p>` : ''}
            <p class="ad-prompt-tip">档案与一致性提示词是两回事：这一份讲"这个人是怎么走到今天的"，基本信息里讲"他画出来必须长什么样"。这里只读，改在资产创作。</p>
          </div>
        </div>` : ''}
        ${data.variantsError ? `<div class="card"><div class="bar bad">
          形态清单读不到：${escapeHtml(data.variantsError)}</div></div>` : ''}
        ${!data.variantsError ? `
        <div class="card">
          <div class="card-head"><h2>命名形态</h2>
            <span class="sub">${variants.length} 个 · 图跟着形态走</span>
            <div class="right"><button class="btn sm" data-goto-studio>加形态</button></div></div>
          <div class="card-body">
            ${variants.length ? variants.map((v) => `
              <div class="as-variant">
                <div class="as-variant-head"><b>${escapeHtml(v.name)}</b>
                  <span class="chip plain">${escapeHtml(v.variant_type_label)}</span>
                  <span class="t-dim">${v.image_count} 图${v.source_job_id ? ` · 来源作业 #${v.source_job_id}` : ''}</span></div>
                ${v.prompt ? `<p class="t-desc">${escapeHtml(v.prompt)}</p>` : ''}
                ${v.ref_images.length ? `<div class="ad-imgs">${v.ref_images.map((ip) => `
                  <figure class="ad-img"><img src="${escapeHtml(`/files/${ip}`)}" alt="${escapeHtml(v.name)} 的图"
                    loading="lazy" data-preview="${escapeHtml(`/files/${ip}`)}"></figure>`).join('')}</div>`
                  : '<p class="t-dim">这个形态还没有图。</p>'}
              </div>`).join('')
              : '<p class="t-dim">还没有形态。服饰、视角、形态各存各的图，'
                + '下游才不会被互相矛盾的服装带跑。</p>'}
          </div>
        </div>` : ''}
      </div>

      <div class="ad-panel" data-ad-panel="usage"${activeSection === 'usage' ? '' : ' hidden'}>
        <div class="card">
          <div class="card-head"><h2>最近生成记录</h2>
            <span class="sub">${(data.gens || []).length} 条 · 只读</span>
            <div class="right"><button class="btn sm" data-goto-studio>去生成</button></div></div>
          <div class="card-body">
            ${data.gensError ? `<div class="bar bad">生成记录读不到：${escapeHtml(data.gensError)}</div>` : ''}
            <details class="ad-fold" open>
              <summary class="ad-fold-sum">${(data.gens || []).length ? `共 ${(data.gens || []).length} 条生成记录` : '暂无生成记录（产物需在「资产创作」显式存入）'}</summary>
              <div class="ad-fold-body">
                ${(data.gens || []).length ? (data.gens || []).map((j) => `
                  <div class="ad-ref">
                    <div>
                      <div class="t-strong">${escapeHtml(j.operation_label || j.operation)}
                        <span class="chip ${jobTone(j.status)}" title="${escapeHtml(j.status_label || j.status)}">${escapeHtml(publicStatus(j.status))}</span></div>
                      <p class="t-dim">作业 #${j.job_id} · ${escapeHtml(
                        (j.execution_mode === 'cloud' ? '云端' : '本地'))}
                        ${escapeHtml(j.provider_code || '')}/${escapeHtml(j.model_code || '')}
                        ${j.cost_micros ? `· 预计 ${escapeHtml(formatMoney(j.cost_micros, j.currency))}` : ''}
                        ${j.target_variant_id ? `· 形态 #${j.target_variant_id}` : ''}</p>
                    </div>
                    <a class="btn xs" href="#generate/0">查看</a>
                  </div>`).join('')
                  : '<p class="t-dim">暂无记录。图像生成成功后需预览并显式存入资产。</p>'}
              </div>
            </details>
          </div>
        </div>

        <div class="card">
          <div class="card-head"><h2>被哪些镜头引用</h2>
            <span class="sub">${refs.length} 条 · 只读${impactCounts?.stale ? ` · ${impactCounts.stale} 镜源已变` : ''}</span>
            ${store.currentId && store.can('job:read') && (a.project_id == null || a.project_id === store.currentId)
              ? `<div class="right"><button class="btn xs" data-reference-usage-open>查作业实际用图</button>
                ${a.kind === 'character' ? '<button class="btn xs" data-appearance-open>角色出镜对照</button>' : ''}</div>` : ''}</div>
          <div class="card-body">
            ${cache.refsError ? `<div class="bar bad">引用清单读不到：${escapeHtml(cache.refsError)}
              ——这一格先别当成"没有引用"，更别照着它决定删不删</div>` : ''}
            <details class="ad-fold" open>
              <summary class="ad-fold-sum">${refs.length ? `共 ${refs.length} 条引用镜头明细` : '暂无镜头引用（在创作中心分镜表引用）'}</summary>
              <div class="ad-fold-body">
                ${refs.length ? refs.map((s) => {
                  const imp = impactMap.get(Number(s.shot_id));
                  const impBadge = !imp ? ''
                    : imp.stale ? '<span class="chip amber">源已变 · 建议重跑</span>'
                    : imp.job_id ? `<span class="chip green">${imp.signed ? '已签字仍可用' : '产物仍可用'}</span>`
                    : '<span class="chip gray">未生成</span>';
                  return `
                  <div class="ad-ref">
                    <div>
                      <div class="t-strong">${escapeHtml(s.project_name)}
                        <span class="t-dim">第 ${s.episode_seq} 集 · ${escapeHtml(s.episode_title)}</span> ${impBadge}</div>
                      <p class="t-dim">镜 ${s.seq} · ${escapeHtml(s.summary)}
                        ${s.variant_names && s.variant_names.length
                          ? `<br>用的形态：${escapeHtml(s.variant_names.join('、'))}` : ''}</p>
                    </div>
                    <button class="btn xs" data-goto-shot="${s.episode_id}">去分镜</button>
                  </div>`;
                }).join('')
                  : '<p class="t-dim">还没有镜头引用它。在创作中心的分镜表里选资产即可引用。</p>'}
              </div>
            </details>
          </div>
        </div>
      </div>`}

      <div class="modal" id="ad-del-modal" hidden>
        <div class="modal-box">
          <div class="modal-head">删除资产</div>
          <div class="modal-body">
            <p class="modal-warn">此操作不可撤销，资产本身与它登记过的参考图都会没掉。</p>
            <p>要删除的是「<b>${escapeHtml(a.name)}</b>」${a.ref_images.length
              ? `，含 ${a.ref_images.length} 张参考图` : ''}。</p>
            ${a.ref_shot_count ? `<p class="modal-warn">它正被 ${a.ref_shot_count} 条镜头引用，`
              + '删不掉——先去创作中心把这些镜头的引用去掉。这里不提供"强行删除"，'
              + '悬空的引用会在分镜表里显示成一个没有名字的编号。</p>' : ''}
          </div>
          <div class="modal-actions">
            <button class="btn" id="ad-del-cancel">取消</button>
            <button class="btn danger-solid" id="ad-del-ok"${a.ref_shot_count ? ' disabled' : ''}>
              确认删除</button>
          </div>
        </div>
      </div>
    </section>`;
}

export function mountDetail(root, id) {
  const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
  const back = () => closeDetail('assets');
  root.querySelectorAll('[data-back]').forEach((el) => el.addEventListener('click', back));

  if (!cache || cache.error) return;
  const isNew = Boolean(cache.isNew);
  const a = cache.asset;

  const applySection = (sec) => {
    activeSection = sec;
    root.querySelectorAll('[data-ad-tab]').forEach((btn) => {
      const on = btn.dataset.adTab === sec;
      btn.classList.toggle('on', on);
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    root.querySelectorAll('[data-ad-panel]').forEach((panel) => {
      panel.hidden = panel.dataset.adPanel !== sec;
    });
  };
  root.querySelectorAll('[data-ad-tab]').forEach((btn) => {
    btn.addEventListener('click', () => applySection(btn.dataset.adTab));
  });
  root.querySelectorAll('[data-switch-tab]').forEach((btn) => {
    btn.addEventListener('click', () => applySection(btn.dataset.switchTab));
  });
  applySection(activeSection);

  if (isNew) {
    root.querySelector('#ad-name')?.focus();
  } else {
    bindReferenceActions(root, a);
  }
  root.querySelectorAll('[data-appearance-open]').forEach((btn) => {
    btn.addEventListener('click', () => openCharacterAppearances(a, cache.variants || []));
  });
  root.querySelector('[data-reference-usage-open]')?.addEventListener('click', () => openReferenceUsage(a));

  root.querySelectorAll('[data-preview]').forEach((img) => {
    // 打开前先验一遍同源白名单：`dataset` 读回来的已经是**解转义后**的字符串，
    // 上面那层 escapeHtml 保护的是 HTML 结构，不是这里的跳转目标。
    // 只放行本站 /files/ 与 /api/——这两条是后端给的图片/产物路径；
    // 其余一律不跳（含 javascript:、data:、外站），免得一个被污染的路径变成
    // 「点图执行一段脚本」或把人带去钓鱼页。
    // noopener,noreferrer：新窗口不能通过 window.opener 反向操作本页，
    // 也不把本站地址放进 Referer 带给第三方。
    img.addEventListener('click', () => {
      const url = safeOpenUrl(img.dataset.preview);
      if (url) window.open(url, '_blank', 'noopener,noreferrer');
    });
  });

  // 跳创作中心并定位到那一集：只留一个"下一次进去看哪一集"的提示，
  // 不越界去改创作中心的状态
  // 档案与形态的编辑入口只在资产创作那一页：同一个字段不留两个编辑面
  root.querySelectorAll('[data-goto-studio]').forEach((btn) => {
    btn.addEventListener('click', () => {
      // 交棒而不是只切工作区：上一轮这里只把工作区切过去，创作页仍停在人
      // 上一次留下的草稿上，看起来像"点了没反应"。
      setStudioTarget(cache.asset.id);
      setSub('assets', 1);
      closeDetail('assets');   // 它自己就会重渲染，不再另发一次跳转事件
    });
  });

  root.querySelectorAll('[data-goto-shot]').forEach((btn) => {
    btn.addEventListener('click', () => {
      setParam('writing', Number(btn.dataset.gotoShot));
      window.dispatchEvent(new CustomEvent('app:go', { detail: 'writing' }));
    });
  });

  const buildAssetPayload = () => {
    const nameEl = root.querySelector('#ad-name');
    const name = nameEl.value.trim();
    if (!name) { nameEl.focus(); return null; }
    const globalEl = root.querySelector('#ad-global');
    return {
      kind: root.querySelector('#ad-kind').value,
      name,
      summary: root.querySelector('#ad-summary').value.trim(),
      consistency_prompt: root.querySelector('#ad-prompt').value.trim(),
      tags: split(root.querySelector('#ad-tags').value),
      aliases: split(root.querySelector('#ad-aliases').value),
      scope: globalEl?.checked ? 'global' : 'project',
    };
  };

  const createNewAsset = async (payload, btn) => {
    let created = null;
    const ok = await run('创建资产', async () => {
      created = await api.createAsset(store.currentId, payload);
      const known = new Set(created.ref_images || []);
      for (const item of pendingImages) {
        const updated = await api.uploadAssetImage(created.id, item.file);
        const added = (updated?.ref_images || []).find((p) => !known.has(p));
        if (added) {
          known.add(added);
          if (item.slotKey) {
            await api.setAssetImagePurpose(created.id, {
              path: added, variant_id: null, purpose: item.slotKey,
            });
          }
        }
      }
    }, { btn });
    if (!ok || !created) return null;
    pendingImages.forEach((p) => { if (p.url) URL.revokeObjectURL(p.url); });
    pendingImages = [];
    return created;
  };

  const saveBtn = root.querySelector('#ad-save');
  saveBtn?.addEventListener('click', async () => {
    const payload = buildAssetPayload();
    if (!payload) return;
    if (isNew) {
      const created = await createNewAsset(payload, saveBtn);
      if (created) openDetail('assets', created.id);
      return;
    }
    const ok = await run('保存', () => api.updateAsset(id, {
      ...payload,
      seen_version: a.version || '',
    }), { btn: saveBtn });
    if (ok) refresh();
  });

  const randomCard = root.querySelector('#ad-random-card');
  const toggleDrawBtn = root.querySelector('#ad-toggle-draw');
  const openNewCharacterDraw = () => {
    setNewAssetKind('character');
    openDetail('assets', 'new');
  };
  root.querySelector('#ad-new-draw')?.addEventListener('click', openNewCharacterDraw);
  root.querySelector('#ad-draw-another')?.addEventListener('click', openNewCharacterDraw);
  root.querySelectorAll('[data-new-draw]').forEach((btn) => {
    btn.addEventListener('click', openNewCharacterDraw);
  });
  toggleDrawBtn?.addEventListener('click', () => {
    if (!randomCard) return;
    randomCard.hidden = !randomCard.hidden;
    keepDrawCardAssetId = randomCard.hidden ? null : a.id;
    toggleDrawBtn.textContent = randomCard.hidden ? '🎲 抽卡 / 重生设定集' : '收起抽卡面板';
  });
  root.querySelectorAll('[data-goto-draw]').forEach((btn) => {
    btn.addEventListener('click', () => {
      keepDrawCardAssetId = a.id;
      activeSection = 'basic';
      root.querySelectorAll('.ad-sec-tab').forEach((t) => {
        const on = t.dataset.adTab === 'basic';
        t.classList.toggle('on', on);
        t.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      root.querySelectorAll('.ad-panel').forEach((p) => {
        p.hidden = p.dataset.adPanel !== 'basic';
      });
      if (randomCard) {
        randomCard.hidden = false;
        randomCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
      if (toggleDrawBtn) toggleDrawBtn.textContent = '收起抽卡面板';
    });
  });
  const kindSel = root.querySelector('#ad-kind');
  if (isNew && randomCard && kindSel) {
    kindSel.addEventListener('change', () => {
      randomCard.hidden = kindSel.value !== 'character';
    });
  }

  const randBtn = root.querySelector('#ad-rand-btn');
  const randCancelBtn = root.querySelector('#ad-rand-cancel');
  const randThemeInput = root.querySelector('#ad-rand-theme');
  const randStatus = root.querySelector('#ad-rand-status');
  let randAbort = null;
  let randCanceled = false;
  let randImagePromptEn = '';
  let randPromptZhSnapshot = '';
  randCancelBtn?.addEventListener('click', () => {
    if (!randAbort) return;
    randCanceled = true;
    randAbort.abort();
  });
  randThemeInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing && randBtn && !randBtn.disabled) {
      e.preventDefault();
      randBtn.click();
    }
  });
  randBtn?.addEventListener('click', async () => {
    const modelSel = root.querySelector('#ad-rand-model') || root.querySelector('#ad-ai-model');
    const picked = modelSel?.value ? modelSel.value.split('|') : null;
    const mode = picked && picked.length === 3 ? picked[0] : 'auto';
    const providerCode = picked && picked.length === 3 ? picked[1] : null;
    const modelCode = picked && picked.length === 3 ? picked[2] : null;
    if (mode === 'cloud') {
      const go = await confirmDanger({
        title: '使用云端文本模型生成随机角色',
        danger: false,
        okText: '确认生成',
        body: `<p>当前将使用云端文本模型 <b>${escapeHtml(providerCode)} / ${escapeHtml(modelCode)}</b> 随机生成角色人设，可能产生少量云端调用费用。</p>`,
      });
      if (!go || !randBtn.isConnected) return;
    }
    const modelLabel = providerCode && modelCode ? `${providerCode} / ${modelCode}` : '默认文本模型';
    const startedAt = Date.now();
    const ctl = new AbortController();
    randAbort = ctl;
    randCanceled = false;
    let timedOut = false;
    const timeoutId = setTimeout(() => {
      timedOut = true;
      ctl.abort();
    }, 120_000);
    const updateWaiting = () => {
      if (!randStatus || !randBtn.isConnected) return;
      const sec = Math.max(1, Math.floor((Date.now() - startedAt) / 1000));
      const hint = mode === 'local' || mode === 'auto' ? '，本地模型首次加载可能较慢' : '';
      randStatus.textContent = `正在通过 ${modelLabel} 构思角色设定…（已等待 ${sec}s${hint}）`;
    };
    let tickId = 0;
    const scheduleTick = () => {
      tickId = setTimeout(() => {
        if (!randBtn.isConnected) {
          ctl.abort();
          return;
        }
        if (!document.hidden) updateWaiting();
        scheduleTick();
      }, 1000);
    };
    scheduleTick();
    randBtn.disabled = true;
    if (randCancelBtn) randCancelBtn.hidden = false;
    if (randStatus) randStatus.textContent = `正在通过 ${modelLabel} 构思角色设定…`;
    try {
      const theme = randThemeInput?.value.trim() || '';
      const out = await api.assetRandomCharacter(store.currentId, {
        theme, execution_mode: mode, provider_code: providerCode, model_code: modelCode,
      }, { signal: ctl.signal });
      if (!randBtn.isConnected) return;
      root.querySelector('#ad-name').value = out.name || '';
      root.querySelector('#ad-summary').value = out.summary || '';
      root.querySelector('#ad-tags').value = (out.tags || []).join(', ');
      root.querySelector('#ad-aliases').value = (out.aliases || []).join(', ');
      root.querySelector('#ad-prompt').value = out.consistency_prompt || '';
      randImagePromptEn = (out.image_prompt_en || '').trim();
      randPromptZhSnapshot = (out.consistency_prompt || '').trim();
      randBtn.textContent = '🎲 换一个人设';
      const elapsed = ((Date.now() - startedAt) / 1000).toFixed(1);
      const startedHint = out.ollama_auto_started ? '已自动拉起本地 Ollama 并' : '已';
      if (randStatus) {
        randStatus.textContent = `${startedHint}通过 ${out.provider} / ${out.model} 生成「${out.name}」（耗时 ${elapsed}s），可继续微调或重抽，满意后点「保存并生成三视图」`;
      }
    } catch (err) {
      if (!randStatus || !randBtn.isConnected) return;
      if (randCanceled) {
        randStatus.textContent = '已取消本次随机生成';
      } else if (timedOut) {
        randStatus.textContent = '随机生成超时（120 秒未返回），请确认文本模型运行正常，或切换更快的模型后重试';
      } else {
        randStatus.textContent = `随机生成失败：${err.message || err}`;
      }
    } finally {
      clearTimeout(timeoutId);
      clearTimeout(tickId);
      if (randAbort === ctl) randAbort = null;
      if (randBtn.isConnected) randBtn.disabled = false;
      if (randCancelBtn?.isConnected) randCancelBtn.hidden = true;
    }
  });

  const copyToClipboard = async (text) => {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  };

  root.querySelector('[data-copy-expr-prompt]')?.addEventListener('click', async () => {
    const name = root.querySelector('#ad-name')?.value.trim() || a.name || '未命名角色';
    const summary = root.querySelector('#ad-summary')?.value.trim() || a.summary || '';
    const prompt = root.querySelector('#ad-prompt')?.value.trim() || a.consistency_prompt || '';
    const core = prompt || summary;
    if (!core) {
      window.dispatchEvent(new CustomEvent('app:toast', {
        detail: { text: '请先填写角色简介或一致性提示词后再复制', kind: 'err' },
      }));
      return;
    }
    const exprPrompt = [
      `请参考我附上的角色「面部特写 / 封面定妆」图片及以下设定，为我**分别生成 6 张独立的高清角色表情差分特写图**（不要拼成九宫格，也不要在画面上添加任何文字、标签或水印）：`,
      '',
      `【角色名称】${name}`,
      summary ? `【角色简介】${summary}` : '',
      `【核心外形一致性设定】${core}`,
      '',
      `【统一画面规格】全部为正方 1:1 宽高比（高分辨率 ≥1024×1024 像素），胸部以上正面/微侧特写，干净纯白背景，严格保持参考图中的画风、脸型五官、瞳色、发型发色、耳饰发卡与领口服装 100% 不变，仅变化面部神态与微手势：`,
      `1. 第 1 张【自然微笑】：温柔元气的自然微笑，眼神明亮放松。`,
      `2. 第 2 张【平静】：日常放松的平静神态，嘴角自然微抿。`,
      `3. 第 3 张【惊讶】：睁大双眼、微微张嘴的惊讶神态。`,
      `4. 第 4 张【害羞】：脸颊微红、眼神略微闪躲的可爱害羞神态。`,
      `5. 第 5 张【认真】：眉眼微蹙、目光专注坚定的认真/战斗神态。`,
      `6. 第 6 张【委屈】：微微嘟嘴、眼眶湿润略带委屈的撒娇神态。`,
    ].filter((line) => line !== '').join('\n');
    try {
      await copyToClipboard(exprPrompt);
      window.dispatchEvent(new CustomEvent('app:toast', {
        detail: { text: '已复制主角 6 表情差分提示词（建议附上面部特写图发给 GPT，再导入补充参考图）', kind: 'ok' },
      }));
    } catch (err) {
      window.dispatchEvent(new CustomEvent('app:toast', {
        detail: { text: `复制失败：${err.message || err}`, kind: 'err' },
      }));
    }
  });

  const saveTvBtn = root.querySelector('#ad-save-tv');
  const copyGptBtn = root.querySelector('#ad-copy-gpt');
  const copyQuadSavedBtn = root.querySelector('[data-copy-quad-prompt]');
  const tvModeSel = root.querySelector('#ad-tv-mode');
  const handleCopyQuadPrompt = async () => {
    const name = root.querySelector('#ad-name')?.value.trim() || a.name || '未命名角色';
    const summary = root.querySelector('#ad-summary')?.value.trim() || a.summary || '';
    const prompt = root.querySelector('#ad-prompt')?.value.trim() || a.consistency_prompt || '';
    const core = prompt || summary;
    if (!core) {
      if (randStatus) randStatus.textContent = '请先随机生成人设或填写一致性提示词后再复制';
      else {
        window.dispatchEvent(new CustomEvent('app:toast', {
          detail: { text: '请先在「基本信息」填写一致性提示词后再复制', kind: 'warn' },
        }));
      }
      root.querySelector('#ad-prompt')?.focus();
      return;
    }
    const gptPrompt = [
      `请根据以下角色设定，为我**分别生成 4 张独立的高清角色设定图**（适用于 ChatGPT GPT Image / Google Nano Banana，不要拼在一张图里，也不要在画面上添加任何文字、标签、水印或标尺）：`,
      '',
      `【角色名称】${name}`,
      summary ? `【角色简介】${summary}` : '',
      `【核心外形一致性设定】${core}`,
      '',
      `请严格按以下 4 个画面规格与比例逐张生成并输出独立图片，并确保 4 张图中的画风质感、脸型五官、瞳色、发型发色、服装配色材质与配饰 100% 一致：`,
      `1. 第 1 张【面部特写（封面）】（正方 1:1 宽高比，高分辨率 ≥1024×1024 像素）：胸部以上面部特写，清晰展现五官、眼神光、发饰与表情细节，干净纯白背景，无文字。`,
      `2. 第 2 张【正面定妆照】（竖版 3:4 宽高比，高分辨率 ≥1024×1365 像素）：同一角色正面全身站姿定妆照，面向镜头，干净纯白背景，无文字。`,
      `3. 第 3 张【三视图（正/侧/背）】（宽屏横版 16:9 宽高比，高分辨率 ≥1536×864 像素）：同一角色的正面、侧面、背面全身三视图，水平等高并排对齐，干净纯白背景，无文字。`,
      `4. 第 4 张【背面高清图】（竖版 3:4 宽高比，高分辨率 ≥1024×1365 像素）：同一角色的背面全身图，清晰展现背部发型层次、服装剪裁与随身配饰，干净纯白背景，无文字。`,
    ].filter((line) => line !== '').join('\n');
    try {
      await copyToClipboard(gptPrompt);
      if (randStatus) randStatus.textContent = '已复制 4 图设定集提示词，可贴给 ChatGPT / Nano Banana 逐张出图并导入 4 个槽位';
      window.dispatchEvent(new CustomEvent('app:toast', {
        detail: { text: '已复制 4 图设定提示词（适配 ChatGPT / Nano Banana）', kind: 'ok' },
      }));
    } catch (err) {
      if (randStatus) randStatus.textContent = `复制失败：${err.message || err}`;
      else {
        window.dispatchEvent(new CustomEvent('app:toast', {
          detail: { text: `复制失败：${err.message || err}`, kind: 'err' },
        }));
      }
    }
  };
  copyGptBtn?.addEventListener('click', handleCopyQuadPrompt);
  copyQuadSavedBtn?.addEventListener('click', handleCopyQuadPrompt);
  const tvModelSel = root.querySelector('#ad-tv-model');
  attachEngineFilter(root.querySelector('#ad-rand-model'), cache.textOptions || [], {
    storageKey: 'asset_detail_text',
    healthLabel: HEALTH_LABEL,
    engineAriaLabel: '人设文本引擎',
  });
  attachEngineFilter(tvModelSel, cache.imageOptions || [], {
    storageKey: 'asset_detail_image',
    healthLabel: HEALTH_LABEL,
    engineAriaLabel: '生图引擎',
  });
  attachEngineFilter(root.querySelector('#ad-ai-model'), cache.textOptions || [], {
    storageKey: 'asset_detail_ai_text',
    healthLabel: HEALTH_LABEL,
    engineAriaLabel: '扩写文本引擎',
    wrapClass: 'emp-pair-grow',
  });
  const tvTplWrap = root.querySelector('#ad-tv-tpl-wrap');
  const tvTplSel = root.querySelector('#ad-tv-template');
  const tvInstWrap = root.querySelector('#ad-tv-inst-wrap');
  const tvInstSel = root.querySelector('#ad-tv-instance');
  const tvCkptWrap = root.querySelector('#ad-tv-ckpt-wrap');
  const tvCkptSel = root.querySelector('#ad-tv-checkpoint');
  let cachedImgProfiles = [];
  const syncTvTemplates = async () => {
    if (!tvTplWrap || !tvTplSel || !tvModelSel) return;
    const picked = tvModelSel.value ? tvModelSel.value.split('|') : [];
    const provider = picked[1] || '';
    if (!provider || !store.currentId) { tvTplWrap.hidden = true; return; }
    try {
      const list = await api.generationProfiles(store.currentId);
      if (!tvTplSel.isConnected) return;
      cachedImgProfiles = (Array.isArray(list) ? list : []).filter(
        (p) => p.capability === 'image' && (!p.provider_code || p.provider_code === provider),
      );
      tvTplWrap.hidden = cachedImgProfiles.length === 0;
      tvTplSel.innerHTML = '<option value="">默认参数（不套模板）</option>'
        + cachedImgProfiles.map((p) => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('');
    } catch {
      tvTplWrap.hidden = true;
    }
  };
  const syncComfyCheckpointsOnly = async () => {
    if (!tvCkptSel) return;
    const instId = tvInstSel?.value ? Number(tvInstSel.value) : null;
    const data = await loadComfyCheckpoints({ instanceId: instId });
    if (!tvCkptSel.isConnected) return;
    tvCkptSel.innerHTML = checkpointOptionsMarkup(data, rememberedCheckpoint());
    tvCkptSel.title = data.ok
      ? `节点：${data.display_name || data.endpoint}`
      : (data.error || '未能拉取 ComfyUI 模型列表');
  };
  const syncComfyCheckpoint = async () => {
    if (!tvModelSel || !tvCkptWrap || !tvCkptSel) return;
    const isComfy = tvModelSel.value === 'local|comfyui|comfyui-workflow';
    tvCkptWrap.hidden = !isComfy;
    if (tvInstWrap) tvInstWrap.hidden = !isComfy;
    void syncTvTemplates();
    if (!isComfy) return;
    if (tvInstSel) {
      const instances = await loadComfyInstances();
      if (!tvInstSel.isConnected) return;
      tvInstSel.innerHTML = instanceOptionsMarkup(instances, rememberedInstanceId());
      if (tvInstWrap) tvInstWrap.hidden = instances.length <= 1;
    }
    await syncComfyCheckpointsOnly();
  };
  tvModelSel?.addEventListener('change', () => { void syncComfyCheckpoint(); });
  tvInstSel?.addEventListener('change', () => {
    rememberInstanceId(tvInstSel.value);
    void syncComfyCheckpointsOnly();
  });
  tvCkptSel?.addEventListener('change', () => { rememberCheckpoint(tvCkptSel.value); });
  void syncComfyCheckpoint();
  tvModeSel?.addEventListener('change', () => {
    if (!saveTvBtn) return;
    if (tvModeSel.value === 'single') {
      saveTvBtn.textContent = isNew ? '🎨 保存并生成三视图' : '🎨 保存并重新生成三视图';
    } else {
      saveTvBtn.textContent = isNew ? '🎨 保存并生成设定集' : '🎨 保存并重新生成设定集';
    }
  });
  saveTvBtn?.addEventListener('click', async () => {
    const payload = buildAssetPayload();
    if (!payload) return;
    const tvSel = root.querySelector('#ad-tv-model');
    const picked = tvSel?.value ? tvSel.value.split('|') : null;
    if (!picked || picked.length !== 3) {
      if (randStatus) randStatus.textContent = '暂无可用生图模型，请先到「模型中心」配置图像通道';
      return;
    }
    const [execMode, providerCode, modelCode] = picked;
    const isQuad = (tvModeSel?.value || 'quad') === 'quad';
    const jobCount = isQuad ? 4 : 1;
    if (execMode === 'cloud') {
      const go = await confirmDanger({
        title: isNew
          ? (isQuad ? '创建角色并提交云端 4 格设定集生成' : '创建角色并提交云端三视图生成')
          : (isQuad ? '更新角色并重新提交云端 4 格设定集生成' : '更新角色并重新提交云端三视图生成'),
        danger: false,
        okText: isNew ? '确认创建并生图' : '确认保存并生图',
        body: `<p>将先${isNew ? '创建' : '保存'}角色「<b>${escapeHtml(payload.name)}</b>」，并提交 <b>${jobCount} 条</b>生图任务到云端模型 <b>${escapeHtml(providerCode)} / ${escapeHtml(modelCode)}</b>（按模型中心口径计费）。</p>`,
      });
      if (!go || !saveTvBtn.isConnected) return;
    }
    saveTvBtn.disabled = true;
    if (saveBtn) saveBtn.disabled = true;
    if (randStatus) randStatus.textContent = `正在${isNew ? '创建' : '保存'}角色并提交 ${jobCount} 条生图任务…`;
    let targetAsset = null;
    if (isNew) {
      targetAsset = await createNewAsset(payload, saveTvBtn);
    } else {
      const updated = await run('保存', () => api.updateAsset(a.id, {
        ...payload,
        seen_version: a.version || '',
      }), { btn: saveTvBtn });
      if (updated) targetAsset = typeof updated === 'object' && updated.id ? updated : { id: a.id };
    }
    if (!targetAsset) {
      if (saveTvBtn.isConnected) saveTvBtn.disabled = false;
      if (saveBtn?.isConnected) saveBtn.disabled = false;
      return;
    }
    const created = targetAsset;
    keepDrawCardAssetId = created.id;
    const coreDesc = payload.consistency_prompt || payload.summary || payload.name;
    const isLocalImg = execMode === 'local';
    const hasFreshEn = Boolean(randImagePromptEn && payload.consistency_prompt.trim() === randPromptZhSnapshot);
    const enCore = hasFreshEn ? randImagePromptEn : coreDesc;
    const isMflux = providerCode === 'mflux';

    const buildSlotPrompt = (enPrefix, zhPrefix, sourceText = null) => {
      const enText = sourceText ?? enCore;
      const rawText = sourceText ?? coreDesc;
      if (isLocalImg && hasFreshEn) return `${enPrefix}, ${enText}`.slice(0, 1000);
      if (isLocalImg) return `${enPrefix}, ${rawText}`.slice(0, 1000);
      return `${zhPrefix}${rawText}`.slice(0, 1000);
    };
    const stripFacialTraits = (text) => String(text || '')
      .split(/[,，]/)
      .map((s) => s.trim())
      .filter((s) => s && !/\b(face|eyes?|brows?|eyebrows?|nose|mouth|smile|smiling|expression|looking at viewer|gaze|pupils?|lips)\b/i.test(s)
        && !/(五官|眼神|微笑|表情|面向镜头|眉毛|眼睛|鼻|嘴)/.test(s))
      .join(', ');
    const backEnCore = stripFacialTraits(enCore) || enCore;
    const backCoreDesc = stripFacialTraits(coreDesc) || coreDesc;
    const negCommon = 'lowres, bad anatomy, bad hands, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark, blurry, text';
    const negSingle = `${negCommon}, multiple views, character sheet, grid, collage, comic, small portraits, insets, border, split screen`;
    const negThreeView = `${negCommon}, 4 views, 5 views, 6 views, 4koma, comic, crowded, overlapping, floating heads, insets, close-up bubble`;
    const negBackOnly = `${negSingle}, face, eyes, nose, mouth, smile, looking at viewer, looking back, over shoulder, profile, side view, portrait`;

    const tvStyle = root.querySelector('#ad-tv-style')?.value || store.current?.art_style || '2d';
    const isNon2D = tvStyle === '25d' || tvStyle === '3d';
    const strip2DStyle = (text) => String(text || '')
      .split(/[,，]/)
      .map((s) => s.trim())
      .filter((s) => s && !/\b(cel[- ]?shad(?:ed|ing)|flat colors?|lineart|clean crisp lineart|2d|anime style)\b/i.test(s)
        && !/(赛璐璐|平涂|线稿)/.test(s))
      .join(', ');
    const styleEn = tvStyle === '3d'
      ? '3D stylized anime character render, Unreal Engine 5, Octane render, PBR materials, subsurface scattering, volumetric lighting, soft 3D shading, no lineart'
      : (tvStyle === '25d'
        ? '2.5D semi-realistic thick-paint anime illustration, rich brushwork, soft dimensional shading, volumetric lighting, no crisp lineart'
        : 'masterpiece, best quality, 2D anime cel-shaded illustration, consistent anime character design');
    const styleZh = tvStyle === '3d'
      ? '3D立体国漫CG渲染，无线稿，次表面散射与体积光影，'
      : (tvStyle === '25d' ? '2.5D半写实厚涂插画，立体明暗过渡，' : '二次元日漫赛璐璐风格，');
    const anchorEnCore = isNon2D ? (strip2DStyle(enCore) || enCore) : enCore;
    const anchorCoreDesc = isNon2D ? (strip2DStyle(coreDesc) || coreDesc) : coreDesc;
    const anchorSource = isLocalImg && hasFreshEn ? anchorEnCore : anchorCoreDesc;
    const specs = isQuad ? [
      {
        purpose: '封面定妆',
        label: '封面定妆',
        prompt: buildSlotPrompt(
          `${styleEn}, solo, full body, front view, straight-on, standing pose, looking at viewer, simple white background, clean background`,
          `${styleZh}角色正面全身定妆照（封面基准），单人居中，站姿清晰，面向镜头，干净纯白背景，保持五官发型服装配色完全一致：`,
          anchorSource,
        ),
        params: isMflux
          ? { width: 576, height: 768 }
          : { width: 768, height: 1024, ...(providerCode === 'comfyui' ? { negative_prompt: negSingle } : {}) },
      },
      {
        purpose: '三视图',
        label: '三视图',
        prompt: buildSlotPrompt(
          `${styleEn}, character turnaround sheet, multiple views, 3views, front view, side view, back view, three full body views side by side, standing pose, simple white background, clean background`,
          `${styleZh}角色三视图设定稿（严格仅正面、侧面、背面三个全身像并排），站姿清晰，干净纯白背景，保持五官发型服装配色完全一致：`,
        ),
        params: isMflux
          ? { width: 896, height: 512 }
          : { width: 1344, height: 768, ...(providerCode === 'comfyui' ? { negative_prompt: negThreeView } : {}) },
      },
      {
        purpose: '面部特写',
        label: '面部特写',
        prompt: buildSlotPrompt(
          `${styleEn}, solo, close-up portrait, upper body, facing viewer, detailed face and eyes, simple white background, clean background`,
          `${styleZh}角色面部与半身特写，单人居中，清晰展现五官、眼神、发饰与表情细节，干净纯白背景，保持设定完全一致：`,
        ),
        params: isMflux
          ? { width: 512, height: 512 }
          : { width: 768, height: 768, ...(providerCode === 'comfyui' ? { negative_prompt: `${negSingle}, full body` } : {}) },
      },
      {
        purpose: '背面细节',
        label: '背面细节',
        prompt: buildSlotPrompt(
          `${styleEn}, (from behind:1.4), (back view:1.4), solo, full body, facing away from viewer, looking away, back of head, showing back of hair and outfit, standing pose, simple white background, clean background`,
          `${styleZh}角色纯背面全身设定图，完全背对镜头不露脸，清晰展现后脑勺发型、背部服装剪裁与随身配饰细节，干净纯白背景：`,
          isLocalImg && hasFreshEn ? backEnCore : backCoreDesc,
        ),
        params: isMflux
          ? { width: 576, height: 768 }
          : { width: 768, height: 1024, ...(providerCode === 'comfyui' ? { negative_prompt: negBackOnly } : {}) },
      },
    ] : [
      {
        purpose: '三视图',
        label: '三视图',
        prompt: buildSlotPrompt(
          `${styleEn}, character turnaround sheet, multiple views, 3views, front view, side view, back view, three full body views side by side, standing pose, simple white background, clean background`,
          `${styleZh}角色三视图设定稿（严格仅正面、侧面、背面三个全身像并排），站姿清晰，干净纯白背景，保持五官发型服装配色完全一致：`,
        ),
        params: isMflux
          ? { width: 896, height: 512 }
          : { width: 1344, height: 768, ...(providerCode === 'comfyui' ? { negative_prompt: negThreeView } : {}) },
      },
    ];

    const chosenImgOpt = (cache.imageOptions || []).find(
      (o) => o.backend === execMode && o.provider === providerCode && o.model === modelCode,
    );
    const firstPrice = execMode === 'cloud' && providerCode !== 'mock'
      ? (chosenImgOpt?.prices || [])[0] || null
      : null;
    const estimate = firstPrice ? {
      quantity: 1,
      unit: firstPrice.unit,
      unit_price_micros: firstPrice.amount_micros,
      currency: firstPrice.currency,
      source: firstPrice.source,
    } : null;

    const batchStamp = Date.now();
    const chosenTpl = tvTplSel?.value
      ? cachedImgProfiles.find((p) => String(p.id) === tvTplSel.value)
      : null;
    const tplParams = (chosenTpl?.params && typeof chosenTpl.params === 'object') ? chosenTpl.params : {};
    const chosenCheckpoint = (providerCode === 'comfyui' && tvCkptSel && !tvCkptWrap?.hidden)
      ? tvCkptSel.value.trim()
      : '';
    const chosenInstanceId = (providerCode === 'comfyui' && tvInstSel?.value)
      ? Number(tvInstSel.value)
      : null;
    const isKontext = providerCode === 'comfyui'
      && /flux/i.test(chosenCheckpoint)
      && /kontext/i.test(chosenCheckpoint);
    const submitSpecs = isKontext ? [specs[0]] : specs;
    const followupSpecs = isKontext ? specs.slice(1).map((sp) => ({
      purpose: sp.purpose,
      label: sp.label,
      prompt: sp.purpose === '面部特写'
        ? 'Keep the exact same character, face, hair, eye accessory, and outfit from the reference image; generate a close-up upper body portrait facing viewer on a clean solid white background'
        : (sp.purpose === '背面细节'
          ? 'Keep the exact same character, hair color, outfit, and accessories from the reference image; show her full body strictly from behind, facing away from viewer, back of head visible, no face visible, clean solid white background'
          : 'Keep the exact same character, outfit, hair, and accessories from the reference image; generate a clean character turnaround sheet with exactly 3 full-body views side by side (front view, side profile view, back view) on a clean solid white background'),
      params: { ...tplParams, ...sp.params, ...(chosenCheckpoint ? { checkpoint: chosenCheckpoint } : {}) },
      execution_mode: execMode,
      provider_code: providerCode,
      model_code: modelCode,
      ...(chosenInstanceId ? { engine_instance_id: chosenInstanceId } : {}),
      estimate,
    })) : [];
    const submittedList = [];
    let firstErr = null;
    for (let idx = 0; idx < submitSpecs.length; idx += 1) {
      const spec = submitSpecs[idx];
      const submitParams = {
        ...tplParams,
        ...spec.params,
        ...(chosenCheckpoint ? { checkpoint: chosenCheckpoint } : {}),
      };
      try {
        const submitted = await api.assetGenerationSubmit(store.currentId, {
          operation: 'asset_image',
          target_asset_id: created.id,
          image_prompt: spec.prompt,
          execution_mode: execMode,
          provider_code: providerCode,
          model_code: modelCode,
          ...(chosenInstanceId ? { engine_instance_id: chosenInstanceId } : {}),
          params: submitParams,
          estimate,
          user_confirmed_cloud: execMode === 'cloud',
          idempotency_key: `ad-tv-${created.id}-${idx}-${batchStamp}`,
        });
        submittedList.push({
          assetId: created.id,
          jobId: submitted.job_id,
          purpose: spec.purpose,
          label: spec.label,
          status: submitted.status || 'queued',
          imported: false,
          importError: '',
          error: '',
          ...(idx === 0 && followupSpecs.length ? { followupSpecs } : {}),
        });
      } catch (err) {
        if (!firstErr) firstErr = err;
      }
    }

    if (submittedList.length) {
      saveTurnaroundPending([...pendingTurnaroundJobs.filter((j) => j.assetId !== created.id), ...submittedList]);
      pendingTurnaroundNotice = null;
      const actionVerb = isNew ? '创建' : '更新';
      const msg = isKontext
        ? `已${actionVerb}角色「${payload.name}」，正在生成第 1 张正面定妆照，完成后将自动生成其余 3 张`
        : (submittedList.length === submitSpecs.length
          ? `已${actionVerb}角色「${payload.name}」并排队 ${submittedList.length} 张设定图，完成后将自动填入对应槽位`
          : `已${actionVerb}角色「${payload.name}」，已排队 ${submittedList.length}/${submitSpecs.length} 张（部分失败：${firstErr?.message || firstErr}）`);
      window.dispatchEvent(new CustomEvent('app:toast', {
        detail: { text: msg, kind: submittedList.length === submitSpecs.length ? 'ok' : 'err' },
      }));
    } else {
      saveTurnaroundPending(pendingTurnaroundJobs.filter((j) => j.assetId !== created.id));
      const errMsg = `角色「${payload.name}」已保存，但设定图任务提交失败：${firstErr?.message || firstErr || '未知错误'}`;
      pendingTurnaroundNotice = { assetId: created.id, kind: 'bad', text: errMsg };
      window.dispatchEvent(new CustomEvent('app:toast', {
        detail: { text: errMsg, kind: 'err' },
      }));
    }
    openDetail('assets', created.id);
  });

  const activeWatchJobs = !isNew && store.currentId
    ? pendingTurnaroundJobs.filter((j) => j.assetId === a.id && !j.imported)
    : [];
  if (activeWatchJobs.length) {
    const watchBar = root.querySelector('#ad-tv-watch');
    const batchIds = new Set(activeWatchJobs.map((j) => j.jobId));
    const tryImportSlot = async (item) => {
      const imported = await api.assetImportImage(a.id, { job_id: item.jobId });
      if (!imported?.path) throw new Error('未返回导入路径');
      await api.setAssetImagePurpose(a.id, {
        path: imported.path, variant_id: null, purpose: item.purpose,
      });
      if (item.purpose === '面部特写') {
        await api.setAssetCover(a.id, imported.path).catch(() => {});
      }
      item.imported = true;
      item.importError = '';
      if (Array.isArray(item.followupSpecs) && item.followupSpecs.length) {
        const followups = item.followupSpecs;
        delete item.followupSpecs;
        const stamp = Date.now();
        for (let fi = 0; fi < followups.length; fi += 1) {
          const fu = followups[fi];
          try {
            const sub = await api.assetGenerationSubmit(store.currentId, {
              operation: 'asset_image',
              target_asset_id: a.id,
              image_prompt: fu.prompt,
              execution_mode: fu.execution_mode,
              provider_code: fu.provider_code,
              model_code: fu.model_code,
              ...(fu.engine_instance_id ? { engine_instance_id: fu.engine_instance_id } : {}),
              params: { ...fu.params, image: imported.path },
              estimate: fu.estimate || null,
              user_confirmed_cloud: fu.execution_mode === 'cloud',
              idempotency_key: `ad-tv-fu-${a.id}-${fi}-${stamp}`,
            });
            pendingTurnaroundJobs.push({
              assetId: a.id,
              jobId: sub.job_id,
              purpose: fu.purpose,
              label: fu.label,
              status: sub.status || 'queued',
              imported: false,
              importError: '',
              error: '',
            });
          } catch { /* ignore individual followup submit error */ }
        }
      }
      saveTurnaroundPending(pendingTurnaroundJobs);
    };
    const renderWatchBar = () => {
      if (!watchBar?.isConnected) return;
      const doneCount = activeWatchJobs.filter((j) => j.status === 'succeeded' && j.imported).length;
      const failCount = activeWatchJobs.filter((j) => j.status === 'failed' || j.status === 'cancelled').length;
      const retryable = activeWatchJobs.filter((j) => j.status === 'succeeded' && !j.imported && j.importError);
      const summaryText = activeWatchJobs.map((j) => {
        const tag = j.status === 'succeeded'
          ? (j.imported ? '✓ 已入槽' : (j.importError ? '⚠ 入槽失败' : '待入槽'))
          : (j.status === 'failed' || j.status === 'cancelled' ? '✗' : '生成中');
        return `${j.label} ${tag}`;
      }).join(' · ');
      if (failCount === activeWatchJobs.length) {
        watchBar.className = 'bar bad ad-tv-watch';
        watchBar.textContent = `设定图生成未完成：${activeWatchJobs[0].error || '请前往生成中心查看'}`;
        return;
      }
      watchBar.className = retryable.length ? 'bar bad ad-tv-watch' : 'bar info ad-tv-watch';
      const retryBtnHtml = retryable.length
        ? ' <button type="button" class="btn xs" id="ad-tv-retry-import">重试补入槽</button>'
        : '';
      watchBar.innerHTML = `🎨 角色设定图进度（已入槽 ${doneCount}/${activeWatchJobs.length}）：${escapeHtml(summaryText)}${retryBtnHtml} <a href="#generate/0">去生成中心查看</a>`;
      watchBar.querySelector('#ad-tv-retry-import')?.addEventListener('click', async (ev) => {
        const btn = ev.currentTarget;
        btn.disabled = true;
        for (const item of retryable) {
          try { await tryImportSlot(item); } catch (err) { item.importError = err?.message || '入槽失败'; }
        }
        saveTurnaroundPending(pendingTurnaroundJobs);
        renderWatchBar();
        if (activeWatchJobs.every((j) => j.imported || j.status === 'failed' || j.status === 'cancelled')) {
          saveTurnaroundPending(pendingTurnaroundJobs.filter((j) => !batchIds.has(j.jobId)));
          refresh();
        }
      });
    };
    renderWatchBar();
    (async () => {
      for (let i = 0; i < 180; i += 1) {
        if (!root.isConnected || !pendingTurnaroundJobs.some((j) => batchIds.has(j.jobId))) return;
        let importedAny = false;
        let changed = false;
        for (const item of activeWatchJobs) {
          if (item.imported || item.status === 'failed' || item.status === 'cancelled') continue;
          try {
            if (item.status !== 'succeeded') {
              const gen = await api.assetGeneration(store.currentId, item.jobId);
              if (!root.isConnected || !pendingTurnaroundJobs.some((j) => batchIds.has(j.jobId))) return;
              if (item.status !== gen.status) { item.status = gen.status; changed = true; }
              if (gen.status === 'failed' || gen.status === 'cancelled') {
                item.error = gen.error || publicStatus(gen.status);
                changed = true;
                continue;
              }
            }
            if (item.status === 'succeeded' && !item.imported) {
              try {
                await tryImportSlot(item);
                importedAny = true;
                changed = true;
              } catch (err) {
                item.importError = err?.message || '入槽失败';
                changed = true;
              }
            }
          } catch {
            // 网络抖动时继续下一轮
          }
        }
        if (changed) saveTurnaroundPending(pendingTurnaroundJobs);
        renderWatchBar();
        const allSettled = activeWatchJobs.every((j) => j.imported || j.status === 'failed' || j.status === 'cancelled');
        if (allSettled) {
          saveTurnaroundPending(pendingTurnaroundJobs.filter((j) => !batchIds.has(j.jobId)));
          refresh();
          return;
        }
        if (importedAny) {
          refresh();
          return;
        }
        await new Promise((r) => setTimeout(r, 2500));
      }
      saveTurnaroundPending(pendingTurnaroundJobs.filter((j) => !batchIds.has(j.jobId)));
    })();
  }

  const aiBtn = root.querySelector('#ad-ai-refine');
  const aiStatus = root.querySelector('#ad-ai-status');
  const aiUndo = root.querySelector('#ad-ai-undo');
  let prevPromptBeforeAi = null;
  aiUndo?.addEventListener('click', () => {
    const promptEl = root.querySelector('#ad-prompt');
    if (promptEl && prevPromptBeforeAi !== null) {
      promptEl.value = prevPromptBeforeAi;
      promptEl.focus();
      prevPromptBeforeAi = null;
      aiUndo.hidden = true;
      if (aiStatus) aiStatus.textContent = '已还原为润色前的关键词';
    }
  });
  aiBtn?.addEventListener('click', async () => {
    const promptEl = root.querySelector('#ad-prompt');
    const name = root.querySelector('#ad-name')?.value.trim() || '';
    const summary = root.querySelector('#ad-summary')?.value.trim() || '';
    const draftPrompt = promptEl?.value.trim() || '';
    if (!name && !summary && !draftPrompt) {
      if (aiStatus) aiStatus.textContent = '请先填写名称或几个关键词再试';
      promptEl?.focus();
      return;
    }
    aiBtn.disabled = true;
    if (aiStatus) aiStatus.textContent = '正在检查可用文本模型…';
    try {
      const options = await api.providerOptions();
      if (!aiBtn.isConnected) return;
      const textOpts = (options || []).filter((o) => o.capability === 'text');
      const modelSel = root.querySelector('#ad-ai-model');
      const picked = modelSel?.value ? modelSel.value.split('|') : null;
      const chosen = picked && picked.length === 3
        ? textOpts.find((o) => o.backend === picked[0] && o.provider === picked[1] && o.model === picked[2])
        : null;
      const healthy = textOpts.filter((o) => o.health_state === 'ok');
      const pool = healthy.length ? healthy : textOpts;
      const slot = chosen || pool.find((o) => o.backend === 'local') || pool[0];
      if (!slot) {
        if (aiStatus) aiStatus.textContent = '暂无可用文本模型，请先到「模型中心」配置文本槽位';
        return;
      }
      if (slot.backend === 'cloud') {
        const go = await confirmDanger({
          title: '使用云端文本模型完善提示词',
          danger: false,
          okText: '确认提交',
          body: `<p>当前将使用云端文本模型 <b>${escapeHtml(slot.adapter_label || slot.provider)} / ${escapeHtml(slot.model)}</b> 扩写提示词，可能产生少量云端调用费用。</p>`,
        });
        if (!go || !aiBtn.isConnected) {
          if (aiStatus) aiStatus.textContent = '';
          return;
        }
      }
      const kind = root.querySelector('#ad-kind')?.value || 'character';
      const operation = kind === 'scene' ? 'scene_setting' : kind === 'prop' ? 'prop_setting' : 'character_profile';
      if (aiStatus) aiStatus.textContent = `正在提交给 ${slot.adapter_label || slot.provider} / ${slot.model}…`;
      const submitted = await api.assetGenerationSubmit(store.currentId, {
        operation,
        target_asset_id: isNew ? null : Number(id),
        text_mode: 'expand',
        target_field: 'consistency_prompt',
        instruction: '根据已有关键词、名称与简介，扩写并润色为可直接用于生图的一致性提示词，突出跨镜头固定不变的外形、配色、材质与特征细节，不要包含一次性动作。',
        draft_name: name,
        draft_summary: summary,
        draft_consistency_prompt: draftPrompt,
        execution_mode: slot.backend,
        provider_code: slot.provider,
        model_code: slot.model,
        user_confirmed_cloud: slot.backend === 'cloud',
        idempotency_key: `ad-refine-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      });
      if (!aiBtn.isConnected) return;
      if (aiStatus) aiStatus.textContent = `排队/生成中（作业 #${submitted.job_id}）…`;
      for (let i = 0; i < 60; i += 1) {
        await new Promise((r) => setTimeout(r, 1200));
        if (!aiBtn.isConnected) return;
        const res = await api.assetGeneration(store.currentId, submitted.job_id);
        if (!aiBtn.isConnected) return;
        if (res.status === 'succeeded') {
          const raw = String(res.fields?.consistency_prompt || res.raw || '').trim();
          const cleaned = raw
            .replace(/^```[\w-]*\n([\s\S]*?)\n```$/m, '$1')
            .replace(/^#{1,6}\s*一致性提示词[^\n]*\n+/m, '')
            .trim();
          if (!cleaned) {
            if (aiStatus) aiStatus.textContent = res.note || '生成完成但结果为空，可调整关键词重试';
            return;
          }
          if (promptEl) {
            prevPromptBeforeAi = promptEl.value;
            promptEl.value = cleaned.slice(0, 1000);
            promptEl.focus();
          }
          if (aiUndo) aiUndo.hidden = false;
          if (aiStatus) aiStatus.textContent = `已直接填入提示词框（${slot.model}），核对后请点右上角「保存」`;
          return;
        }
        if (!['pending', 'queued', 'running'].includes(res.status)) {
          if (aiStatus) aiStatus.textContent = res.error_message || res.note || `生成未成功（${res.status_label || res.status}）`;
          return;
        }
      }
      if (aiStatus) aiStatus.textContent = '生成耗时较长，可稍后在「生成中心」查看进度';
    } catch (err) {
      if (aiStatus && aiBtn.isConnected) aiStatus.textContent = `完善失败：${err.message || err}`;
    } finally {
      if (aiBtn.isConnected) aiBtn.disabled = false;
    }
  });

  const revBtn = root.querySelector('#ad-ai-reverse');
  const revCancel = root.querySelector('#ad-ai-cancel');
  let revAbort = null;
  revCancel?.addEventListener('click', () => {
    if (revAbort) revAbort.abort();
  });
  const runReversePrompt = async (imagePaths, triggerEl) => {
    if (isNew || !a.ref_images.length) {
      if (aiStatus) aiStatus.textContent = '请先保存资产并上传至少一张参考图';
      return;
    }
    if (revAbort) revAbort.abort();
    const ctl = new AbortController();
    revAbort = ctl;
    const promptEl = root.querySelector('#ad-prompt');
    const lang = root.querySelector('#ad-vision-lang')?.value || 'zh';
    const provider = root.querySelector('#ad-vision-provider')?.value || 'auto';
    if (triggerEl) triggerEl.disabled = true;
    if (revBtn && revBtn !== triggerEl) revBtn.disabled = true;
    if (revCancel) revCancel.hidden = false;
    if (aiStatus) aiStatus.textContent = imagePaths.length ? '正在分析指定参考图…' : '正在综合参考图反推提示词…';
    try {
      const res = await api.reverseAssetPrompt(
        a.id,
        { image_paths: imagePaths, provider, language: lang },
        { signal: ctl.signal },
      );
      if (!root.isConnected || ctl.signal.aborted) return;
      const existing = promptEl?.value.trim() || '';
      const choice = await askReversePromptApply(res, Boolean(existing));
      if (!choice || !root.isConnected || !choice.text) {
        if (aiStatus) aiStatus.textContent = '已取消应用反推结果';
        return;
      }
      if (promptEl) {
        prevPromptBeforeAi = promptEl.value;
        promptEl.value = (choice.mode === 'append' && existing
          ? `${existing}\n${choice.text}`
          : choice.text).slice(0, 1000);
        promptEl.focus();
      }
      if (aiUndo) aiUndo.hidden = false;
      if (aiStatus) aiStatus.textContent = `已反推提示词（${res.provider} / ${res.model}），核对后请点右上角「保存」`;
    } catch (err) {
      if (ctl.signal.aborted || err?.name === 'AbortError') {
        if (aiStatus && root.isConnected) aiStatus.textContent = '已取消本次看图反推';
        return;
      }
      if (aiStatus && root.isConnected) aiStatus.textContent = `反推失败：${err.message || err}`;
    } finally {
      if (revAbort === ctl) revAbort = null;
      if (revCancel?.isConnected) revCancel.hidden = true;
      if (triggerEl?.isConnected) triggerEl.disabled = false;
      if (revBtn?.isConnected) revBtn.disabled = isNew || !a.ref_images.length;
    }
  };
  revBtn?.addEventListener('click', () => runReversePrompt([], revBtn));

  const bindDimBadges = (scopeEl) => {
    const canEdit = isNew ? store.can('asset:write') : (store.can('asset:write') && (a.project_id !== null || store.isManager));
    const curKind = root.querySelector('#ad-kind')?.value || a.kind;
    const slotMap = new Map(slotsForKind(curKind).map((s) => [s.key, s]));
    scopeEl.querySelectorAll('.ad-img img').forEach((img) => {
      const fill = () => {
        if (img.naturalWidth <= 0 || img.naturalHeight <= 0) return;
        const fig = img.closest('.ad-img');
        const b = img.parentElement?.querySelector('[data-dim-badge]');
        const msgEl = fig?.querySelector('[data-check-msg]');
        const slot = img.dataset.slotKey ? slotMap.get(img.dataset.slotKey) || null : null;
        const check = evaluateReferenceImage(img.naturalWidth, img.naturalHeight, slot);
        if (b) {
          const icon = check.level === 'bad' ? '✕ ' : (check.level === 'warn' ? '⚠ ' : '✓ ');
          b.textContent = `${icon}${img.naturalWidth}×${img.naturalHeight}`;
          b.classList.remove('is-ok', 'is-warn', 'is-bad');
          b.classList.add(`is-${check.level}`);
          b.title = check.reasons.length ? check.reasons.join('；') : '符合参考图规格要求';
          b.hidden = false;
        }
        if (fig) {
          fig.classList.toggle('is-check-warn', check.level === 'warn');
          fig.classList.toggle('is-check-bad', check.level === 'bad');
        }
        if (msgEl) {
          if (check.reasons.length) {
            const text = (check.level === 'bad' ? '✕ ' : '⚠ ') + check.reasons.join(' · ');
            const canFix = !isNew && canEdit && check.ratioMismatch && slot && img.dataset.refPath;
            const canUpscale = !isNew && canEdit && check.lowRes && img.dataset.refPath;
            msgEl.innerHTML = `<span>${escapeHtml(text)}</span>${canUpscale
              ? ' <button type="button" class="btn xs ad-fix-btn" data-upscale-ref title="调用已启用的 ComfyUI 节点做 4x 动漫超分并替换原图">✨ AI 超分提清</button>' : ''}${canFix
              ? ' <button type="button" class="btn xs ad-fix-btn" data-fix-ref>✨ 修正比例</button>' : ''}`;
            msgEl.classList.remove('is-warn', 'is-bad');
            msgEl.classList.add(`is-${check.level}`);
            msgEl.hidden = false;
            const upscaleBtn = msgEl.querySelector('[data-upscale-ref]');
            if (upscaleBtn) {
              upscaleBtn.addEventListener('click', async () => {
                const oldPath = img.dataset.refPath;
                if (!oldPath) return;
                let res = null;
                const ok = await run('AI 动漫超分提清', async () => {
                  res = await api.upscaleAssetImage(id, { path: oldPath });
                }, { btn: upscaleBtn });
                if (ok) {
                  const dimTip = res?.width && res?.height ? `（${res.width}×${res.height}）` : '';
                  window.dispatchEvent(new CustomEvent('app:toast', {
                    detail: { text: `已通过 ComfyUI 超分提清并替换原图${dimTip}`, kind: 'ok' },
                  }));
                  refresh();
                }
              });
            }
            const fixBtn = msgEl.querySelector('[data-fix-ref]');
            if (fixBtn) {
              fixBtn.addEventListener('click', async () => {
                const oldPath = img.dataset.refPath;
                if (!oldPath) return;
                let fixedFile = null;
                try {
                  fixedFile = await askFixReferenceImage(`/files/${oldPath}`, slot);
                } catch (err) {
                  window.dispatchEvent(new CustomEvent('app:toast', {
                    detail: { text: `无法读取参考图：${err?.message || err}`, kind: 'bad' },
                  }));
                  return;
                }
                if (!fixedFile) return;
                const wasCover = oldPath === (a.ref_images || [])[0];
                const prevSet = new Set(a.ref_images || []);
                const ok = await run('修正参考图比例', async () => {
                  const updated = await api.uploadAssetImage(id, fixedFile);
                  const newPath = (updated?.ref_images || []).find((p) => !prevSet.has(p))
                    || (updated?.ref_images || [])[(updated?.ref_images?.length || 1) - 1]
                    || '';
                  if (!newPath) throw new Error('上传修正图后未拿到新路径');
                  await api.setAssetImagePurpose(id, { path: newPath, variant_id: null, purpose: slot.key });
                  if (wasCover) {
                    await api.setAssetCover(id, newPath);
                  }
                  await api.deleteAssetImage(id, oldPath);
                }, { btn: fixBtn });
                if (ok) refresh();
              });
            }
          } else {
            msgEl.hidden = true;
          }
        }
      };
      if (img.complete) fill();
      else img.addEventListener('load', fill, { once: true });
    });
  };
  bindDimBadges(root);

  const uploadOneFile = async (file, slot = null, triggerEl = null) => {
    const prepared = await prepareSlotUpload(file, slot);
    if (!prepared) return;
    if (isNew) {
      if (slot) {
        const existingIdx = pendingImages.findIndex((p) => p.slotKey === slot.key);
        if (existingIdx >= 0) {
          const [old] = pendingImages.splice(existingIdx, 1);
          if (old?.url) URL.revokeObjectURL(old.url);
        }
      }
      const entry = { file: prepared.file, url: URL.createObjectURL(prepared.file), slotKey: slot?.key || null };
      if (slot?.isCover) pendingImages.unshift(entry);
      else pendingImages.push(entry);
      paintPending();
      return;
    }
    const prevSet = new Set(a.ref_images || []);
    let uploadedPath = '';
    const ok = await run('上传', async () => {
      const updated = await api.uploadAssetImage(id, prepared.file);
      uploadedPath = (updated?.ref_images || []).find((p) => !prevSet.has(p))
        || (updated?.ref_images || [])[(updated?.ref_images?.length || 1) - 1]
        || '';
      if (uploadedPath && slot?.key) {
        await api.setAssetImagePurpose(id, { path: uploadedPath, variant_id: null, purpose: slot.key });
      }
      if (uploadedPath && slot?.isCover && (a.ref_images || []).length > 0) {
        await api.setAssetCover(id, uploadedPath);
      }
    }, { btn: triggerEl });
    if (ok) refresh();
  };

  const paintPending = () => {
    const slotsHost = root.querySelector('[data-ad-slots]');
    const host = root.querySelector('[data-ad-imgs]');
    if (!host || !slotsHost) return;
    const cntBadge = root.querySelector('[data-ad-tab="refs"] .ad-sec-cnt');
    if (cntBadge) cntBadge.textContent = String(pendingImages.length);
    const curKind = root.querySelector('#ad-kind')?.value || a.kind;
    const slots = slotsForKind(curKind);
    const byKey = new Map();
    const extra = [];
    pendingImages.forEach((item, idx) => {
      if (item.slotKey && !byKey.has(item.slotKey)) byKey.set(item.slotKey, { item, idx });
      else extra.push({ item, idx });
    });

    const pendingCard = ({ item, idx }, slotMeta = null) => `
      <figure class="ad-img${idx === 0 ? ' cover' : ''}">
        ${slotMeta ? `<div class="ad-slot-bar"><b class="ad-slot-title">${escapeHtml(slotMeta.label)}</b><span class="ad-slot-ratio">${escapeHtml(slotMeta.spec)}</span></div>` : ''}
        <div class="ad-img-wrap">
          <img src="${escapeHtml(item.url)}" alt="${escapeHtml(item.file.name)}"${slotMeta ? ` data-slot-key="${escapeHtml(slotMeta.key)}"` : ''}>
          <span class="ad-dim-badge" data-dim-badge hidden></span>
        </div>
        <div class="ad-check-msg" data-check-msg hidden></div>
        <figcaption>
          ${idx === 0 ? '<span class="chip blue">预选封面</span>' : `<span class="t-dim">图 ${idx + 1}</span>`}
          <button type="button" class="btn xs danger" data-rm-pending="${idx}">移除</button>
        </figcaption>
      </figure>`;

    slotsHost.innerHTML = slots.map((s) => {
      const hit = byKey.get(s.key);
      if (hit) return pendingCard(hit, s);
      return `<div class="ad-slot-empty">
        <div class="ad-slot-bar">
          <b class="ad-slot-title">${escapeHtml(s.label)}</b>
          <span class="ad-slot-ratio">${escapeHtml(s.spec)}</span>
        </div>
        <div class="ad-slot-drop">
          <p class="ad-slot-hint">${escapeHtml(s.hint)}</p>
          <label class="btn xs primary ad-slot-upload">预选到此槽
            <input type="file" data-slot-upload="${escapeHtml(s.key)}" accept="image/png,image/jpeg,image/webp" hidden>
          </label>
        </div>
      </div>`;
    }).join('');

    host.innerHTML = extra.length
      ? extra.map((entry) => pendingCard(entry, null)).join('')
      : '';

    const container = root.querySelector('[data-ad-slots]')?.parentElement;
    if (container) bindDimBadges(container);

    slotsHost.querySelectorAll('[data-slot-upload]').forEach((inp) => {
      inp.addEventListener('change', async (e) => {
        const f = e.target.files?.[0];
        e.target.value = '';
        if (!f) return;
        const slot = slots.find((s) => s.key === inp.dataset.slotUpload) || null;
        await uploadOneFile(f, slot, inp);
      });
    });

    container?.querySelectorAll('[data-rm-pending]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const i = Number(btn.dataset.rmPending);
        const [removed] = pendingImages.splice(i, 1);
        if (removed?.url) URL.revokeObjectURL(removed.url);
        paintPending();
      });
    });
  };

  if (isNew) {
    paintPending();
    root.querySelector('#ad-kind')?.addEventListener('change', paintPending);
  } else {
    root.querySelectorAll('[data-slot-upload]').forEach((inp) => {
      inp.addEventListener('change', async (e) => {
        const f = e.target.files?.[0];
        e.target.value = '';
        if (!f) return;
        const slot = slotsForKind(a.kind).find((s) => s.key === inp.dataset.slotUpload) || null;
        await uploadOneFile(f, slot, inp);
      });
    });
  }

  // 注意选择器：这个 input 是 id="ad-upload"（藏在 label 里），
  // 老列表页用的是 data-upload，抄过来会绑到一个不存在的节点上、上传静默失效
  root.querySelector('#ad-upload')?.addEventListener('change', async (e) => {
    const files = [...(e.target.files || [])];
    e.target.value = '';
    if (!files.length) return;
    if (isNew) {
      for (const file of files) {
        await uploadOneFile(file, null, e.target);
      }
      return;
    }
    await uploadOneFile(files[0], null, e.target);
  });

  root.querySelectorAll('[data-cover]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const ok = await run('设为封面', () => api.setAssetCover(id, btn.dataset.cover));
      if (ok) refresh();
    });
  });

  root.querySelectorAll('[data-rm-img]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const ok = await run('删除这张图', () => api.deleteAssetImage(id, btn.dataset.rmImg));
      if (ok) refresh();
    });
  });

  const delModal = root.querySelector('#ad-del-modal');
  const delOk = root.querySelector('#ad-del-ok');
  const closeDel = () => { delModal.hidden = true; delOk.disabled = false; };
  root.querySelector('#ad-del')?.addEventListener('click', () => { delModal.hidden = false; });
  root.querySelector('#ad-del-cancel')?.addEventListener('click', closeDel);
  delModal?.addEventListener('click', (e) => { if (e.target === delModal) closeDel(); });
  // Escape 走 util 的那一个实现：绑在 delModal 上只有"焦点已经 Tab 进弹窗"
  // 才收得到 keydown，而打开这个确认框时焦点还留在外面那张卡片上。
  if (delModal) bindModalDismiss(delModal, closeDel);
  delOk?.addEventListener('click', async () => {
    // 按钮交给 run 管进行中态：点下去会显示「删除资产中…」并禁用，
    // 结束（成功或失败）自动还原。原来这里只在失败分支手动放开，
    // 成功分支靠 closeDel() 把弹窗整个移除——看起来没问题，但一旦
    // closeDel 之后还有别的路径回到这个按钮，它就会停在禁用态。
    const ok = await run('删除资产', () => api.deleteAsset(id), { btn: delOk });
    if (!ok) return;
    closeDel();
    back();                      // 删完停在列表，别停在这条已经不存在的详情上
    refresh();
  });
}
