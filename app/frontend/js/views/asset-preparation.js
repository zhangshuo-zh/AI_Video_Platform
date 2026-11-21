import { api } from '../api.js';
import { setParam, setSub } from '../navstate.js';
import { renderView } from '../router.js';
import { store } from '../store.js';
import { escapeHtml } from '../util.js';
import { referenceDialog } from './asset-reference-actions.js';
import { setStudioTarget } from './asset-workspaces.js';

function collectNeeds(shots, assets, variants, missingPurposes) {
  const needs = new Map();
  function add(assetId, variant, issue, seq, issueKey = issue) {
    const key = `${assetId}/${variant?.id ?? ''}/${issueKey}`;
    if (!needs.has(key)) needs.set(key, { assetId, variant, issue, seqs: new Set() });
    needs.get(key).seqs.add(seq);
  }

  const seqs = new Set(shots.map((shot) => shot.seq));
  const covered = new Set();
  for (const item of missingPurposes) {
    if (!seqs.has(item.seq)) continue;
    const variant = variants.get(item.variant_id);
    covered.add(`${item.seq}/${item.asset_id}/${variant?.id ?? ''}`);
    add(item.asset_id, variant, `缺「${item.purpose}」参考图`, item.seq, `purpose/${item.purpose_key}`);
  }
  for (const shot of shots) {
    const selectedVariants = shot.variant_ids.map((id) => variants.get(id)).filter(Boolean);
    for (const id of shot.variant_ids) {
      if (!variants.has(id)) add(null, null, `无法核对形态 #${id}`, shot.seq);
    }
    for (const id of shot.asset_ids) {
      const asset = assets.get(id);
      if (!asset) {
        add(id, null, '无法读取这条引用', shot.seq);
        continue;
      }
      const selected = selectedVariants.filter((variant) => variant.asset_id === id);
      if (selected.length) {
        for (const variant of selected) {
          if (!variant.image_count && !covered.has(`${shot.seq}/${id}/${variant.id}`)) {
            add(id, variant, '缺形态参考图', shot.seq);
          }
        }
      } else if (!asset.image_count && !covered.has(`${shot.seq}/${id}/`)) {
        add(id, null, '缺基础参考图', shot.seq);
      }
    }
  }
  return [...needs.values()];
}

export async function openAssetPreparation() {
  const projectId = store.currentId;
  const episodeId = store.currentEpisodeId;
  const dialog = referenceDialog('本场待备素材', '<div data-preparation><p role="status">正在读取当前集分镜…</p></div>');
  const host = dialog.querySelector('[data-preparation]');
  try {
    const { shots, assets: assetRows, variants: variantRows, missing_purposes: missingPurposes = [] } = await api.assetPreparation(episodeId);
    if (!dialog.open || projectId !== store.currentId || episodeId !== store.currentEpisodeId) return;

    const assets = new Map(assetRows.map((asset) => [asset.id, asset]));
    const sceneIds = [...new Set(shots.flatMap((shot) =>
      shot.asset_ids.filter((id) => assets.get(id)?.kind === 'scene')))];
    const variants = new Map(variantRows.map((variant) => [variant.id, variant]));
    host.innerHTML = `<label>场景 <select class="ctl" data-preparation-scene>
      <option value="">当前集全部镜头</option>
      ${sceneIds.map((id) => `<option value="${id}">${escapeHtml(assets.get(id).name)}</option>`).join('')}
      <option value="unassigned">未引用场景的镜头</option>
    </select></label><div data-preparation-list></div>`;
    const select = host.querySelector('select');
    const results = host.querySelector('[data-preparation-list]');
    function paint() {
      const chosen = select.value;
      const scoped = shots.filter((shot) => !chosen || (chosen === 'unassigned'
        ? !shot.asset_ids.some((id) => sceneIds.includes(id)) : shot.asset_ids.includes(Number(chosen))));
      const needs = collectNeeds(scoped, assets, variants, missingPurposes);
      results.innerHTML = `<p class="hint">${scoped.length} 个镜头 · ${needs.length} 项待核对</p>`
        + (needs.length ? `<ul class="asset-preparation-list">${needs.map((item) => {
          const asset = assets.get(item.assetId);
          const title = [asset?.name, item.variant?.name].filter(Boolean).join(' · ');
          const canPrepare = asset && store.can('asset:write') && (!asset.is_global || store.isManager);
          return `<li><strong>${escapeHtml(title || (item.assetId ? '#' + item.assetId : '形态引用'))}</strong>
            <span>${escapeHtml(item.issue)} · 镜 ${[...item.seqs].join('、')}</span>
            ${asset ? `<button type="button" class="btn xs" data-prepare-asset="${asset.id}"
              data-prepare-variant="${item.variant?.id ?? ''}" data-prepare-edit="${Boolean(canPrepare)}">${canPrepare ? '去补素材' : '查看资产'}</button>` : ''}</li>`;
        }).join('')}</ul>` : `<p>${!scoped.length ? '没有匹配的分镜。'
          : scoped.some((shot) => shot.asset_ids.length) ? '已引用的资产和形态没有待补的参考图。'
            : '这些分镜还没有引用资产。'}</p>`);
    }
    select.addEventListener('change', paint);
    results.addEventListener('click', (event) => {
      const button = event.target.closest('[data-prepare-asset]');
      if (!button || projectId !== store.currentId || episodeId !== store.currentEpisodeId) return;
      const id = Number(button.dataset.prepareAsset);
      if (button.dataset.prepareEdit === 'true' && button.dataset.prepareVariant) {
        setStudioTarget(id, Number(button.dataset.prepareVariant));
        setParam('assets', null);
        setSub('assets', 1);
      } else {
        setParam('assets', id);
        setSub('assets', 0);
      }
      dialog.close();
      renderView('assets');
    });
    paint();
  } catch (error) {
    if (dialog.open) host.textContent = `读取待备素材失败：${error.message}`;
  }
}
