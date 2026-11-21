import { api } from '../api.js';
import { gotoEntry } from '../goto.js';
import { store } from '../store.js';
import { escapeHtml, run } from '../util.js';
import { referenceDialog } from './asset-reference-actions.js';

export async function openReferenceUsage(asset) {
  const projectId = store.currentId;
  const dialog = referenceDialog('作业实际用图', '<div data-reference-usage><p role="status">正在读取视频作业回执…</p></div>');
  const host = dialog.querySelector('[data-reference-usage]');
  try {
    const [uses, variants] = await Promise.all([
      api.assetReferenceUsage(projectId, asset.id), api.assetVariants(asset.id),
    ]);
    if (!dialog.open || projectId !== store.currentId) return;
    if (!uses.length) {
      host.textContent = '当前项目没有这条资产已确认的帧输入记录。';
      return;
    }
    const images = new Map();
    for (const group of [asset, ...variants]) {
      for (const [index, path] of group.ref_images.entries()) {
        const purpose = group.image_purposes?.[path] || `第 ${index + 1} 张`;
        images.set(path, `${group === asset ? '基础参考图' : group.name} · ${purpose}`);
      }
    }
    const usedPaths = [...new Set(uses.map((use) => use.path))];
    const canEdit = store.can('doc:write');
    host.innerHTML = `
      <p class="hint">仅列当前项目中成功的视频作业，帧输入以执行器回执为准。</p>
      <label>旧图 <select class="ctl" data-usage-old>${usedPaths.map((path) =>
        `<option value="${escapeHtml(path)}">${escapeHtml(images.get(path) || path.split('/').at(-1))}</option>`).join('')}</select></label>
      <div class="asset-reference-compare" data-usage-previews></div>
      <div data-usage-list></div>
      ${canEdit ? `<label>替换为 <select class="ctl" data-usage-new></select></label>
        <button type="button" class="btn primary" data-usage-apply>更新所选镜头帧输入</button>
        <p class="hint" role="status" data-usage-note>勾选要采用新图的镜头。保存后的帧输入用于下一次生成。</p>` : ''}`;
    const old = host.querySelector('[data-usage-old]');
    const list = host.querySelector('[data-usage-list]');
    const replacement = host.querySelector('[data-usage-new]');
    const apply = host.querySelector('[data-usage-apply]');
    const note = host.querySelector('[data-usage-note]');
    let eligible = [];
    function showPreviews() {
      const paths = [['旧图', old.value], ['新图', replacement?.value]];
      host.querySelector('[data-usage-previews]').innerHTML = paths.filter(([, path]) => path)
        .map(([label, path]) => images.has(path)
          ? `<figure><img class="asset-reference-preview" src="/files/${escapeHtml(path)}" alt="${label}">
              <figcaption>${label}</figcaption></figure>`
          : `<p class="hint">${label}已从资产移除，仍可查看历史作业。</p>`).join('');
    }
    function showUses() {
      const matches = uses.filter((use) => use.path === old.value);
      const byFrame = new Map();
      for (const item of matches) {
        const key = `${item.shot_id}/${item.field}`;
        if (!byFrame.has(key)) byFrame.set(key, []);
        byFrame.get(key).push(item);
      }
      eligible = [...byFrame.values()].map((items) => items[0]);
      list.innerHTML = `<ul class="asset-preparation-list">${[...byFrame.values()].map((items, index) => {
        const frame = items[0];
        const name = `第 ${frame.episode_seq} 集 · 镜 ${frame.seq} · ${frame.field === 'first_frame_image' ? '首帧' : '尾帧'}`;
        return `<li>${canEdit && frame.replaceable
          ? `<label><input type="checkbox" data-usage-frame="${index}"> ${name}</label>`
          : `<strong>${name}</strong>`}
          ${!frame.replaceable ? '<span class="hint">镜头已经指定了其他图片</span>' : ''}
          <div>${items.map((item) =>
            `<button type="button" class="btn xs" data-usage-job="${item.job_id}">作业 #${item.job_id} · ${item.signed ? '已签审通过' : '暂无通过签审'}</button>`).join(' ')}</div></li>`;
      }).join('')}</ul>`;
      if (replacement) {
        replacement.innerHTML = '<option value="">请选择新图</option>' + [...images]
          .filter(([path]) => path !== old.value)
          .map(([path, label]) => `<option value="${escapeHtml(path)}">${escapeHtml(label)}</option>`).join('');
      }
      showPreviews();
    }
    old.addEventListener('change', showUses);
    replacement?.addEventListener('change', showPreviews);
    list.addEventListener('click', (event) => {
      const button = event.target.closest('[data-usage-job]');
      if (!button) return;
      dialog.close();
      gotoEntry({ module: 'generate', job_id: Number(button.dataset.usageJob) });
    });
    apply?.addEventListener('click', async () => {
      if (projectId !== store.currentId) return;
      const changes = [...list.querySelectorAll('[data-usage-frame]:checked')].map((input) => {
        const row = eligible[Number(input.dataset.usageFrame)];
        return { shot_id: row.shot_id, field: row.field, expected: row.current };
      });
      if (!replacement.value || !changes.length) {
        note.textContent = '请选择新图并勾选至少一个帧位置。';
        return;
      }
      if (changes.length > 100) { note.textContent = '每次最多更新 100 个帧位置。'; return; }
      let result;
      const ok = await run('更新所选镜头', async () => {
        result = await api.replaceAssetReferences(projectId, asset.id, {
          old_path: old.value, new_path: replacement.value, changes,
        });
      }, { btn: apply });
      if (!ok || !dialog.open) return;
      dialog.close();
      window.dispatchEvent(new CustomEvent('app:toast', {
        detail: { text: `已更新 ${result.updated} 个镜头的帧输入`, kind: 'ok' },
      }));
    });
    showUses();
  } catch (error) {
    if (dialog.open) host.textContent = `读取实际用图失败：${error.message}`;
  }
}
