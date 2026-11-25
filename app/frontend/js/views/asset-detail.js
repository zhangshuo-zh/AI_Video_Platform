import { openCharacterAppearances } from './asset-appearances.js';
import { openReferenceUsage } from './asset-reference-usage.js';
import { referenceActions, bindReferenceActions } from './asset-reference-actions.js';
import { askReversePromptApply, assignReferenceSlots, prepareSlotUpload, slotsForKind } from './asset-reference-slots.js';
import { store } from '../store.js';
import { api } from '../api.js';
import { bindModalDismiss, confirmDanger, escapeHtml, formatMoney, run, safeOpenUrl } from '../util.js';
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
let pendingTurnaroundJob = null; // { assetId, jobId } 创建角色后待自动挂入「三视图」槽位的生成作业

export function setNewAssetKind(kind) {
  newAssetKind = ['character', 'scene', 'prop'].includes(kind) ? kind : 'character';
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
    store.can('job:submit') ? api.providerOptions() : Promise.resolve([]),
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
  return { asset, shots, refsError, profile, profileError, variants, variantsError,
           gens, gensError, impact, textOptions };
}

export async function renderDetail(id) {
  const isNew = id === 'new';
  if (isNew) {
    pendingImages.forEach((p) => { if (p.url) URL.revokeObjectURL(p.url); });
    pendingImages = [];
  }
  const newProviderOptions = isNew && store.can('job:submit')
    ? await api.providerOptions().catch(() => [])
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
          ${!isNew && canEdit ? '<button class="btn danger" id="ad-del">删除</button>' : ''}
          ${canEdit ? `<button class="btn primary" id="ad-save">${isNew ? '保存并创建' : '保存'}</button>` : ''}
        </div>
      </div>

      <div class="ad-grid">
        <div class="stack">
          <div class="card">
            <div class="card-head"><h2>基本信息</h2>
              <span class="sub">名称在同一范围、同一类型里唯一</span></div>
            <div class="card-body">
              ${isNew && canEdit && store.can('job:submit') && store.currentId ? (() => {
                const imgOpts = data.imageOptions || [];
                const isBadImg = (o) => ['missing', 'unconfigured', 'unregistered'].includes(o.health_state);
                const imgPref = imgOpts.find((o) => o.backend === 'local' && (o.health_state === 'ok' || o.validation_state === 'ok'))
                  || imgOpts.find((o) => o.backend === 'local' && !isBadImg(o))
                  || imgOpts.find((o) => o.health_state === 'ok' || o.validation_state === 'ok')
                  || imgOpts[0];
                const imgOptHtml = imgOpts.map((o, idx) => {
                  const val = `${o.backend}|${o.provider}|${o.model}`;
                  const sel = imgPref && o === imgPref ? ' selected' : (!imgPref && idx === 0 ? ' selected' : '');
                  const tag = o.backend === 'cloud' ? '云端' : '本地';
                  const warn = isBadImg(o) ? `（${HEALTH_LABEL[o.health_state] || '不可用'}）`
                    : (o.validation_state === 'ok' ? '（已验证）' : '');
                  return `<option value="${escapeHtml(val)}"${sel}>${tag} · ${escapeHtml(o.adapter_label || o.provider)} / ${escapeHtml(o.model)}${warn}</option>`;
                }).join('');
                return `<div class="ad-random-card" id="ad-random-card"${a.kind === 'character' ? '' : ' hidden'}>
                  <div class="ad-random-row">
                    <div class="ad-random-title"><b>🎲 随机角色抽卡（两步确认流）</b>
                      <span class="t-dim">第一步随机生成完整人设，可反复重抽微调；第二步保存角色并异步生成三视图</span></div>
                    <div class="ad-random-controls">
                      <input class="ctl sm" id="ad-rand-theme" maxlength="100"
                        placeholder="灵感词（选填，留空纯随机，如：赛博猫娘、白发异瞳）">
                      <button type="button" class="btn sm" id="ad-rand-btn">🎲 随机生成人设</button>
                    </div>
                  </div>
                  <div class="ad-random-row ad-random-tv">
                    <label class="ad-tv-label">三视图生图模型
                      <select class="ctl sm" id="ad-tv-model" aria-label="三视图生图模型">
                        ${imgOptHtml || '<option value="">暂无可用生图模型（请先到模型中心配置）</option>'}
                      </select>
                    </label>
                    <button type="button" class="btn primary sm" id="ad-save-tv"${imgOpts.length ? '' : ' disabled'}>🎨 保存并生成三视图</button>
                  </div>
                  <p class="t-dim ad-random-status" id="ad-rand-status" role="status"></p>
                </div>`;
              })() : ''}
              <div class="form-grid">
                <label for="ad-kind">资产类型</label>
                <select id="ad-kind" class="ctl"${canEdit ? '' : ' disabled'}>${kindOptions}</select>

                <label for="ad-name">名称</label>
                <input id="ad-name" class="ctl" maxlength="60" value="${escapeHtml(a.name)}"
                       autocomplete="off"${canEdit ? '' : ' disabled'}>

                <label for="ad-summary" class="top">简介</label>
                <textarea id="ad-summary" class="ctl" rows="2" maxlength="500"
                          ${canEdit ? '' : 'disabled'}>${escapeHtml(a.summary)}</textarea>

                <label for="ad-tags">标签</label>
                <input id="ad-tags" class="ctl" value="${escapeHtml((a.tags || []).join(', '))}"
                       placeholder="逗号分隔，最多 10 个"${canEdit ? '' : ' disabled'}>

                <label for="ad-aliases">别名</label>
                <input id="ad-aliases" class="ctl" value="${escapeHtml((a.aliases || []).join(', '))}"
                       placeholder="逗号分隔"${canEdit ? '' : ' disabled'}>

                <label for="ad-prompt" class="top">一致性提示词</label>
                <div class="ad-prompt-col">
                  <textarea id="ad-prompt" class="ctl" rows="5" maxlength="1000"
                            placeholder="可先写几个关键特征词，再点下方「AI 完善提示词」扩写润色"${canEdit ? '' : ' disabled'}
                      >${escapeHtml(a.consistency_prompt)}</textarea>
                  ${canEdit && store.can('job:submit') && store.currentId ? (() => {
                    const textOpts = data.textOptions || [];
                    const isBadHealth = (o) => ['missing', 'unconfigured', 'unregistered'].includes(o.health_state);
                    const preferred = textOpts.find((o) => o.backend === 'local' && (o.health_state === 'ok' || o.validation_state === 'ok'))
                      || textOpts.find((o) => o.backend === 'local' && !isBadHealth(o))
                      || textOpts.find((o) => o.health_state === 'ok' || o.validation_state === 'ok')
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
                        <button type="button" class="btn sm" id="ad-ai-reverse"${isNew || !a.ref_images.length ? ' disabled' : ''} title="${isNew ? '请先保存资产并上传参考图' : (!a.ref_images.length ? '请先在右侧上传参考图' : '综合封面与三视图反向生成一致性提示词')}">📷 看图反推</button>
                      </div>
                    </div>
                    <div class="ad-ai-feedback">
                      <span class="t-dim" id="ad-ai-status" role="status"></span>
                      <button type="button" class="btn xs ghost" id="ad-ai-cancel" hidden>取消反推</button>
                      <button type="button" class="btn xs ghost" id="ad-ai-undo" hidden>撤销还原</button>
                    </div>`;
                  })() : ''}
                </div>

                <span class="pf-label">归属</span>
                ${isGlobal
                  ? '<span class="t-dim">全局资产池 —— 已属于所有项目，不能收回成单个项目资产'
                    + '（可能正被多个项目的镜头引用，收回会让引用悬空）</span>'
                  : (store.isManager
                      ? `<label class="chk"><input type="checkbox" id="ad-global">
                           <span>${isNew ? '放进全局资产池（各项目都能引用，不属于当前项目）' : '升为全局资产（id 不变，现有引用照旧有效）'}</span></label>`
                      : '<span class="t-dim">本项目资产（全局资产只有管理员能建）</span>')}
              </div>
              ${isNew ? '<p class="modal-meta">同一范围、同一类型里名称唯一；重名说明该合并，不是新建。</p>'
                : `<p class="modal-meta">创建于 ${a.created_at}　最后修改 ${a.updated_at}${canEdit ? '' : '　· 你对此资产只读'}</p>`}
              ${a.ref_shot_count ? `<p class="note" style="margin-top:8px">当前被 <b>${a.ref_shot_count}</b> 条镜头引用${signedCount ? `（含 <b>${signedCount}</b> 条已签字正式产物）` : ''}${impactCounts?.stale ? `，其中 <b>${impactCounts.stale}</b> 镜源已过期` : ''}；修改一致性提示词或更换封面后，相关镜头产物将标记为源已变。</p>` : ''}
            </div>
          </div>

          ${isNew ? '' : (() => {
            const miss = missingOf(a, data.profile, variants);
            return `<div class="card">
            <div class="card-head"><h2>还缺什么</h2>
              <span class="sub">${miss.length ? `${miss.length} 项` : '该有都有了'}</span>
              ${miss.length ? '<div class="right"><button class="btn sm" data-goto-studio>去补齐</button></div>' : ''}</div>
            <div class="card-body">
              ${miss.length ? `<div class="ad-miss-list">${miss.map((m) => `<span class="chip amber">缺 · ${escapeHtml(m)}</span>`).join('')}</div>
                  <p class="t-dim" style="margin:8px 0 0">以上项目可在「资产创作」中补齐——这里只读，写只在创作那一页。</p>`
                : '<p class="t-dim">基础资料齐了。齐不等于对：一致性提示词好不好用，'
                  + '要看下一镜出来的画面，那要跑一次才知道。</p>'}
            </div>
          </div>`;
          })()}

          ${!isNew && data.profileError ? `<div class="card"><div class="bar bad">
            人物档案读不到：${escapeHtml(data.profileError)}</div></div>` : ''}
          ${!isNew && a.kind === 'character' && !data.profileError ? `
          <div class="card">
            <div class="card-head"><h2>人物档案</h2>
              <span class="sub">${data.profile && data.profile.exists ? '最后改于 ' + escapeHtml(data.profile.updated_at) : '还没写过'}</span>
              <div class="right"><button class="btn sm" data-goto-studio>去创作</button></div></div>
            <div class="card-body">
              ${[['履历', 'biography'], ['性格', 'personality'], ['成长过程', 'growth_arc'],
                 ['文字设定提示词', 'text_prompt']].map(([label, key]) => `
                <p class="note"><b>${label}</b><br>
                  ${escapeHtml((data.profile && data.profile[key]) || '—')}</p>`).join('')}
              ${data.profile ? `<p class="note"><b>专属 TTS 配音绑定</b><br>
                声线：${escapeHtml(data.profile.tts_voice || '默认')} ·
                语速：${escapeHtml(String(data.profile.tts_rate ?? 1.0))}x ·
                情绪／造声：${escapeHtml(data.profile.tts_mood || '默认')}</p>` : ''}
              <p class="t-desc">档案与一致性提示词是两回事：这一份讲"这个人是怎么走到今天的"，
                上面那一格讲"他画出来必须长什么样"。这里只读，改在资产创作。</p>
            </div>
          </div>` : ''}
          ${!isNew && data.variantsError ? `<div class="card"><div class="bar bad">
            形态清单读不到：${escapeHtml(data.variantsError)}</div></div>` : ''}
          ${!isNew && !data.variantsError ? `
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

          <div class="card">
            <div class="card-head"><h2>一致性提示词怎么写</h2></div>
            <div class="card-body">
              <p class="note">
                ${a.kind === 'character'
                  ? '角色：面部与识别特征、发型、服装、体型与总体风格、必须保持不变的细节。'
                  : a.kind === 'scene'
                    ? '场景：空间结构、建筑风格、环境与光照氛围、需要保持一致的固定元素。'
                    : '道具：形状、材质、颜色、尺寸与比例、独特标记。'}
                这段是喂给生成模型的稳定描述，抽取只会给建议值，最终由你改。
              </p>
            </div>
          </div>
        </div>

        <div class="stack">
          <div class="card">
            <div class="card-head"><h2>参考图</h2>
              <span class="sub">第一张是封面</span>
              <div class="right">
                ${!isNew && a.kind === 'character' && store.currentId && store.can('job:read') && (a.project_id == null || a.project_id === store.currentId)
                  ? '<button class="btn sm" data-appearance-open>角色出镜对照</button>' : ''}
                ${canEdit ? `<label class="btn sm ad-upload">${isNew ? '预选参考图' : '上传参考图'}
                  <input type="file" id="ad-upload" accept="image/png,image/jpeg,image/webp"${isNew ? ' multiple' : ''} hidden>
                </label>` : ''}
              </div></div>
            <div class="card-body">
              ${!isNew && pendingTurnaroundJob && pendingTurnaroundJob.assetId === a.id
                ? `<div class="bar info ad-tv-watch" id="ad-tv-watch" role="status">🎨 正在生成角色三视图（作业 #${pendingTurnaroundJob.jobId}），完成后将自动挂入「三视图」槽位… <a href="#generate/0">去生成中心查看</a></div>`
                : ''}
              ${isNew ? '<div class="ad-slots" data-ad-slots></div><div class="ad-imgs" data-ad-imgs></div>' : (() => {
                const { slots, extra } = assignReferenceSlots(a.kind, a.ref_images, a.image_purposes);
                const canUse = Boolean(store.currentEpisodeId && store.can('job:submit'));
                const coverPath = a.ref_images[0] || '';
                const renderCard = (p, slotMeta = null) => {
                  const isCover = p === coverPath;
                  return `<figure class="ad-img${isCover ? ' cover' : ''}">
                    ${slotMeta ? `<div class="ad-slot-bar"><b class="ad-slot-title">${escapeHtml(slotMeta.label)}</b><span class="ad-slot-ratio">${escapeHtml(slotMeta.spec)}</span></div>` : ''}
                    <div class="ad-img-wrap">
                      <img src="${escapeHtml(`/files/${p}`)}" alt="${escapeHtml(slotMeta ? slotMeta.label : '参考图')}" loading="lazy" data-preview="${escapeHtml(`/files/${p}`)}">
                      <span class="ad-dim-badge" data-dim-badge hidden></span>
                    </div>
                    <figcaption>
                      ${isCover ? '<span class="chip blue">封面</span>'
                        : (canEdit ? `<button class="btn xs" data-cover="${escapeHtml(p)}">设为封面</button>` : '')}
                      ${canEdit && !isNew ? `<button type="button" class="btn xs" data-reverse-img="${escapeHtml(p)}" title="仅用这张图反推一致性提示词">反推</button>` : ''}
                      ${canEdit ? `<button class="btn xs danger" data-rm-img="${escapeHtml(p)}">删除</button>` : ''}
                    </figcaption>
                    ${referenceActions(p, a.image_purposes?.[p], canEdit, canUse)}
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
                const extraHtml = extra.length ? `
                  <div class="ad-extra-head">补充参考图（${extra.length}）</div>
                  <div class="ad-imgs" data-ad-imgs>${extra.map((p) => renderCard(p, null)).join('')}</div>` : '';
                return `<div class="ad-slots">${slotsHtml}</div>${extraHtml}`;
              })()}
              ${canEdit ? `<p class="modal-meta">短边 ≥ 512px，支持 PNG / JPEG / WebP（≤20MB），按槽位上传可对齐推荐比例。</p>` : ''}
            </div>
          </div>

          ${isNew ? '' : `<div class="card">
            <div class="card-head"><h2>最近生成记录</h2>
              <span class="sub">${(data.gens || []).length} 条 · 只读</span>
              <div class="right"><button class="btn sm" data-goto-studio>去生成</button></div></div>
            <div class="card-body">
              ${data.gensError ? `<div class="bar bad">生成记录读不到：${escapeHtml(data.gensError)}</div>` : ''}
              <details class="ad-fold"${data.gensError ? ' open' : ''}>
                <summary class="ad-fold-sum">${(data.gens || []).length ? `展开查看 ${(data.gens || []).length} 条生成记录` : '暂无生成记录（产物需在「资产创作」显式存入）'}</summary>
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
              <details class="ad-fold"${(refs.length > 0 && refs.length <= 5) || impactCounts?.stale || cache.refsError ? ' open' : ''}>
                <summary class="ad-fold-sum">${refs.length ? `展开查看 ${refs.length} 条引用镜头明细` : '暂无镜头引用（在创作中心分镜表引用）'}</summary>
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
          </div>`}
        </div>
      </div>

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
  const kindSel = root.querySelector('#ad-kind');
  if (isNew && randomCard && kindSel) {
    kindSel.addEventListener('change', () => {
      randomCard.hidden = kindSel.value !== 'character';
    });
  }

  const randBtn = root.querySelector('#ad-rand-btn');
  const randStatus = root.querySelector('#ad-rand-status');
  randBtn?.addEventListener('click', async () => {
    const modelSel = root.querySelector('#ad-ai-model');
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
    randBtn.disabled = true;
    if (randStatus) randStatus.textContent = '正在构思角色设定…';
    try {
      const theme = root.querySelector('#ad-rand-theme')?.value.trim() || '';
      const out = await api.assetRandomCharacter(store.currentId, {
        theme, execution_mode: mode, provider_code: providerCode, model_code: modelCode,
      });
      if (!randBtn.isConnected) return;
      root.querySelector('#ad-name').value = out.name || '';
      root.querySelector('#ad-summary').value = out.summary || '';
      root.querySelector('#ad-tags').value = (out.tags || []).join(', ');
      root.querySelector('#ad-aliases').value = (out.aliases || []).join(', ');
      root.querySelector('#ad-prompt').value = out.consistency_prompt || '';
      randBtn.textContent = '🎲 换一个人设';
      if (randStatus) {
        randStatus.textContent = `已通过 ${out.provider} / ${out.model} 生成「${out.name}」，可继续微调或重抽，满意后点「保存并生成三视图」`;
      }
    } catch (err) {
      if (randStatus && randBtn.isConnected) randStatus.textContent = `随机生成失败：${err.message || err}`;
    } finally {
      if (randBtn.isConnected) randBtn.disabled = false;
    }
  });

  const saveTvBtn = root.querySelector('#ad-save-tv');
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
    if (execMode === 'cloud') {
      const go = await confirmDanger({
        title: '创建角色并提交云端三视图生成',
        danger: false,
        okText: '确认创建并生图',
        body: `<p>将先创建角色「<b>${escapeHtml(payload.name)}</b>」，并提交三视图生成任务到云端模型 <b>${escapeHtml(providerCode)} / ${escapeHtml(modelCode)}</b>（按模型中心口径计费）。</p>`,
      });
      if (!go || !saveTvBtn.isConnected) return;
    }
    saveTvBtn.disabled = true;
    if (saveBtn) saveBtn.disabled = true;
    if (randStatus) randStatus.textContent = '正在创建角色并提交三视图生成任务…';
    const created = await createNewAsset(payload, saveTvBtn);
    if (!created) {
      if (saveTvBtn.isConnected) saveTvBtn.disabled = false;
      if (saveBtn?.isConnected) saveBtn.disabled = false;
      return;
    }
    const coreDesc = payload.consistency_prompt || payload.summary || payload.name;
    const turnaroundPrompt = `角色三视图设定稿（正面、侧面、背面），全身像，站姿清晰，干净纯色背景，保持五官发型服装配色完全一致：${coreDesc}`.slice(0, 1000);
    try {
      const submitted = await api.assetGenerationSubmit(store.currentId, {
        operation: 'asset_image',
        target_asset_id: created.id,
        image_prompt: turnaroundPrompt,
        execution_mode: execMode,
        provider_code: providerCode,
        model_code: modelCode,
        params: { width: 1344, height: 768 },
        user_confirmed_cloud: execMode === 'cloud',
        idempotency_key: `ad-tv-${created.id}-${Date.now()}`,
      });
      pendingTurnaroundJob = { assetId: created.id, jobId: submitted.job_id };
    } catch (err) {
      pendingTurnaroundJob = null;
      if (randStatus && saveTvBtn.isConnected) {
        randStatus.textContent = `角色已创建，但三视图任务提交失败：${err.message || err}`;
      }
    }
    openDetail('assets', created.id);
  });

  if (!isNew && pendingTurnaroundJob && pendingTurnaroundJob.assetId === a.id && store.currentId) {
    const watch = pendingTurnaroundJob;
    const watchBar = root.querySelector('#ad-tv-watch');
    (async () => {
      for (let i = 0; i < 60; i += 1) {
        await new Promise((r) => setTimeout(r, 2500));
        if (!root.isConnected || pendingTurnaroundJob?.jobId !== watch.jobId) return;
        try {
          const gen = await api.assetGeneration(store.currentId, watch.jobId);
          if (!root.isConnected || pendingTurnaroundJob?.jobId !== watch.jobId) return;
          if (gen.status === 'succeeded') {
            pendingTurnaroundJob = null;
            const imported = await api.assetImportImage(a.id, { job_id: watch.jobId });
            if (imported?.path) {
              await api.setAssetImagePurpose(a.id, { path: imported.path, variant_id: null, purpose: '三视图' });
            }
            refresh();
            return;
          }
          if (gen.status === 'failed' || gen.status === 'cancelled') {
            pendingTurnaroundJob = null;
            if (watchBar?.isConnected) {
              watchBar.className = 'bar bad ad-tv-watch';
              watchBar.textContent = `三视图生成未完成（${publicStatus(gen.status)}）：${gen.error || '请前往生成中心查看'}`;
            }
            return;
          }
        } catch {
          // 网络抖动时继续下一轮
        }
      }
      pendingTurnaroundJob = null;
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
  root.querySelectorAll('[data-reverse-img]').forEach((btn) => {
    btn.addEventListener('click', () => runReversePrompt([btn.dataset.reverseImg], btn));
  });

  const bindDimBadges = (scopeEl) => {
    scopeEl.querySelectorAll('.ad-img img').forEach((img) => {
      const fill = () => {
        const b = img.parentElement?.querySelector('[data-dim-badge]');
        if (b && img.naturalWidth > 0) {
          b.textContent = `${img.naturalWidth}×${img.naturalHeight}`;
          b.hidden = false;
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
          <img src="${escapeHtml(item.url)}" alt="${escapeHtml(item.file.name)}">
          <span class="ad-dim-badge" data-dim-badge hidden></span>
        </div>
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
