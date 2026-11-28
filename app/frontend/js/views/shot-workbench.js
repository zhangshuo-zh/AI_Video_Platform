import { openShotTask, readReworkDraft, reworkPrefill, SHOT_TASK_GROUPS, shotTaskFocus, taskAtCursor, taskCursor } from '../episode-work.js';
import { api } from '../api.js';
import { store } from '../store.js';
import { bindModalDismiss, escapeHtml, run } from '../util.js';
import { getParam, getShotEntry, setJobPrefill, setParam, setReviewReturn, setShotEntry, setSub } from '../navstate.js';
import { renderView } from '../router.js';
import { jobLabel, jobTone, publicStatus } from '../validation.js';
import { gotoEntry } from '../goto.js';
import { checklistHtml } from './review-checklist.js';
let reviewDraft = null;
let currentShotData = null;
let savedFrame = null;

/**
 * 单镜头生产工作台（0037，任务书 §6）。这是本次改造的业务闭环：
 * 一镜从设定、引用、关键帧、视频、音频到审核与"能不能进合成"，
 * 在一屏里看完并做完该做的跳转，不需要在六个模块之间来回找。
 *
 * 四条纪律：
 * 1. **正式产物只有一个定义**——最新一条"成功且签过通过"的作业。
 *    这一页不给"设为正式"这种旁路按钮；要改正式结果就去审核中心签另一条，
 *    与 `composition/timeline.build()` 的取片规则同源。
 * 2. **模型不支持的能力不出现**。首尾帧、运镜参数这类只有在清单里
 *    登记过对应参数时才允许显示为可提交，否则连下拉都不给（任务书 §6.1 第四阶段）。
 * 3. **写动作一律交回各模块**：提交给生成中心、签字给审核中心、素材给音频中心。
 *    这一页自己只写一件事——镜头设定五栏（那是分镜的字段，走的还是 `PUT /api/shots`）。
 * 4. **能不能合成用同一份预检**：`compose_ready.blockers` 就是
 *    `precheck.shot_precheck()` 的清单，不在这页另算一套判据。
 */

function chip(tone, text, title = '') {
  return `<span class="chip ${tone}"${title ? ` title="${escapeHtml(title)}"` : ''}>${escapeHtml(text)}</span>`;
}

// 素材状态只有四个取值（0019 的 CHECK），但界面要说人话：
// queued 显示"排队中"而不是"queued"，和作业那一列同一套词
const CLIP_LABEL = { draft: '草稿', queued: '排队中', ready: '已就绪', failed: '失败' };

function jobChip(job) {
  if (!job) return '<span class="hint">没有记录</span>';
  return chip(jobTone(job.status), jobLabel(job.status, job.status), job.error_message || '');
}

function videoArtifactIndex(job) {
  return job.status === 'succeeded'
    ? job.artifacts.findIndex((a) => a.exists && /\.(mp4|webm|mov|m4v)$/i.test(a.path)) : -1;
}

function artifactPreview(job) {
  if (!job || !job.artifacts || !job.artifacts.length) return '<span class="hint">产物不可读或已清理</span>';
  const videoIndex = job.capability === 'video' ? videoArtifactIndex(job) : -1;
  const index = videoIndex >= 0 ? videoIndex
    : job.artifacts.findIndex((a) => a.exists && /\.(mp4|webm|mov|m4v|png|jpg|jpeg|webp)$/i.test(a.path));
  if (index < 0) return '<span class="hint">产物不可预览或已清理</span>';
  const name = job.artifacts[index].path.split('/').pop() || '';
  const src = api.jobArtifactUrl(job.job_id, index);
  if (/\.(mp4|webm|mov|m4v)$/i.test(name)) {
    return `<video controls playsinline preload="none" class="sw-preview-video" src="${src}"></video>`;
  }
  if (/\.(png|jpg|jpeg|webp)$/i.test(name)) {
    return `<img loading="lazy" src="${src}" alt="${escapeHtml(name)}" class="sw-preview-img">`;
  }
  return `<span class="hint mono">${escapeHtml(name)}</span>`;
}

function jobRows(jobs, formalId) {
  if (!jobs.length) return '<p class="hint">这一镜还没有生成记录。</p>';
  return `<div class="table-wrap"><table>
    <thead><tr><th>作业</th><th>状态</th><th>对外</th><th>模型</th><th>产物</th><th>签字</th><th>耗时／成本</th></tr></thead>
    <tbody>${jobs.map((j) => `<tr${j.job_id === formalId ? ' class="on"' : ''}>
      <td class="mono">#${j.job_id}${j.job_id === formalId ? ' ' + chip('green', '正式') : ''}</td>
      <td>${jobChip(j)}</td>
      <td><span class="hint">${escapeHtml(publicStatus(j.status))}</span></td>
      <td class="hint">${escapeHtml(j.provider)}／${escapeHtml(j.model)}
        <div class="hint">${escapeHtml(j.execution_mode)}</div></td>
      <td>${artifactPreview(j)}</td>
      <td>${j.sign ? chip(j.sign.decision === 'approved' ? 'green' : 'red',
        j.sign.decision === 'approved' ? '通过' : '未通过',
        `${j.sign.signed_by} · ${j.sign.signed_at}`) : '<span class="hint">没签过</span>'}</td>
      <td class="hint">${j.elapsed_ms ? Math.round(j.elapsed_ms / 1000) + 's' : '—'}
        ${j.cost_micros ? ' · ' + (j.cost_micros / 1e6).toFixed(2) + (j.currency || '') : ''}</td>
    </tr>`).join('')}</tbody></table></div>`;
}

const TRANSITION_LABELS = {
  cut: '硬切 (Cut)',
  crossfade: '叠化 (Crossfade)',
  fade_black: '黑场渐变 (Fade Black)',
};
const CAMERA_MOVE_OPTIONS = ['固定', '推镜', '拉镜', '摇镜', '移镜', '环绕', '跟随', '升降'];

function frameSrc(val) {
  const s = String(val || '').trim();
  if (!s) return '';
  if (s.startsWith('/') || s.startsWith('http://') || s.startsWith('https://')) return s;
  return `/files/${s}`;
}

function fmtMsSec(ms) {
  return `${(Math.max(0, Number(ms) || 0) / 1000).toFixed(2)}s`;
}

function shotToast(text, kind = 'warn') {
  window.dispatchEvent(new CustomEvent('app:toast', { detail: { text, kind } }));
}

function frameCaptureControls(d, player, jobId, suffix = '') {
  if (!store.can('doc:write')) return '';
  const saved = savedFrame?.shotId === d.shot_id && savedFrame.player === player && savedFrame.jobId === jobId;
  return `<div class="row u-gap-6" data-frame-capture="${player}" data-frame-job="${jobId}">
    <select class="ctl sm" aria-label="当前帧保存位置">
      <option value="next" ${d.next_shot_id ? '' : 'disabled'}>写进下一镜首帧</option>
      <option value="self">存为本镜尾帧</option>
      <option value="save" selected>仅截取保存</option>
    </select>
    <button type="button" class="btn xs" id="sw-extract-current${suffix}">📷 截取当前帧</button>
    ${saved ? `<a class="btn xs" href="${escapeHtml(savedFrame.url)}" download>下载已截帧</a>` : ''}
  </div>`;
}

function stageVideoIndex(d) {
  return d.preview.formal_video ? videoArtifactIndex(d.preview.formal_video) : -1;
}

