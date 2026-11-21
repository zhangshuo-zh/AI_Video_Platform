import { api } from '../api.js';
import { setJobPrefill } from '../navstate.js';
import { renderView } from '../router.js';
import { store } from '../store.js';
import { escapeHtml, run } from '../util.js';

export function referenceActions(path, purpose, canEdit, canUse) {
  const encoded = escapeHtml(path);
  return `<div class="asset-ref-actions">
    ${canEdit ? `<form data-reference-purpose="${encoded}">
      <input class="ctl xs" name="purpose" maxlength="30" aria-label="参考图用途"
        placeholder="用途，如侧脸/反打" value="${escapeHtml(purpose || '')}">
      <button class="btn xs" type="submit">保存</button>
    </form>` : (purpose ? `<span class="chip plain">${escapeHtml(purpose)}</span>` : '')}
    ${canUse ? `<button class="btn xs" data-reference-use="${encoded}">用作镜头首帧</button>` : ''}
  </div>`;
}

export function referenceDialog(title, body) {
  const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const dialog = document.createElement('dialog');
  dialog.className = 'asset-reference-dialog';
  dialog.innerHTML = `<form method="dialog" class="stack">
    <h2>${escapeHtml(title)}</h2>${body}
    <div><button class="btn" value="cancel">关闭</button></div>
  </form>`;
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener('close', () => {
    dialog.remove();
    if (opener?.isConnected) opener.focus({ preventScroll: true });
  }, { once: true });
  document.body.append(dialog);
  dialog.showModal();
  return dialog;
}

export function bindReferenceActions(container, asset, variant = null) {
  const group = variant || asset;
  const assetId = asset.id;
  const variantId = variant?.id ?? null;
  container.querySelectorAll('[data-reference-purpose]').forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const purpose = form.elements.purpose.value.trim();
      const button = form.querySelector('button');
      button.disabled = true;
      try {
        const saved = await run('保存用途', () => api.setAssetImagePurpose(assetId, {
          path: form.dataset.referencePurpose, variant_id: variantId, purpose,
        }));
        if (saved && form.isConnected) {
          const path = form.dataset.referencePurpose;
          group.image_purposes ??= {};
          if (purpose) group.image_purposes[path] = purpose;
          else delete group.image_purposes[path];
          window.dispatchEvent(new CustomEvent('app:toast', {
            detail: { text: '参考图用途已保存', kind: 'ok' },
          }));
        }
      } finally {
        button.disabled = false;
      }
    });
  });

  container.querySelectorAll('[data-reference-use]').forEach((button) => {
    button.addEventListener('click', async () => {
      const projectId = store.currentId;
      const episodeId = store.currentEpisodeId;
      button.disabled = true;
      let shots;
      try {
        const rows = await api.shots(episodeId);
        shots = rows.filter((shot) => shot.asset_ids.includes(assetId)
          && (variantId === null || shot.variant_ids.includes(variantId)));
      } catch (error) {
        if (button.isConnected) referenceDialog('读取分镜失败', `<p>${escapeHtml(error.message)}</p>`);
        return;
      } finally {
        button.disabled = false;
      }
      if (!button.isConnected || projectId !== store.currentId || episodeId !== store.currentEpisodeId) return;
      if (!shots.length) {
        referenceDialog('没有匹配的分镜', '<p>请在当前集的分镜中引用这条资产或形态。</p>');
        return;
      }
      const image = button.dataset.referenceUse;
      const dialog = referenceDialog('作为本次生成的首帧', `
        <img class="asset-reference-preview" src="/files/${escapeHtml(image)}" alt="所选首帧">
        <label>当前集分镜 <select class="ctl" name="shot">
          ${shots.map((shot) => `<option value="${shot.id}">第 ${shot.seq} 镜 · ${escapeHtml(shot.summary)}</option>`).join('')}
        </select></label>
        <p class="hint">生成参数会预填这张图，请选择支持首帧输入的模型。</p>
        <button class="btn primary" type="button" data-reference-continue>前往生成</button>`);
      dialog.querySelector('[data-reference-continue]').addEventListener('click', () => {
        const shot = shots.find((item) => item.id === Number(dialog.querySelector('select').value));
        if (!shot || projectId !== store.currentId || episodeId !== store.currentEpisodeId) {
          dialog.close();
          return;
        }
        const params = { image };
        if (shot.last_frame_image) params.last_frame_image = shot.last_frame_image;
        if (shot.negative_prompt) params.negative_prompt = shot.negative_prompt;
        setJobPrefill({
          projectId, episodeId, shotId: shot.id, capability: 'video', sceneKey: 'shot_video',
          prompt: shot.prompt || shot.summary, params, requireReferenceImage: true,
        });
        dialog.close();
        renderView('generate');
      });
    });
  });
}
