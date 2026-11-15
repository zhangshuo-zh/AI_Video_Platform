import { api } from '../api.js';
import { store } from '../store.js';
import { escapeHtml, run } from '../util.js';
import { getParam, setJobPrefill, setParam, setSub } from '../navstate.js';
import { renderView } from '../router.js';
import { jobLabel, jobTone, publicStatus } from '../validation.js';
import { gotoEntry } from '../goto.js';
import { checklistHtml } from './review-checklist.js';
// 这一屏正在审的那条作业；勾清单要写回它
let swReviewJob = null;

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

function artifactPreview(job) {
  if (!job || !job.artifacts || !job.artifacts.length) return '<span class="hint">产物不可读或已清理</span>';
  const first = job.artifacts.find((a) => a.exists) || job.artifacts[0];
  const name = (first.path || '').split('/').pop() || '';
  const ext = name.split('.').pop().toLowerCase();
  if (/^(mp4|webm|mov|m4v)$/.test(ext)) {
    return `<video controls preload="metadata" class="sw-preview-video" src="${api.jobArtifactUrl(job.job_id, 0)}"></video>`;
  }
  if (/^(png|jpg|jpeg|webp)$/.test(ext)) {
    return `<img src="${api.jobArtifactUrl(job.job_id, 0)}" alt="${escapeHtml(name)}" class="sw-preview-img">`;
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

function previewStageHtml(d) {
  const s = d.setting;
  const formal = d.preview.formal_video;
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
    ${formal
      ? `<video id="sw-stage-player" class="wb-spotlight-player" controls preload="metadata" src="${api.jobArtifactUrl(formal.job_id, 0)}"></video>`
      : '<div class="wb-spotlight-empty">还没有签过通过的正式视频，预览区留空——不拿未签字的产物冒充正式结果；下方仍可预设粗剪出入点与镜头转场。</div>'}
    ${formal ? `<div class="kv-grid">
      <div class="kv"><dt>参数快照</dt><dd class="mono t-desc">${escapeHtml(formal.params || '{}')}</dd></div>
      ${formal.retry_of_job_id ? `<div class="kv"><dt>重试来源</dt><dd>#${formal.retry_of_job_id}</dd></div>` : ''}
    </div>` : ''}
    <div class="wb-trim-scrubber" id="sw-trim-scrubber" data-planned-ms="${plannedMs}">
      <div class="wb-trim-bar">
        <span id="sw-trim-readout">入点 ${fmtMsSec(trimIn)} · 出点 ${trimOut > 0 ? fmtMsSec(trimOut) : '至片尾'}</span>
        ${formal ? `<div class="row u-gap-6">
          <button type="button" class="btn xs" id="sw-trim-play">▶ 试播剪辑段</button>
          <button type="button" class="btn xs primary" id="sw-extract-tail"
            data-seq="${d.setting.seq}" data-has-next="${d.next_shot_id ? '1' : '0'}"
            title="${d.next_shot_id ? '从正式视频末帧抽帧并写入下一镜首帧槽位' : '已是本集最后一镜，抽帧存为当前镜头尾帧'}">📸 截尾帧给下一镜</button>
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


/** ⑦ 审核这一段：待签作业、就地签字、以及共用的质量清单面板。 */
function reviewBlock(d, cl) {
  const canSign = store.can('review:sign');
  const pending = d.review.pending || [];
  const summary = `<p class="hint">已签 ${d.review.signs.length} 条；待签 ${pending.length} 条。
      正式产物只认「最新一条成功且签过通过」的作业，与成片取片同一条口径。</p>`;
  const rows = pending.length ? `<ul class="hint">${pending.map((j) => `<li>
      <b>作业 #${j.job_id}</b> ${escapeHtml((j.prompt || '').slice(0, 40))}
      <span class="t-dim">${escapeHtml(j.finished_at || '')}</span>
      ${canSign ? `<div class="row" style="gap:6px;display:inline-flex;margin-left:6px">
        <button class="btn xs${cl && !cl.can_approve ? '' : ' primary'}"
                data-jsign="approved" data-job="${j.job_id}"
                ${cl && !cl.can_approve ? 'disabled title="' + escapeHtml(cl.hint) + '"' : ''}>通过</button>
        <button class="btn xs" data-jsign="returned" data-job="${j.job_id}">退回</button>
        <button class="btn xs danger" data-jsign="rejected" data-job="${j.job_id}">驳回</button>
      </div>` : ''}</li>`).join('')}</ul>` : '';
  return `${summary}${rows}${checklistHtml(cl)}
    <p class="hint">要看这一项目全部待签的产物，去审核中心·产物审核；
      但**签字这件事在这一屏就能做完**。</p>`;
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
    try {
      d = await api.shotWorkbench(shotId);
    } catch (err) {
      return `<section class="view on"><div class="page-head"><h1>镜头工作台</h1></div>
        <div class="card"><div class="empty"><div class="empty-t">读不到这一镜</div>
        <p>${escapeHtml(err.message || '')}</p>
        <button class="btn" id="sw-back">回到分镜列表</button></div></div></section>`;
    }
    // ⑦ 就地审核（任务书 §12.1「视频结果直接提交审核」）：
    // 待签的产物就摆在镜头详情里，不用再跑一趟审核中心。清单读不到时只少一块面板，
    // 不挡住这一镜的其余七段。
    const reviewTarget = d.review.pending[0] || null;
    swReviewJob = reviewTarget ? reviewTarget.job_id : null;
    let cl = null;
    if (reviewTarget) {
      try { cl = await api.jobChecklist(reviewTarget.job_id); } catch { cl = null; }
    }

    const head = `<div class="page-head">
      <h1>第 ${d.setting.seq} 镜</h1>
      <p>${escapeHtml((d.setting.summary || '').slice(0, 80))}</p>
      <div class="right">
        <button class="btn" id="sw-back">分镜表</button>
        <button class="btn" id="sw-edit">编辑设定</button>
        <button class="btn primary" id="sw-generate">提交生成</button>
      </div></div>`;

    const section = (title, body) => `<div class="card u-mb-12">
      <div class="card-head"><h2>${title}</h2></div>
      <div class="card-body">${body}</div></div>`;

    return `<section class="view on">${head}
      ${section('① 镜头设定', settingForm(d))}
      ${section('② 资产引用', refsBlock(d))}
      ${section(`③ 关键帧（${d.keyframes.jobs.length} 条记录）`, jobRows(d.keyframes.jobs, d.keyframes.formal_job_id)
        + '<p class="hint">「正式」＝最新一条签过通过的关键帧作业。要换正式结果，去审核中心签另一条。</p>')}
      ${section(`④ 视频（${d.videos.jobs.length} 条记录）`, jobRows(d.videos.jobs, d.videos.formal_job_id)
        + (d.videos.source_stale ? '<p class="modal-err">这一镜的分镜或资产在生成之后改过，最新产物可能已过期。</p>' : ''))}
      ${section('⑤ 音频', audioBlock(d))}
      ${section('⑥ 预览与粗剪', previewStageHtml(d))}
      ${section('⑦ 审核', reviewBlock(d, cl))}
      ${section('⑧ 生产完成条件', readyBlock(d))}
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
    </section>`;
  },

  mount(root) {
    const go = (entry) => {
      gotoEntry(entry);
    };
    root.querySelectorAll('[data-check]').forEach((b) => b.addEventListener('click', async () => {
      if (!swReviewJob) return;
      const ok = await run('记录清单', () => api.saveJobChecklist(swReviewJob, {
        items: [{ code: b.dataset.check, state: b.dataset.state, note: '' }],
      }), { btn: b });
      if (ok) renderView('shot');
    }));
    root.querySelectorAll('[data-jsign]').forEach((b) => b.addEventListener('click', async () => {
      const jobId = Number(b.dataset.job);
      const ok = await run('签字', () => api.jobReviewSign(jobId, {
        anchor: 'artifact', decision: b.dataset.jsign, note: '',
      }), { btn: b });
      if (ok) renderView('shot');
    }));
    root.querySelectorAll('[data-goto]').forEach((b) => b.addEventListener('click', () => {
      let entry;
      try { entry = JSON.parse(b.dataset.goto); } catch { return; }
      go(entry);
    }));
    root.querySelectorAll('[data-open-shot]').forEach((b) => b.addEventListener('click', () => {
      setParam('shot', Number(b.dataset.openShot));
      renderView('shot');
    }));
    root.querySelectorAll('[data-asset]').forEach((b) => b.addEventListener('click', () => {
      setSub('assets', 0);
      setParam('assets', Number(b.dataset.asset));
      renderView('assets');
    }));
    root.querySelector('#sw-back')?.addEventListener('click', () => {
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
        prompt: (root.querySelector('#sw-prompt') || {}).value || '',
        params: Object.keys(prefillParams).length ? prefillParams : undefined });
      renderView('generate');
    });
    const editBtn = root.querySelector('#sw-edit');
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
      const player = root.querySelector('#sw-stage-player');
      const playhead = root.querySelector('#sw-trim-playhead');
      let totalMs = Math.max(1000, Number(scrubber.dataset.plannedMs) || 5000);
      let stopAtSec = null;

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
          if (stopAtSec != null && player.currentTime >= stopAtSec) {
            player.pause();
            stopAtSec = null;
          }
        });
        root.querySelector('#sw-trim-play')?.addEventListener('click', () => {
          const tin = Math.max(0, Number(inNum?.value) || 0) / 1000;
          const tout = Math.max(0, Number(outNum?.value) || 0) / 1000;
          stopAtSec = tout > tin ? tout : null;
          player.currentTime = tin;
          player.play().catch(() => {});
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
  },
};