function previewStageHtml(d) {
  const s = d.setting;
  const formal = d.preview.formal_video;
  const videoIndex = stageVideoIndex(d);
  const plannedMs = Math.max(1000, Math.round((Number(s.duration) || 5) * 1000));
  const trimIn = Math.max(0, Number(s.trim_in_ms) || 0);
  const trimOut = Math.max(0, Number(s.trim_out_ms) || 0);
  const trans = s.transition || 'cut';
  const transOpts = Object.entries(TRANSITION_LABELS).map(([k, lbl]) =>
    `<option value="${k}"${k === trans ? ' selected' : ''}>${lbl}</option>`).join('');

  return `<div class="wb-spotlight-stage">
    <div class="wb-spotlight-head">
      <span>${formal
        ? `正式成片 · 作业 #${formal.job_id} · ${escapeHtml(formal.provider)}／${escapeHtml(formal.model)} · 第 ${formal.attempt} 次尝试`
        : '暗场监看舞台 · 尚未签字正式视频'}</span>
      <span class="chip blue">${escapeHtml(TRANSITION_LABELS[trans] || trans)}</span>
    </div>
    ${videoIndex >= 0
      ? `<video id="sw-stage-player" class="wb-spotlight-player" controls preload="metadata" src="${api.jobArtifactUrl(formal.job_id, videoIndex)}"></video>`
      : formal ? '<div class="wb-spotlight-empty">正式视频文件不可播放。</div>'
      : '<div class="wb-spotlight-empty">还没有签过通过的正式视频，预览区留空——不拿未签字的产物冒充正式结果；下方仍可预设粗剪出入点与镜头转场。</div>'}
    ${formal ? `<div class="kv-grid">
      <div class="kv"><dt>参数快照</dt><dd class="mono t-desc">${escapeHtml(formal.params || '{}')}</dd></div>
      ${formal.retry_of_job_id ? `<div class="kv"><dt>重试来源</dt><dd>#${formal.retry_of_job_id}</dd></div>` : ''}
    </div>` : ''}
    <div class="wb-trim-scrubber" id="sw-trim-scrubber" data-planned-ms="${plannedMs}">
      <div class="wb-trim-bar">
        <span id="sw-trim-readout">入点 ${fmtMsSec(trimIn)} · 出点 ${trimOut > 0 ? fmtMsSec(trimOut) : '至片尾'}</span>
        ${formal ? `<div class="row u-gap-6">
          ${videoIndex >= 0 ? '<button type="button" class="btn xs" id="sw-trim-play">▶ 试播剪辑段</button>' : ''}
          <button type="button" class="btn xs primary" id="sw-extract-tail"
            data-seq="${d.setting.seq}" data-has-next="${d.next_shot_id ? '1' : '0'}"
            title="${d.next_shot_id ? '从正式视频末帧抽帧并写入下一镜首帧槽位' : '已是本集最后一镜，抽帧存为当前镜头尾帧'}">📸 截尾帧给下一镜</button>
          ${videoIndex >= 0 ? frameCaptureControls(d, 'stage', formal.job_id) : ''}
        </div>` : ''}
      </div>
      <div class="wb-trim-track">
        <div class="wb-trim-window" id="sw-trim-win"></div>
        <div class="wb-trim-playhead" id="sw-trim-playhead" hidden></div>
      </div>
      <div class="wb-trim-ranges">
        <label class="wb-trim-field">入点游标 (In)
          <input type="range" class="wb-trim-range" id="sw-trim-in-range" min="0" max="${plannedMs}" step="100" value="${Math.min(trimIn, plannedMs)}">
        </label>
        <label class="wb-trim-field">出点游标 (Out, 拉满=至片尾)
          <input type="range" class="wb-trim-range" id="sw-trim-out-range" min="0" max="${plannedMs}" step="100" value="${trimOut > 0 ? Math.min(trimOut, plannedMs) : plannedMs}">
        </label>
      </div>
      <div class="wb-trim-controls">
        <label class="wb-trim-field">入点 (ms)
          <input class="ctl sm" id="sw-trim-in-num" type="number" min="0" step="100" value="${trimIn}">
        </label>
        <label class="wb-trim-field">出点 (ms, 0=至片尾)
          <input class="ctl sm" id="sw-trim-out-num" type="number" min="0" step="100" value="${trimOut}">
        </label>
        <label class="wb-trim-field">入镜转场
          <select class="ctl sm" id="sw-trim-trans">${transOpts}</select>
        </label>
      </div>
      <div class="row">
        <button type="button" class="btn sm primary" id="sw-trim-save">保存粗剪与转场</button>
        <span class="modal-err" id="sw-trim-err"></span>
      </div>
    </div>
  </div>`;
}

function settingForm(d) {
  const s = d.setting;
  const row = (label, key, hint) => `
    <div class="kv"><dt>${label}</dt><dd>${escapeHtml(s[key] || '未填')}
      ${hint ? `<div class="hint">${hint}</div>` : ''}</dd></div>`;
  const trimText = `${fmtMsSec(s.trim_in_ms)} → ${Number(s.trim_out_ms) > 0 ? fmtMsSec(s.trim_out_ms) : '片尾'}`;
  const transText = TRANSITION_LABELS[s.transition] || s.transition || '硬切 (Cut)';
  const firstImg = String(s.first_frame_image || '').trim();
  const lastImg = String(s.last_frame_image || '').trim();
  const slotBox = (label, val, field) => `
    <div class="wb-frame-slot">
      <div class="wb-frame-head">
        <span>${label}</span>
        ${val ? `<button type="button" class="btn xs ghost" data-clear-frame="${field}">清空</button>` : '<span class="hint">未指定</span>'}
      </div>
      <div class="wb-frame-thumb">
        ${val ? `<img src="${escapeHtml(frameSrc(val))}" alt="${escapeHtml(label)}">`
              : `<span>${field === 'first_frame_image' ? '默认采用关键帧或引用图' : '可选尾帧控制，生视频双帧锚定'}</span>`}
      </div>
      ${val ? `<span class="hint mono">${escapeHtml(val)}</span>` : ''}
    </div>`;
  return `
  <div class="kv-grid">
    ${row('镜头编号', 'seq')}
    ${row('时长（秒）', 'duration')}
    ${row('景别', 'shot_size', '今天只有工作台在显示它，执行器不读；填了不会改变画面')}
    ${row('运镜', 'camera_move')}
    ${row('画面比例', 'aspect')}
    ${row('镜头风格', 'style')}
    <div class="kv"><dt>粗剪区间</dt><dd class="mono">${trimText}</dd></div>
    <div class="kv"><dt>入镜转场</dt><dd>${escapeHtml(transText)}</dd></div>
  </div>
  <h3 class="drawer-h">首尾帧连线（图生视频首尾帧引导）</h3>
  <div class="wb-frame-bridge">
    ${slotBox('首帧 (First Frame)', firstImg, 'first_frame_image')}
    <span class="wb-frame-arrow" aria-hidden="true">首帧 ──🎬──► 尾帧</span>
    ${slotBox('尾帧 (Last Frame)', lastImg, 'last_frame_image')}
  </div>
  <h3 class="drawer-h">画面描述与提示词</h3>
  <div class="kv-grid">
    <div class="kv"><dt>内容摘要</dt><dd class="t-desc">${escapeHtml(s.summary)}</dd></div>
    <div class="kv"><dt>生成提示词</dt><dd class="t-desc">${escapeHtml(s.prompt || '未填')}</dd></div>
    <div class="kv"><dt>负面提示词</dt><dd class="t-desc">${escapeHtml(s.negative_prompt || '未填')}</dd></div>
  </div>`;
}

function refsBlock(d) {
  const r = d.refs;
  const assets = r.assets.length ? r.assets.map((a) => `
    <div class="kv"><dt>${escapeHtml(a.name)}
      <div class="hint">${escapeHtml(a.kind)}</div></dt>
    <dd>${a.images ? chip('plain', `${a.images} 张参考图`) : chip('amber', '没有参考图')}
      ${a.consistency_prompt ? '' : chip('amber', '没有一致性提示词')}
      <button class="btn xs" data-asset="${a.id}">看这条资产</button></dd></div>`).join('')
    : '<p class="hint">这一镜没有引用任何资产——人物与场景一致性没有依据。</p>';
  const vs = r.variants.length ? r.variants.map((v) => `
    <div class="kv"><dt>${escapeHtml(v.parent_name || '形态')} · ${escapeHtml(v.name)}
      <div class="hint">${escapeHtml(v.variant_type)}</div></dt>
    <dd>${v.images ? chip('plain', `${v.images} 张形态图`) : chip('amber', '形态没有图')}</dd></div>`).join('')
    : '<p class="hint">没有指定形态（人物的哪套衣服）。</p>';
  const missing = [...r.missing_assets.map((x) => `#${x}`), ...r.missing_variants.map((x) => `形态#${x}`)];
  return `
  <h3 class="drawer-h">人物／场景／道具</h3>${assets}
  ${missing.length ? `<p class="modal-err">引用了不存在的东西：${missing.join('、')}——这是阻塞项，提交生成会被拦。</p>` : ''}
  <h3 class="drawer-h">形态</h3>${vs}`;
}

