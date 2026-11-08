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
    return `<video controls preload="metadata" style="max-width:260px" src="${api.jobArtifactUrl(job.job_id, 0)}"></video>`;
  }
  if (/^(png|jpg|jpeg|webp)$/.test(ext)) {
    return `<img src="${api.jobArtifactUrl(job.job_id, 0)}" alt="${escapeHtml(name)}" style="max-width:200px">`;
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

function settingForm(d) {
  const s = d.setting;
  const row = (label, key, hint) => `
    <div class="kv"><dt>${label}</dt><dd>${escapeHtml(s[key] || '未填')}
      ${hint ? `<div class="hint">${hint}</div>` : ''}</dd></div>`;
  return `
  <div class="kv-grid">
    ${row('镜头编号', 'seq')}
    ${row('时长（秒）', 'duration')}
    ${row('景别', 'shot_size', '今天只有工作台在显示它，执行器不读；填了不会改变画面')}
    ${row('运镜', 'camera_move')}
    ${row('画面比例', 'aspect')}
    ${row('镜头风格', 'style')}
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
  <p class="note"><b>配音与音乐今天只能登记，不能真出声。</b>
    ${escapeHtml(engines.tts.label)}：${engines.tts.provider ? `默认轨 ${escapeHtml(engines.tts.provider)}（${escapeHtml(engines.tts.health.detail)}）` : '默认轨没配'}；
    执行器还没接线，所以这一页不给"提交配音"的按钮。口型（Rhubarb）同样未接入。</p>
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
        <div class="empty-t">还没有项目</div></div></div></section>`;
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

    const section = (title, body) => `<div class="card" style="margin-bottom:12px">
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
      ${section('⑥ 预览与确认', d.preview.formal_video
        ? `<div class="kv-grid"><div class="kv"><dt>正式视频</dt><dd>${artifactPreview(d.preview.formal_video)}</dd></div>
           <div class="kv"><dt>模型</dt><dd>${escapeHtml(d.preview.formal_video.provider)}／${escapeHtml(d.preview.formal_video.model)}</dd></div>
           <div class="kv"><dt>参数快照</dt><dd class="mono t-desc">${escapeHtml(d.preview.formal_video.params || '{}')}</dd></div>
           <div class="kv"><dt>版本</dt><dd>第 ${d.preview.formal_video.attempt} 次尝试${d.preview.formal_video.retry_of_job_id ? `（重试自 #${d.preview.formal_video.retry_of_job_id}）` : ''}</dd></div></div>`
        : '<p class="hint">还没有签过通过的正式视频，预览区是空的——不拿最新一条未签字的产物冒充正式结果。</p>')}
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
          <input class="ctl" id="sw-move" maxlength="40" value="${escapeHtml(d.setting.camera_move)}">
          <label for="sw-aspect">画面比例</label>
          <input class="ctl" id="sw-aspect" maxlength="20" value="${escapeHtml(d.setting.aspect)}">
          <label for="sw-style">镜头风格</label>
          <input class="ctl" id="sw-style" maxlength="60" value="${escapeHtml(d.setting.style)}">
          <label for="sw-neg">负面提示词</label>
          <input class="ctl" id="sw-neg" maxlength="2000" value="${escapeHtml(d.setting.negative_prompt)}">
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
      b.disabled = true;
      const ok = await run('记录清单', () => api.saveJobChecklist(swReviewJob, {
        items: [{ code: b.dataset.check, state: b.dataset.state, note: '' }],
      }));
      b.disabled = false;
      if (ok) renderView('shot');
    }));
    root.querySelectorAll('[data-jsign]').forEach((b) => b.addEventListener('click', async () => {
      const jobId = Number(b.dataset.job);
      b.disabled = true;
      const ok = await run('签字', () => api.jobReviewSign(jobId, {
        anchor: 'artifact', decision: b.dataset.jsign, note: '',
      }));
      b.disabled = false;
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
          detail: { text: '预检没跑成：' + (err.message || '未知错误') } }));
        return;
      }
      if (check.status === '阻塞') {
        window.dispatchEvent(new CustomEvent('app:toast', {
          detail: { text: `预检阻塞 ${check.blocking} 项：` + check.items
            .filter((i) => i.level === '阻塞').map((i) => i.text).join('；') } }));
        return;
      }
      setJobPrefill({ projectId: store.currentId, episodeId: store.currentEpisodeId,
        shotId: Number(shotId), capability: 'video',
        // 同一镜的生成入口，场景键与分镜页那一处一致（`shot_video`）。
        sceneKey: 'shot_video',
        prompt: (root.querySelector('#sw-prompt') || {}).value || '' });
      renderView('generate');
    });
    const editBtn = root.querySelector('#sw-edit');
    editBtn?.addEventListener('click', () => {
      const modal = root.querySelector('#sw-modal');
      if (modal) modal.hidden = false;
    });
    root.querySelector('#sw-save')?.addEventListener('click', async () => {
      const errEl = root.querySelector('#sw-error');
      errEl.textContent = '';
      const shotId = Number(getParam('shot'));
      // 引用关系必须先原样读回来：这一页不管引用，带空数组提交等于替人把
      // 人物与场景清空——那是"改个景别把整镜引用洗掉"的事故，不是保存设定。
      let cur = null;
      try {
        const rows = await api.shots(store.currentEpisodeId || 0);
        cur = rows.find((s) => s.id === shotId);
      } catch (err) {
        errEl.textContent = '读不回这一镜的现有引用，已停止保存：' + (err.message || '未知错误');
        return;
      }
      if (!cur) {
        errEl.textContent = '这一镜已经不在这个集里了，先回分镜表再看。';
        return;
      }
      const body = {
        summary: root.querySelector('#sw-summary').value.trim(),
        prompt: root.querySelector('#sw-prompt').value.trim(),
        duration: Number(root.querySelector('#sw-duration').value),
        asset_ids: cur.asset_ids || [],
        variant_ids: cur.variant_ids || [],
        shot_size: root.querySelector('#sw-size').value.trim(),
        camera_move: root.querySelector('#sw-move').value.trim(),
        aspect: root.querySelector('#sw-aspect').value.trim(),
        style: root.querySelector('#sw-style').value.trim(),
        negative_prompt: root.querySelector('#sw-neg').value.trim(),
      };
      if (!body.summary) {
        errEl.textContent = '内容摘要不能空——生成时它就是提示词的替身。';
        return;
      }
      const ok = await run('保存设定', () => api.updateShot(shotId, body));
      if (ok) {
        root.querySelector('#sw-modal').hidden = true;
        window.dispatchEvent(new CustomEvent('app:refresh'));
      }
    });
  },
};
