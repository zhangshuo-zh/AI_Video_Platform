import { api } from '../api.js';
import { gotoEntry } from '../goto.js';
import { store } from '../store.js';
import { escapeHtml } from '../util.js';
import { referenceDialog } from './asset-reference-actions.js';

export async function openCharacterAppearances(asset, variants) {
  const projectId = store.currentId;
  const dialog = referenceDialog('角色出镜对照', '<div data-appearance-body><p role="status">正在读取已通过的镜头…</p></div>');
  const host = dialog.querySelector('[data-appearance-body]');
  try {
    const rows = await api.characterAppearances(projectId, asset.id);
    if (!dialog.open || projectId !== store.currentId) return;
    if (!rows.length) {
      host.textContent = '当前项目还没有引用这名角色且通过签审的视频镜头。';
      return;
    }
    const references = [asset, ...variants].flatMap((group) => group.ref_images.map((path, index) => ({
      path, label: `${group === asset ? '基础图' : group.name} · ${group.image_purposes?.[path] || '第 ' + (index + 1) + ' 张'}`,
    })));
    host.innerHTML = `
      <p class="hint">每个镜头显示最近一次签审通过的视频。形态取分镜的当前引用，画面由你核对。</p>
      <div class="asset-reference-compare asset-appearance-compare">
        <div><label>参考图 <select class="ctl" data-appearance-reference>${references.map((ref, i) =>
          `<option value="${i}">${escapeHtml(ref.label)}</option>`).join('')}</select></label>
          ${references.length ? '<img class="asset-reference-preview" data-appearance-image alt="角色参考图">' : '<p>角色还没有参考图。</p>'}
        </div>
        <div><fieldset data-appearance-shots><legend>对照镜头（最多 3 个）</legend>
          ${rows.map((row, i) => `<label><input type="checkbox" value="${i}"${i < 3 ? ' checked' : ''}>
            第 ${row.episode_seq} 集 · 镜 ${row.seq}${row.variant_names.length
              ? ' · ' + escapeHtml(row.variant_names.join('、')) : ''}</label>`).join('')}
          </fieldset><div data-appearance-video class="asset-appearance-videos"></div>
        </div>
      </div>`;
    const image = host.querySelector('[data-appearance-image]');
    const reference = host.querySelector('[data-appearance-reference]');
    const shots = host.querySelector('[data-appearance-shots]');
    const video = host.querySelector('[data-appearance-video]');
    function showImage() {
      if (image) image.src = `/files/${references[Number(reference.value)].path}`;
    }
    function showVideo() {
      const checks = [...shots.querySelectorAll('input')];
      const chosen = checks.filter((input) => input.checked);
      checks.forEach((input) => { input.disabled = !input.checked && chosen.length >= 3; });
      const clips = chosen.map((input) => rows[Number(input.value)])
        .sort((a, b) => a.episode_seq - b.episode_seq || a.seq - b.seq);
      video.innerHTML = clips.map((row) => `<figure>
        ${row.artifact_index == null ? '<p>视频文件当前不可播放。</p>'
          : `<video controls preload="metadata" class="asset-reference-preview"
              src="${api.jobArtifactUrl(row.job_id, row.artifact_index)}#t=0.1"></video>
             <span class="t-dim" data-video-state>已就绪 · 默认暂停</span>`}
        <figcaption>第 ${row.episode_seq} 集 · 镜 ${row.seq} · ${escapeHtml(row.summary)}</figcaption>
        <button type="button" class="btn xs" data-appearance-job="${row.job_id}">查看作业 #${row.job_id}</button>
      </figure>`).join('') || '<p>请勾选要对照的镜头。</p>';
      video.querySelectorAll('video').forEach((el) => {
        const badge = el.parentElement?.querySelector('[data-video-state]');
        el.addEventListener('loadstart', () => { if (badge) badge.textContent = '加载片段中…'; });
        el.addEventListener('loadeddata', () => { if (badge) badge.textContent = '已就绪 · 默认暂停'; });
        el.addEventListener('error', () => { if (badge) badge.textContent = '视频加载失败，可点下方查看作业'; });
      });
    }
    reference.disabled = !references.length;
    reference.addEventListener('change', showImage);
    shots.addEventListener('change', showVideo);
    video.addEventListener('click', (event) => {
      const button = event.target.closest('[data-appearance-job]');
      if (!button) return;
      dialog.close();
      gotoEntry({ module: 'generate', job_id: Number(button.dataset.appearanceJob) });
    });
    showImage();
    showVideo();
  } catch (error) {
    if (dialog.open) host.textContent = `读取出镜记录失败：${error.message}`;
  }
}