/** 音频这条腿"现在到底能不能出声"——按后端给的三个事实说，不硬编码。
 *
 *  这里曾经写死"配音与音乐今天只能登记，不能真出声……执行器还没接线"。
 *  那句话在 2026-11-05 早于配音接线（`runners._voxcpm`），接线之后它就成了
 *  对用户说的假话：明明能提交配音，界面还在说不能。更麻烦的是没人会去试，
 *  所以这种"把能用的说成不能用"能活很久。
 *
 *  `runner_wired` 由服务端问 `runners.has_branch()` 得到（`production._audio_wired`），
 *  前端只负责把它翻译成人话——两边不各写一份真相。
 */
function audioWiringNote(engines) {
  const tts = engines?.tts || {};
  const music = engines?.music || {};
  const ok = (e) => Boolean(e.provider) && Boolean(e.runner_wired);
  if (ok(tts) && ok(music)) {
    return `<b>配音与音乐都能提交执行。</b>口型（Rhubarb）仍未接入，这一页不给口型按钮。`;
  }
  if (ok(tts)) {
    return `<b>配音能提交执行</b>（默认轨 ${escapeHtml(tts.provider)}）；
      音乐这一格${music.provider ? '执行器没有这一条分支' : '默认轨还没配'}，暂时只能登记素材。
      口型（Rhubarb）未接入。`;
  }
  if (tts.provider) {
    return `<b>配音默认轨配了，但执行器没有这一条分支</b>，只能登记素材。口型（Rhubarb）未接入。`;
  }
  return `<b>音频执行这一腿还没接线</b>：能登记台词与素材归属，暂时不能真出声。口型（Rhubarb）也未接入。`;
}

function audioBlock(d) {
  const clips = d.audio.clips;
  const engines = d.audio.engines;
  const rows = clips.length ? `<div class="table-wrap"><table>
    <thead><tr><th>类型</th><th>说话人</th><th>文本</th><th>状态</th><th>时长</th></tr></thead>
    <tbody>${clips.map((c) => `<tr><td>${escapeHtml(c.kind)}</td>
      <td>${escapeHtml(c.speaker || '—')}</td>
      <td><span class="t-desc">${escapeHtml((c.text || '').slice(0, 60))}</span></td>
      <td>${chip(c.status === 'ready' ? 'green' : c.status === 'failed' ? 'red' : 'gray',
        CLIP_LABEL[c.status] || c.status)}</td>
      <td class="hint">${c.duration_ms ? Math.round(c.duration_ms / 100) / 10 + 's' : '—'}</td></tr>`).join('')}
    </tbody></table></div>` : '<p class="hint">这一镜还没有台词／旁白素材。</p>';
  return `
  <p class="note">${audioWiringNote(engines)}</p>
  <p><button class="btn xs" data-goto='{"module":"audio","sub":0}'>去音频中心登记素材</button></p>
  ${rows}`;
}


function reviewNoteHtml(note) {
  const marks = /\[(?:v#(\d+)\s+)?(\d+(?:\.\d+)?)s(?:\s*[-–]\s*(\d+(?:\.\d+)?)s)?\]/g;
  let html = '';
  let cursor = 0;
  for (const match of note.matchAll(marks)) {
    const [token, job, start, end] = match;
    if (!Number.isFinite(Number(start)) || (end && (!Number.isFinite(Number(end)) || Number(end) <= Number(start)))) continue;
    html += escapeHtml(note.slice(cursor, match.index));
    html += `<button type="button" class="chip plain" data-note-play="${start}"
      ${end ? `data-note-end="${end}"` : ''}${job ? ` data-note-job="${job}" title="视频版本 #${job}"` : ''}>▶ ${start}s${end ? `–${end}s` : ''}</button>`;
    cursor = match.index + token.length;
  }
  return html + escapeHtml(note.slice(cursor));
}

/** ⑦ 审核这一段：待签作业、就地签字、以及共用的质量清单面板。 */
function reviewBlock(d, target, checklist, missing) {
  const candidates = [...d.videos.jobs, ...d.keyframes.jobs].filter((job) => job.status === 'succeeded');
  const note = reviewDraft && reviewDraft.jobId === target?.job_id ? reviewDraft.note : '';
  const canSign = store.can('review:sign');
  const sign = target?.sign;
  const playable = target?.capability === 'video' && videoArtifactIndex(target) >= 0;
  const history = (d.review.signs || []).filter((item) => item.note
    && !(item.job_id === target?.job_id && item.signed_at === sign?.signed_at && item.note === sign?.note));
  const rework = ['returned', 'rejected'].includes(sign?.decision) && store.can('job:submit');
  const savedDraft = rework && readReworkDraft(d.project_id, d.episode_id, d.shot_id, target.job_id);
  const sourceUnavailable = target?.rework_of_job_id && target.capability === 'video'
    && !d.videos.jobs.some((job) => job.job_id === target.rework_of_job_id && videoArtifactIndex(job) >= 0);
  return `
    ${missing ? '<p class="bar warn">指定版本已不在该镜头，未选择其他版本。</p>' : ''}
    ${candidates.length ? `<label>审核版本 <select class="ctl" id="sw-review-version">
      <option value="" disabled ${target ? '' : 'selected'}>选择一个版本</option>
      ${candidates.map((j) => `<option value="${j.job_id}" ${target?.job_id === j.job_id ? 'selected' : ''}
        ${j.capability === 'video' && videoArtifactIndex(j) >= 0 ? 'data-playable' : ''}>
        #${j.job_id} · ${j.capability === 'image' ? '关键帧' : '视频'} · ${escapeHtml(j.model)}</option>`).join('')}
    </select></label>` : '<p class="hint">还没有可审核的产物。</p>'}
    ${target ? `<div id="sw-review-target" data-review-job="${target.job_id}">
      <div class="sw-review-preview">${artifactPreview(target)}</div>
      ${playable ? frameCaptureControls(d, 'review', target.job_id, stageVideoIndex(d) >= 0 ? '-review' : '') : ''}
      ${sign?.note ? `<p class="sw-review-note">审核意见 · ${reviewNoteHtml(sign.note)}</p>` : ''}
      ${sourceUnavailable ? '<p class="hint">返工来源视频不可播放，可继续审核新版。</p>' : ''}
      <div class="actions">
        ${target.capability === 'video' && d.videos.jobs.filter((j) => videoArtifactIndex(j) >= 0).length > 1 ? '<button type="button" class="btn" id="sw-ab-review">对比这一版</button>' : ''}
        ${rework ? `<button type="button" class="btn primary" id="sw-rework">${savedDraft ? '继续返工草稿' : '按此意见返工'}</button>` : ''}
      </div>
      ${checklistHtml(checklist)}
      ${canSign ? `<label for="sw-review-note">审核意见</label>
        <textarea class="ctl" id="sw-review-note" maxlength="500" rows="2">${escapeHtml(note)}</textarea>
        <div class="actions">
          ${playable ? `<button class="btn xs" type="button" id="sw-note-time">插入当前播放时间</button>
            <button class="btn xs" type="button" id="sw-note-range">设为起点</button>
            <span class="hint" id="sw-note-range-status" role="status"></span>` : ''}
          <button class="btn xs primary" data-jsign="approved" ${checklist?.can_approve ? '' : 'disabled'}>通过</button>
          <button class="btn xs" data-jsign="returned">退回</button>
          <button class="btn xs danger" data-jsign="rejected">驳回</button>
        </div>` : ''}
    </div>` : ''}
    ${history.length ? `<details><summary>历史审核意见</summary>${history.map((item) =>
      `<p class="sw-review-note"><span class="hint">#${item.job_id} · ${escapeHtml(item.signed_by)} · ${escapeHtml(item.signed_at)}</span> ${reviewNoteHtml(item.note)}</p>`).join('')}</details>` : ''}`;
}

function readyBlock(d) {
  const r = d.compose_ready;
  const line = (ok, text, extra = '') => `<li>${ok ? chip('green', '满足') : chip('red', '不满足')} ${escapeHtml(text)}${extra ? ` — ${escapeHtml(extra)}` : ''}</li>`;
  const blockers = (r.blockers || []).map((b) => `<li>${chip('red', '阻塞')} ${escapeHtml(b.text)}
    ${b.locate && b.locate.module
      && !(b.locate.module === 'shot' && Number(b.locate.shot_id) === Number(d.shot_id))
      ? `<button class="btn xs" data-goto="${escapeHtml(JSON.stringify(b.locate))}">去处理</button>`
      : ''}</li>`).join('');
  return `
  <ul class="hint flush" >
    ${line(r.video_ok, '有一条签过通过的正式视频')}
    ${line(r.source_ok, '源版本与最新分镜一致', r.source_ok ? '' : '产物是按旧版分镜生的，建议重新生成')}
    ${line(r.audio_ok, '音频素材没有欠账', r.audio_note)}
    ${line(!r.blockers.length, '生成前预检没有阻塞项')}
  </ul>
  ${blockers ? `<ul class="hint" style="margin:8px 0 0">${blockers}</ul>` : ''}
  <p class="note" style="margin-top:10px"><b>这一栏说"可以"，指的是这一镜能进成片时间线。</b>
    整集还差什么（顺序、字幕、字体、磁盘）在成片中心的合成前检查里算，两处用的是同一份规则。</p>`;
}

function picker(shots, currentId) {
  return `<div class="card"><div class="card-head"><h2>选一个镜头</h2>
    <span class="sub">工作台按集列镜头；点一行进去就是那一镜的生产档案</span></div>
    <div class="table-wrap"><table>
      <thead><tr><th>镜号</th><th>内容</th><th>时长</th><th></th></tr></thead>
      <tbody>${shots.map((s) => `<tr>
        <td class="t-strong">${s.seq}</td>
        <td><span class="t-desc">${escapeHtml((s.summary || '').slice(0, 60))}</span></td>
        <td class="hint">${s.duration}s</td>
        <td><button class="btn xs" data-open-shot="${s.id}">打开工作台</button></td></tr>`).join('')}
      </tbody></table></div></div>`;
}

export default {
  async render() {
    const project = store.current;
    if (!project) {
      return `<section class="view on"><div class="page-head"><h1>镜头工作台</h1>
        <p>先选项目</p></div><div class="card"><div class="empty">
        <div class="empty-t">还没有项目</div>
        <p>在「项目中心」新建一个项目，镜头工作台才有可排的对象。</p></div></div></section>`;
    }
    const shotId = getParam('shot');
    if (!shotId) {
      let shots = [];
      if (store.currentEpisodeId) {
        try { shots = await api.shots(store.currentEpisodeId); } catch { shots = []; }
      }
      return `<section class="view on"><div class="page-head"><h1>镜头工作台</h1>
        <p>${escapeHtml(project.name)}${store.currentEpisode ? ` · 第 ${store.currentEpisode.seq} 集` : ''}</p></div>
        ${store.currentEpisodeId ? picker(shots, shotId)
          : '<div class="card"><div class="empty"><div class="empty-t">还没选集</div><p>在顶栏或项目工作台选一集，这里才列镜头。</p></div></div>'}
        </section>`;
    }
    let d;
    let entry = getShotEntry()?.shotId === Number(shotId) ? getShotEntry() : null;
    if (!entry) {
      const episodeId = store.currentEpisodeId;
      const cursor = taskCursor(project.id, episodeId);
      if (cursor?.shot_id === Number(shotId)) {
        try {
          const { board } = await api.production(project.id, episodeId, true);
          if (getParam('shot') === shotId && store.currentId === project.id
              && store.currentEpisodeId === episodeId && !getShotEntry()) {
            const current = board?.shot_tasks.find((task) => task.group === cursor.group
              && task.shot_id === cursor.shot_id);
            entry = { shotId: Number(shotId), work: cursor, jobId: current?.job_id,
              focus: current ? shotTaskFocus(current) : '', pendingFocus: true };
            setShotEntry(entry);
          }
        } catch { /* 工作台其余部分独立可读 */ }
      }
    }
    try {
      d = await api.shotWorkbench(shotId, entry?.jobId);
      currentShotData = d;
    } catch (err) {
      currentShotData = null;
      return `<section class="view on"><div class="page-head"><h1>镜头工作台</h1></div>
        <div class="card"><div class="empty"><div class="empty-t">读不到这一镜</div>
        <p>${escapeHtml(err.message || '')}</p>
        <button class="btn" id="sw-back">回到分镜列表</button></div></div></section>`;
    }
    // ⑦ 就地审核（任务书 §12.1「视频结果直接提交审核」）：
    // 待签的产物就摆在镜头详情里，不用再跑一趟审核中心。清单读不到时只少一块面板，
    // 不挡住这一镜的其余七段。
    const work = entry?.work?.episodeId === d.episode_id && entry.work.projectId === d.project_id
      && d.project_id === store.currentId && d.episode_id === store.currentEpisodeId ? entry.work : null;
    const wanted = entry?.jobId ?? d.review.pending[0]?.job_id;
    const reviewTarget = [...d.videos.jobs, ...d.keyframes.jobs]
      .find((job) => job.job_id === wanted && job.status === 'succeeded');
    const reviewing = ['review', 'compare'].includes(entry?.focus);
    const directed = reviewing || entry?.focus === 'assets' || entry?.focus === 'job';
    const currentJob = entry?.focus === 'job' ? d.videos.jobs.find((job) => job.job_id === entry.jobId) : null;
    let cl = null;
    if (reviewTarget && store.can('review:sign')) {
      try { cl = await api.jobChecklist(reviewTarget.job_id); } catch { cl = null; }
    }

    const head = `<div class="page-head">
      <h1>第 ${d.setting.seq} 镜</h1>
      <p>${escapeHtml((d.setting.summary || '').slice(0, 80))}</p>
      <div class="right">
        <button class="btn" id="sw-back">${work ? '返回本集待办' : entry?.reviewReturn ? '返回审核队列' : '分镜表'}</button>
        <button class="btn" id="sw-copy-video-prompt" title="一键复制带景别、运镜、时长与引用角色一致性的提示词，可直接用于 Google Flow (Veo) 或 ChatGPT Sora 生成高光视频镜头">🎬 复制 Flow/Sora 提示词</button>
        <button class="btn" id="sw-edit">编辑设定</button>
        <button class="btn${reviewing ? '' : ' primary'}" id="sw-generate">提交生成</button>
      </div></div>`;

    const section = (title, body) => `<div class="card u-mb-12">
      <div class="card-head"><h2>${title}</h2></div>
      <div class="card-body">${body}</div></div>`;

    const review = section('审核版本', reviewBlock(d, reviewTarget, cl, entry?.jobId && !reviewTarget));
    const references = section('资产引用', refsBlock(d));
    return `<section class="view on" data-shot-project="${d.project_id}" data-shot-episode="${d.episode_id}">${head}
      ${work ? `<div class="actions sw-task-actions">
        <strong>${SHOT_TASK_GROUPS[work.group]} · 第 ${work.seq} 镜</strong>
        <button type="button" class="btn primary" id="sw-next-task">继续下一镜</button>
        <span role="status" id="sw-task-status"></span></div>` : ''}
      ${currentJob ? section('当前作业', `<p>#${currentJob.job_id} · ${jobChip(currentJob)}</p>
        <button type="button" class="btn" data-task-job="${currentJob.job_id}">查看作业与诊断</button>`) : ''}
      ${reviewing ? review : ''}
      ${entry?.focus === 'assets' ? references + section('首尾帧与镜头设定', settingForm(d)) : ''}
      ${directed ? '<details class="sw-other-sections"><summary>展开完整镜头档案</summary>' : ''}
      ${entry?.focus !== 'assets' ? section('镜头设定', settingForm(d)) + references : ''}
      ${section(`关键帧（${d.keyframes.jobs.length} 条记录）`, jobRows(d.keyframes.jobs, d.keyframes.formal_job_id)
        + '<p class="hint">「正式」＝最新一条签过通过的关键帧作业。要换正式结果，去审核中心签另一条。</p>')}
      ${section(`视频（${d.videos.jobs.length} 条记录）`, (() => {
        const candidates = (d.videos.jobs || []).filter(
          (j) => videoArtifactIndex(j) >= 0,
        );
        const abToolbar = candidates.length >= 2
          ? `<div class="actions"><button class="btn sm primary" type="button" id="sw-ab-compare">🎬 同镜 A/B 同步对比（${candidates.length} 候选）</button></div>`
          : '';
        return abToolbar
          + jobRows(d.videos.jobs, d.videos.formal_job_id)
          + (d.videos.source_stale ? '<p class="modal-err">这一镜的分镜或资产在生成之后改过，最新产物可能已过期。</p>' : '');
      })())}
      ${section('音频', audioBlock(d))}
      ${section('预览与粗剪', previewStageHtml(d))}
      ${!reviewing ? review : ''}
      ${section('生产完成条件', readyBlock(d))}
      ${directed ? '</details>' : ''}
      <div class="modal" id="sw-modal" hidden><div class="modal-box">
        <div class="modal-head neutral">编辑镜头设定</div>
        <div class="modal-body"><div class="form-grid">
          <label for="sw-summary">内容摘要</label>
          <input class="ctl" id="sw-summary" maxlength="500" value="${escapeHtml(d.setting.summary)}">
          <label for="sw-prompt">生成提示词</label>
          <input class="ctl" id="sw-prompt" maxlength="2000" value="${escapeHtml(d.setting.prompt)}">
          <label for="sw-duration">时长（秒）</label>
          <input class="ctl" id="sw-duration" type="number" min="1" max="60" step="0.5" value="${d.setting.duration}">
          <label for="sw-size">景别</label>
          <input class="ctl" id="sw-size" maxlength="40" value="${escapeHtml(d.setting.shot_size)}">
          <label for="sw-move">运镜</label>
          <input class="ctl" id="sw-move" list="sw-camera-moves" maxlength="40" value="${escapeHtml(d.setting.camera_move)}" placeholder="如：推镜 / 环绕">
          <datalist id="sw-camera-moves">${CAMERA_MOVE_OPTIONS.map((m) => `<option value="${m}">`).join('')}</datalist>
          <label for="sw-aspect">画面比例</label>
          <input class="ctl" id="sw-aspect" maxlength="20" value="${escapeHtml(d.setting.aspect)}">
          <label for="sw-style">镜头风格</label>
          <input class="ctl" id="sw-style" maxlength="60" value="${escapeHtml(d.setting.style)}">
          <label for="sw-neg">负面提示词</label>
          <input class="ctl" id="sw-neg" maxlength="2000" value="${escapeHtml(d.setting.negative_prompt)}">
          <label for="sw-first-frame">首帧参考路径</label>
          <input class="ctl" id="sw-first-frame" maxlength="500" value="${escapeHtml(d.setting.first_frame_image || '')}" placeholder="如 shots/1/xxx.png（可截上一镜尾帧自动写入）">
          <label for="sw-last-frame">尾帧参考路径</label>
          <input class="ctl" id="sw-last-frame" maxlength="500" value="${escapeHtml(d.setting.last_frame_image || '')}" placeholder="可选，图生视频双帧首尾控制">
          <label for="sw-trim-in">入点 (ms)</label>
          <input class="ctl" id="sw-trim-in" type="number" min="0" step="100" value="${Number(d.setting.trim_in_ms || 0)}">
          <label for="sw-trim-out">出点 (ms, 0=至片尾)</label>
          <input class="ctl" id="sw-trim-out" type="number" min="0" step="100" value="${Number(d.setting.trim_out_ms || 0)}">
          <label for="sw-transition">入镜转场</label>
          <select class="ctl" id="sw-transition">${Object.entries(TRANSITION_LABELS).map(([k, lbl]) =>
            `<option value="${k}"${k === (d.setting.transition || 'cut') ? ' selected' : ''}>${lbl}</option>`).join('')}</select>
        </div>
        <p class="modal-meta">景别／运镜／比例／风格／负面提示词这几栏今天只是生产语言：
          执行器不读它们，改了也不会让已生成的产物判过期。要改引用资产请回分镜表。</p>
        <p class="modal-err" id="sw-error"></p></div>
        <div class="modal-actions"><button class="btn" data-close="sw-modal">取消</button>
          <button class="btn primary" id="sw-save">保存设定</button></div>
      </div></div>
      ${(() => {
        const candidates = (d.videos.jobs || []).filter(
          (j) => videoArtifactIndex(j) >= 0,
        );
        if (candidates.length < 2) return '';
        const aDef = candidates.find((job) => job.job_id === reviewTarget?.job_id) || candidates[0];
        const bDef = candidates.find((job) => job.job_id === aDef.rework_of_job_id)
          || candidates.find((job) => job.job_id !== aDef.job_id);
        const optHtml = (selId) => candidates.map((j) => {
          const signTxt = j.sign?.decision === 'approved' ? ' · 已通过' : '';
          return `<option value="${j.job_id}" data-index="${videoArtifactIndex(j)}"${j.job_id === selId ? ' selected' : ''}>#${j.job_id} · ${escapeHtml(j.provider)}/${escapeHtml(j.model)}${signTxt}</option>`;
        }).join('');
        return `<div class="modal" id="sw-ab-modal"
          data-compares-source="${Boolean(aDef.rework_of_job_id && bDef.job_id === aDef.rework_of_job_id)}" hidden><div class="modal-box wb-ab-box">
          <div class="modal-head neutral">同镜多候选视频 A/B 同步对比</div>
          <div class="modal-body">
            <div class="wb-ab-grid">
              <div class="wb-ab-pane">
                <label class="hint" for="sw-ab-sel-a">候选 A</label>
                <select class="ctl sm" id="sw-ab-sel-a">${optHtml(aDef.job_id)}</select>
                <video id="sw-ab-vid-a" controls preload="none" src="${api.jobArtifactUrl(aDef.job_id, videoArtifactIndex(aDef))}"></video>
              </div>
              <div class="wb-ab-pane">
                <label class="hint" for="sw-ab-sel-b">候选 B</label>
                <select class="ctl sm" id="sw-ab-sel-b">${optHtml(bDef.job_id)}</select>
                <video id="sw-ab-vid-b" controls preload="none" src="${api.jobArtifactUrl(bDef.job_id, videoArtifactIndex(bDef))}"></video>
              </div>
            </div>
            <div class="wb-ab-bar">
              <button class="btn sm primary" type="button" id="sw-ab-toggle">▶ 同步播放</button>
              <button class="btn sm" type="button" id="sw-ab-reset">⏮ 归零</button>
              <input type="range" id="sw-ab-seek" min="0" max="10" step="0.05" value="0">
              <span class="mono hint" id="sw-ab-time">0.0s / 0.0s</span>
              <select class="ctl sm" id="sw-ab-rate">
                <option value="0.5">0.5x</option>
                <option value="1" selected>1.0x</option>
                <option value="1.5">1.5x</option>
              </select>
            </div>
          </div>
          <div class="modal-actions">
            <button class="btn" type="button" id="sw-ab-close">关闭</button>
          </div>
        </div></div>`;
      })()}
    </section>`;
  },

  mount(root) {
    const go = (entry) => {
      gotoEntry(entry);
    };
    const entry = getShotEntry();
    if (entry?.pendingFocus && entry.shotId === Number(getParam('shot'))) {
      setShotEntry({ ...entry, pendingFocus: false });
      const page = root.firstElementChild;
      requestAnimationFrame(() => {
        if (!page?.isConnected) return;
        root.scrollTop = 0;
        window.scrollTo(0, 0);
      });
    }
    const page = root.firstElementChild;
    const projectId = Number(page?.dataset.shotProject);
    const episodeId = Number(page?.dataset.shotEpisode);
    root.querySelector('#sw-next-task')?.addEventListener('click', async (event) => {
      const btn = event.currentTarget;
      const work = getShotEntry()?.work;
      if (!work || work.projectId !== store.currentId || work.episodeId !== store.currentEpisodeId) return;
      let board;
      const ok = await run('刷新本集待办', async () => {
        ({ board } = await api.production(work.projectId, work.episodeId, true));
      }, { btn });
      if (!ok || !btn.isConnected || work.projectId !== store.currentId
          || work.episodeId !== store.currentEpisodeId) return;
      const next = taskAtCursor(board.shot_tasks, work, { next: true });
      if (next) openShotTask(next, work.projectId, work.episodeId);
      else {
        root.querySelector('#sw-task-status').textContent = '已到这组末尾，可返回本集待办查看剩余项。';
      }
    });
    root.querySelector('[data-task-job]')?.addEventListener('click', (event) => {
      go({ module: 'generate', job_id: Number(event.currentTarget.dataset.taskJob) });
    });
    const reviewHost = root.querySelector('#sw-review-target');
    const reviewVersions = root.querySelector('#sw-review-version');
    const jobId = Number(reviewHost?.dataset.reviewJob);
    const reviewVideo = reviewHost?.querySelector('video');
    const stageVideo = root.querySelector('#sw-stage-player');
    const players = [reviewVideo, stageVideo].filter(Boolean);
    const segmentEnds = new Map();
    for (const player of players) {
      player.addEventListener('timeupdate', () => {
        const end = segmentEnds.get(player);
        if (end == null || player.seeking || player.currentTime < end) return;
        segmentEnds.delete(player);
        player.pause();
        player.currentTime = end;
      });
      player.addEventListener('ended', () => segmentEnds.delete(player));
    }
    const playSegment = (player, start, end) => {
      if (!player || player.error) {
        shotToast('当前没有可播放的视频');
        return;
      }
      for (const other of players) {
        if (other !== player) other.pause();
      }
      segmentEnds.set(player, end);
      player.currentTime = start;
      player.play().catch((error) => {
        if (error.name !== 'AbortError' && player.isConnected) shotToast('无法自动播放，请点击视频播放器的播放按钮');
      });
    };
    const playReviewMark = (player, start, end) => {
      const details = player?.closest('details');
      if (details) details.open = true;
      playSegment(player, start, end);
      player?.scrollIntoView({ block: 'nearest' });
    };
    const selectReviewVersion = (version, playback = null) => {
      setShotEntry({ ...getShotEntry(), shotId: Number(getParam('shot')),
        focus: 'review', jobId: version, playback });
      renderView('shot');
    };
    root.querySelectorAll('[data-note-play]').forEach((pill) => pill.addEventListener('click', () => {
      const version = Number(pill.dataset.noteJob);
      const start = Number(pill.dataset.notePlay);
      const end = pill.dataset.noteEnd ? Number(pill.dataset.noteEnd) : null;
      if (version && version !== jobId && reviewVersions?.querySelector(`option[value="${version}"][data-playable]`)) {
        selectReviewVersion(version, { start, end });
      } else {
        playReviewMark(reviewVideo && !reviewVideo.error ? reviewVideo : stageVideo, start, end);
      }
    }));
    if (entry?.playback && entry.shotId === Number(getParam('shot'))) {
      setShotEntry({ ...getShotEntry(), playback: null });
      const { start, end } = entry.playback;
      playSegment(reviewVideo && !reviewVideo.error ? reviewVideo : stageVideo, start, end);
      requestAnimationFrame(() => {
        if (reviewVideo?.isConnected) reviewVideo.scrollIntoView({ block: 'nearest' });
      });
    }
    const note = reviewHost?.querySelector('#sw-review-note');
    const saveDraft = () => { reviewDraft = { jobId, note: note.value }; };
    note?.addEventListener('input', saveDraft);
    reviewVersions?.addEventListener('change', (event) => {
      if (event.target.value) selectReviewVersion(Number(event.target.value));
    });
    const insertMark = (start, end = '') => {
      const mark = `[v#${jobId} ${start}s${end ? `-${end}s` : ''}] `;
      if (note.value.length + mark.length > note.maxLength) {
        shotToast('审核意见空间不足，请缩短后再插入时间标记');
        return false;
      }
      note.setRangeText(mark, 0, 0, 'end');
      saveDraft();
      note.focus();
      return true;
    };
    root.querySelector('#sw-note-time')?.addEventListener('click', () => {
      insertMark(reviewVideo.currentTime.toFixed(2));
    });
    const rangeStatus = root.querySelector('#sw-note-range-status');
    let rangeStart = null;
    root.querySelector('#sw-note-range')?.addEventListener('click', (event) => {
      const button = event.currentTarget;
      const time = reviewVideo.currentTime.toFixed(2);
      if (rangeStart === null) {
        rangeStart = time;
        button.textContent = '标记区间';
        rangeStatus.textContent = `起点 ${time}s`;
      } else if (Number(time) <= Number(rangeStart)) {
        rangeStatus.textContent = `起点 ${rangeStart}s · 请将播放头移到起点之后`;
      } else if (insertMark(rangeStart, time)) {
        rangeStart = null;
        button.textContent = '设为起点';
        rangeStatus.textContent = '';
      }
    });
    reviewHost?.querySelectorAll('[data-check]').forEach((b) => b.addEventListener('click', async () => {
      const ok = await run('记录清单', () => api.saveJobChecklist(jobId, {
        items: [{ code: b.dataset.check, state: b.dataset.state, note: '' }],
      }), { btn: b });
      if (ok && b.isConnected) renderView('shot');
    }));
    reviewHost?.querySelectorAll('[data-jsign]').forEach((b) => b.addEventListener('click', async () => {
      const ok = await run('签字', () => api.jobReviewSign(jobId, {
        anchor: 'artifact', decision: b.dataset.jsign, note: note?.value.trim() || '',
      }), { btn: b });
      if (ok && b.isConnected) {
        reviewDraft = null;
        setShotEntry({ ...getShotEntry(), shotId: Number(getParam('shot')), focus: 'review', jobId });
        renderView('shot');
      }
    }));
    root.querySelector('#sw-rework')?.addEventListener('click', async (event) => {
      const btn = event.currentTarget;
      const shotId = Number(getParam('shot'));
      let prefill;
      const ok = await run('打开返工草稿', async () => {
        prefill = await reworkPrefill(projectId, episodeId, shotId, jobId);
      }, { btn });
      if (!ok || !btn.isConnected || Number(getParam('shot')) !== shotId
          || store.currentId !== projectId) return;
      setJobPrefill(prefill);
      renderView('generate');
    });
    root.querySelectorAll('[data-goto]').forEach((b) => b.addEventListener('click', () => {
      let entry;
      try { entry = JSON.parse(b.dataset.goto); } catch { return; }
      go(entry);
    }));
    root.querySelectorAll('[data-open-shot]').forEach((b) => b.addEventListener('click', () => {
      setShotEntry(null);
      setParam('shot', Number(b.dataset.openShot));
      renderView('shot');
    }));
    root.querySelectorAll('[data-asset]').forEach((b) => b.addEventListener('click', () => {
      setSub('assets', 0);
      setParam('assets', Number(b.dataset.asset));
      renderView('assets');
    }));
    root.querySelector('#sw-back')?.addEventListener('click', () => {
      if (root.querySelector('#sw-next-task')) { renderView('production'); return; }
      const returnContext = getShotEntry()?.reviewReturn;
      if (returnContext) {
        setReviewReturn(returnContext);
        renderView('review');
        return;
      }
      setSub('writing', 2);
      renderView('writing');
    });
    root.querySelector('#sw-generate')?.addEventListener('click', async () => {
      const shotId = getParam('shot');
      // 提交前先跑一次统一预检：阻塞就不把人送去生成中心，
      // 因为那边同样会拦——让人白跑一趟比在这里多说一句贵得多。
      let check;
      try { check = await api.precheckShot(shotId); } catch (err) {
        window.dispatchEvent(new CustomEvent('app:toast', {
          detail: { text: '预检没跑成：' + (err.message || '未知错误'), kind: 'err' } }));
        return;
      }
      if (check.status === '阻塞') {
        window.dispatchEvent(new CustomEvent('app:toast', {
          detail: { text: `预检阻塞 ${check.blocking} 项：` + check.items
            .filter((i) => i.level === '阻塞').map((i) => i.text).join('；'),
            kind: 'warn' } }));
        return;
      }
      const firstFrameVal = root.querySelector('#sw-first-frame')?.value.trim() || '';
      const lastFrameVal = root.querySelector('#sw-last-frame')?.value.trim() || '';
      const prefillParams = {};
      if (firstFrameVal) prefillParams.image = firstFrameVal;
      if (lastFrameVal) prefillParams.last_frame_image = lastFrameVal;
      setJobPrefill({ projectId: store.currentId, episodeId: store.currentEpisodeId,
        shotId: Number(shotId), capability: 'video',
        // 同一镜的生成入口，场景键与分镜页那一处一致（`shot_video`）。
        sceneKey: 'shot_video',
        requireReferenceImage: Boolean(firstFrameVal || lastFrameVal),
        requireLastFrame: Boolean(lastFrameVal),
        prompt: (root.querySelector('#sw-prompt') || {}).value || '',
        params: Object.keys(prefillParams).length ? prefillParams : undefined });
      renderView('generate');
    });
    const editBtn = root.querySelector('#sw-edit');
    root.querySelector('#sw-copy-video-prompt')?.addEventListener('click', async () => {
      const s = currentShotData?.setting || {};
      const refs = currentShotData?.references || [];
      const refLines = refs
        .map((r) => `- ${r.name || '资产'}${r.consistency_prompt ? `：${r.consistency_prompt}` : ''}`)
        .filter(Boolean);
      const specParts = [
        s.aspect ? `画面比例 ${s.aspect}` : '',
        s.duration ? `时长约 ${s.duration} 秒` : '',
        s.shot_size ? `景别：${s.shot_size}` : '',
        s.camera_move ? `运镜：${s.camera_move}` : '',
        s.style ? `画风：${s.style}` : '',
      ].filter(Boolean);
      const text = [
        `【高光视频镜头提示词（适配 Google Flow Veo / ChatGPT Sora）】`,
        specParts.length ? `【镜头规格】${specParts.join(' · ')}` : '',
        s.summary ? `【剧情与动作】${s.summary}` : '',
        s.prompt ? `【核心画面提示词】${s.prompt}` : '',
        refLines.length ? `【登场角色/场景一致性】\n${refLines.join('\n')}` : '',
        s.negative_prompt ? `【避免出现】${s.negative_prompt}` : '',
        `【质量要求】保持2D高品质日系动画作画质感、角色五官与服饰全程稳定不崩、运动连贯自然、无文字水印。`,
      ].filter(Boolean).join('\n');
      try {
        await navigator.clipboard.writeText(text);
        window.dispatchEvent(new CustomEvent('app:toast', {
          detail: { text: '已复制 Flow (Veo) / Sora 高光镜头提示词到剪贴板', kind: 'ok' },
        }));
      } catch (err) {
        window.dispatchEvent(new CustomEvent('app:toast', {
          detail: { text: `复制失败：${err?.message || err}`, kind: 'err' },
        }));
      }
    });
    editBtn?.addEventListener('click', () => {
      const modal = root.querySelector('#sw-modal');
      if (modal) modal.hidden = false;
    });
    const saveShotFields = async (errEl, buildOverrides, { closeModal = false } = {}) => {
      if (errEl) errEl.textContent = '';
      const shotId = Number(getParam('shot'));
      // 引用关系必须先原样读回来：这一页不管引用，带空数组提交等于替人把
      // 人物与场景清空——那是"改个景别把整镜引用洗掉"的事故，不是保存设定。
      let cur = null;
      try {
        const rows = await api.shots(store.currentEpisodeId || 0);
        cur = rows.find((s) => s.id === shotId);
      } catch (err) {
        if (errEl) errEl.textContent = '读不回这一镜的现有引用，已停止保存：' + (err.message || '未知错误');
        return;
      }
      if (!cur) {
        if (errEl) errEl.textContent = '这一镜已经不在这个集里了，先回分镜表再看。';
        return;
      }
      const base = {
        summary: cur.summary,
        prompt: cur.prompt || '',
        duration: Number(cur.duration),
        asset_ids: cur.asset_ids || [],
        variant_ids: cur.variant_ids || [],
        shot_size: cur.shot_size || '',
        camera_move: cur.camera_move || '',
        aspect: cur.aspect || '',
        style: cur.style || '',
        negative_prompt: cur.negative_prompt || '',
        trim_in_ms: Number(cur.trim_in_ms || 0),
        trim_out_ms: Number(cur.trim_out_ms || 0),
        transition: cur.transition || 'cut',
        first_frame_image: cur.first_frame_image || '',
        last_frame_image: cur.last_frame_image || '',
      };
      const body = { ...base, ...buildOverrides() };
      if (!body.summary) {
        if (errEl) errEl.textContent = '内容摘要不能空——生成时它就是提示词的替身。';
        return;
      }
      if (body.trim_out_ms > 0 && body.trim_out_ms <= body.trim_in_ms) {
        if (errEl) errEl.textContent = '出点必须大于入点（或填 0 表示保留到片尾）。';
        return;
      }
      const ok = await run('保存设定', () => api.updateShot(shotId, body));
      if (ok) {
        if (closeModal) root.querySelector('#sw-modal').hidden = true;
        window.dispatchEvent(new CustomEvent('app:refresh'));
      }
    };

    root.querySelector('#sw-save')?.addEventListener('click', () => saveShotFields(
      root.querySelector('#sw-error'),
      () => ({
        summary: root.querySelector('#sw-summary').value.trim(),
        prompt: root.querySelector('#sw-prompt').value.trim(),
        duration: Number(root.querySelector('#sw-duration').value),
        shot_size: root.querySelector('#sw-size').value.trim(),
        camera_move: root.querySelector('#sw-move').value.trim(),
        aspect: root.querySelector('#sw-aspect').value.trim(),
        style: root.querySelector('#sw-style').value.trim(),
        negative_prompt: root.querySelector('#sw-neg').value.trim(),
        trim_in_ms: Math.max(0, Math.round(Number(root.querySelector('#sw-trim-in')?.value) || 0)),
        trim_out_ms: Math.max(0, Math.round(Number(root.querySelector('#sw-trim-out')?.value) || 0)),
        transition: root.querySelector('#sw-transition')?.value || 'cut',
        first_frame_image: root.querySelector('#sw-first-frame')?.value.trim() || '',
        last_frame_image: root.querySelector('#sw-last-frame')?.value.trim() || '',
      }),
      { closeModal: true },
    ));

    root.querySelectorAll('[data-clear-frame]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const field = btn.dataset.clearFrame;
        if (field !== 'first_frame_image' && field !== 'last_frame_image') return;
        saveShotFields(root.querySelector('#sw-trim-err'), () => ({ [field]: '' }));
      });
    });

    root.querySelectorAll('[data-frame-capture]').forEach((controls) => {
      const btn = controls.querySelector('button');
      const player = controls.dataset.frameCapture;
      const video = player === 'stage' ? stageVideo : reviewVideo;
      btn.addEventListener('click', async () => {
        if (!video || video.error || !Number.isFinite(video.currentTime)) {
          shotToast('视频不可读，无法截取当前帧');
          return;
        }
        const shotId = Number(getParam('shot'));
        const jobId = Number(controls.dataset.frameJob);
        const target = controls.querySelector('select').value;
        const offsetMs = Math.round(video.currentTime * 1000);
        let result;
        const ok = await run('截取当前帧', async () => {
          result = await api.extractShotFrame(shotId, {
            job_id: jobId,
            offset_ms: offsetMs,
            apply_to_next_shot: target === 'next',
            apply_to_self_last_frame: target === 'self',
          });
        }, { btn });
        if (!ok) return;
        let destination = '已保存 PNG';
        if (result.applied_to_next_shot) destination = '已写入下一镜首帧';
        else if (result.applied_to_self_last_frame) destination = '已存为本镜尾帧';
        else savedFrame = { shotId, player, jobId, url: result.frame_url };
        shotToast(`已截取 ${fmtMsSec(offsetMs)} 的视频帧，${destination}`, 'ok');
        if (btn.isConnected && Number(getParam('shot')) === shotId) renderView('shot');
      });
    });

    root.querySelector('#sw-extract-tail')?.addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      const shotId = Number(getParam('shot'));
      const seqLabel = btn?.dataset?.seq || String(shotId);
      const hasNext = btn?.dataset?.hasNext === '1';
      const errEl = root.querySelector('#sw-trim-err');
      if (errEl) errEl.textContent = '';
      let res = null;
      const ok = await run('截取尾帧', () => api.extractShotFrame(shotId, {
        position: 'last',
        apply_to_next_shot: hasNext,
        apply_to_self_last_frame: !hasNext,
      }).then((out) => { res = out; }));
      if (!ok || !res) return;
      window.dispatchEvent(new CustomEvent('app:toast', {
        detail: {
          text: res.applied_to_next_shot
            ? `已截取第 ${seqLabel} 镜尾帧，并写入下一镜首帧槽位`
            : '已是本集最后一镜，已截取尾帧并存为当前镜头尾帧槽位',
          kind: 'ok',
        },
      }));
      renderView('shot');
    });

    // M2: 双游标粗剪滑轨与暗场播放器实时联动
    const scrubber = root.querySelector('#sw-trim-scrubber');
    if (scrubber) {
      const inRange = root.querySelector('#sw-trim-in-range');
      const outRange = root.querySelector('#sw-trim-out-range');
      const inNum = root.querySelector('#sw-trim-in-num');
      const outNum = root.querySelector('#sw-trim-out-num');
      const winEl = root.querySelector('#sw-trim-win');
      const readout = root.querySelector('#sw-trim-readout');
      const player = stageVideo;
      const playhead = root.querySelector('#sw-trim-playhead');
      let totalMs = Math.max(1000, Number(scrubber.dataset.plannedMs) || 5000);

      const paintTrimWindow = () => {
        const tin = Math.max(0, Math.round(Number(inNum?.value) || 0));
        const tout = Math.max(0, Math.round(Number(outNum?.value) || 0));
        const effOut = tout > 0 ? Math.min(tout, totalMs) : totalMs;
        const leftPct = Math.min(100, (tin / totalMs) * 100);
        const rightPct = Math.max(leftPct, Math.min(100, (effOut / totalMs) * 100));
        if (winEl) {
          winEl.style.left = `${leftPct.toFixed(2)}%`;
          winEl.style.width = `${Math.max(1, rightPct - leftPct).toFixed(2)}%`;
        }
        const netMs = Math.max(0, effOut - tin);
        if (readout) {
          readout.textContent = `入点 ${fmtMsSec(tin)} · 出点 ${tout > 0 ? fmtMsSec(tout) : '至片尾'} · 净长 ${fmtMsSec(netMs)}`;
        }
      };

      const syncRangesMax = (ms) => {
        totalMs = Math.max(500, Math.round(ms));
        if (inRange) inRange.max = String(totalMs);
        if (outRange) outRange.max = String(totalMs);
        if (outRange && Number(outNum?.value || 0) === 0) outRange.value = String(totalMs);
        paintTrimWindow();
      };

      inRange?.addEventListener('input', () => {
        const val = Number(inRange.value) || 0;
        if (inNum) inNum.value = String(val);
        paintTrimWindow();
      });
      outRange?.addEventListener('input', () => {
        const val = Number(outRange.value) || 0;
        if (outNum) outNum.value = val >= totalMs ? '0' : String(val);
        paintTrimWindow();
      });
      inNum?.addEventListener('input', () => {
        if (inRange) inRange.value = String(Math.min(totalMs, Math.max(0, Number(inNum.value) || 0)));
        paintTrimWindow();
      });
      outNum?.addEventListener('input', () => {
        const val = Math.max(0, Number(outNum.value) || 0);
        if (outRange) outRange.value = String(val === 0 ? totalMs : Math.min(totalMs, val));
        paintTrimWindow();
      });
      paintTrimWindow();

      if (player) {
        player.addEventListener('loadedmetadata', () => {
          if (Number.isFinite(player.duration) && player.duration > 0) {
            syncRangesMax(player.duration * 1000);
          }
        });
        player.addEventListener('timeupdate', () => {
          const curMs = (player.currentTime || 0) * 1000;
          if (playhead && totalMs > 0) {
            playhead.hidden = false;
            playhead.style.left = `${Math.min(100, Math.max(0, (curMs / totalMs) * 100)).toFixed(2)}%`;
          }
        });
        root.querySelector('#sw-trim-play')?.addEventListener('click', () => {
          const tin = Math.max(0, Number(inNum?.value) || 0) / 1000;
          const tout = Math.max(0, Number(outNum?.value) || 0) / 1000;
          playSegment(player, tin, tout > tin ? tout : null);
        });
      }

      root.querySelector('#sw-trim-save')?.addEventListener('click', () => saveShotFields(
        root.querySelector('#sw-trim-err'),
        () => ({
          trim_in_ms: Math.max(0, Math.round(Number(inNum?.value) || 0)),
          trim_out_ms: Math.max(0, Math.round(Number(outNum?.value) || 0)),
          transition: root.querySelector('#sw-trim-trans')?.value || 'cut',
        }),
      ));
    }

    // M4: 同镜 A/B 双栏同步播放对比弹窗
    const autoCompare = getShotEntry()?.focus === 'compare';
    if (autoCompare) setShotEntry({ ...getShotEntry(), focus: 'review' });
    const abModal = root.querySelector('#sw-ab-modal');
    if (abModal) {
      const vidA = root.querySelector('#sw-ab-vid-a');
      const vidB = root.querySelector('#sw-ab-vid-b');
      const selA = root.querySelector('#sw-ab-sel-a');
      const selB = root.querySelector('#sw-ab-sel-b');
      const seek = root.querySelector('#sw-ab-seek');
      const timeEl = root.querySelector('#sw-ab-time');
      const toggleBtn = root.querySelector('#sw-ab-toggle');
      const rateSel = root.querySelector('#sw-ab-rate');

      const stopBoth = () => {
        vidA?.pause();
        vidB?.pause();
        if (toggleBtn) toggleBtn.textContent = '▶ 同步播放';
      };
      const closeAb = () => {
        stopBoth();
        abModal.hidden = true;
      };
      const syncUi = () => {
        if (!vidA || !seek || !timeEl) return;
        const dur = Number.isFinite(vidA.duration) && vidA.duration > 0 ? vidA.duration : 10;
        seek.max = dur.toFixed(2);
        const cur = Number(vidA.currentTime || 0);
        seek.value = Math.min(dur, cur).toFixed(2);
        timeEl.textContent = `${cur.toFixed(1)}s / ${dur.toFixed(1)}s`;
      };

      root.querySelectorAll('#sw-ab-compare, #sw-ab-review').forEach((btn) => btn.addEventListener('click', () => {
        abModal.hidden = false;
        syncUi();
      }));
      if (autoCompare && abModal.dataset.comparesSource === 'true') {
        abModal.hidden = false;
        syncUi();
      }
      root.querySelector('#sw-ab-close')?.addEventListener('click', closeAb);
      abModal.addEventListener('click', (e) => { if (e.target === abModal) closeAb(); });
      bindModalDismiss(abModal, closeAb);

      const loadSide = (vid, option) => {
        if (!vid) return;
        const wasPlaying = !vidA?.paused;
        stopBoth();
        vid.src = api.jobArtifactUrl(Number(option.value), Number(option.dataset.index));
        vid.currentTime = 0;
        if (vidB && vid === vidA) vidB.currentTime = 0;
        else if (vidA && vid === vidB) vidA.currentTime = 0;
        syncUi();
        if (wasPlaying) {
          vidA?.play().catch(() => {});
          vidB?.play().catch(() => {});
        }
      };
      selA?.addEventListener('change', (e) => loadSide(vidA, e.target.selectedOptions[0]));
      selB?.addEventListener('change', (e) => loadSide(vidB, e.target.selectedOptions[0]));

      toggleBtn?.addEventListener('click', () => {
        if (!vidA || !vidB) return;
        if (vidA.paused) {
          vidB.currentTime = vidA.currentTime || 0;
          const rate = Number(rateSel?.value || 1) || 1;
          vidA.playbackRate = rate;
          vidB.playbackRate = rate;
          vidA.play().catch(() => {});
          vidB.play().catch(() => {});
          toggleBtn.textContent = '⏸ 同步暂停';
        } else {
          stopBoth();
        }
      });

      root.querySelector('#sw-ab-reset')?.addEventListener('click', () => {
        stopBoth();
        if (vidA) vidA.currentTime = 0;
        if (vidB) vidB.currentTime = 0;
        syncUi();
      });

      seek?.addEventListener('input', () => {
        const t = Number(seek.value) || 0;
        if (vidA) vidA.currentTime = t;
        if (vidB) vidB.currentTime = t;
        syncUi();
      });

      rateSel?.addEventListener('change', () => {
        const rate = Number(rateSel.value || 1) || 1;
        if (vidA) vidA.playbackRate = rate;
        if (vidB) vidB.playbackRate = rate;
      });

      vidA?.addEventListener('loadedmetadata', syncUi);
      vidA?.addEventListener('timeupdate', () => {
        syncUi();
        if (vidB && !vidA.paused && Math.abs((vidB.currentTime || 0) - (vidA.currentTime || 0)) > 0.18) {
          vidB.currentTime = vidA.currentTime || 0;
        }
      });
      vidA?.addEventListener('ended', stopBoth);
    }
  },
};
