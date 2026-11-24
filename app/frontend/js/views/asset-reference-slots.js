/**
 * 参考图标准槽位与上传规范（纯前端 Canvas 校验与居中裁剪，复用 image_purposes 字段）。
 */
import { escapeHtml } from '../util.js';

export const MIN_SHORT_SIDE = 512;

export const SLOT_DEFS = {
  character: [
    { key: '封面定妆', label: '封面 / 正面定妆', spec: '3:4 · ≥1024px', ratio: 3 / 4, hint: '默认封面与镜头首帧基准', isCover: true },
    { key: '三视图', label: '三视图（正/侧/背）', spec: '16:9 · ≥1536px', ratio: 16 / 9, hint: '角色全身正/侧/背一致性基准' },
    { key: '面部特写', label: '面部特写 / 表情', spec: '1:1 · ≥768px', ratio: 1, hint: '保五官、发饰与眼神不崩' },
    { key: '背面细节', label: '背面 / 服饰细节', spec: '3:4 · ≥768px', ratio: 3 / 4, hint: '补全背部结构与关键配饰' },
  ],
  scene: [
    { key: '全景定调', label: '全景定调（封面）', spec: '16:9 · ≥1280px', ratio: 16 / 9, hint: '场景构图与氛围总基准', isCover: true },
    { key: '机位结构', label: '机位 / 结构视角', spec: '16:9 · ≥1024px', ratio: 16 / 9, hint: '多角度空间关系参考' },
    { key: '光影时段', label: '光影 / 时段变体', spec: '16:9 · ≥1024px', ratio: 16 / 9, hint: '日夜或关键灯光状态' },
    { key: '材质细节', label: '局部材质 / 陈设', spec: '1:1 · ≥768px', ratio: 1, hint: '建筑材质与陈设特写' },
  ],
  prop: [
    { key: '正面全貌', label: '正面全貌（封面）', spec: '1:1 · ≥1024px', ratio: 1, hint: '道具完整造型与配色', isCover: true },
    { key: '材质细节', label: '材质 / 纹理特写', spec: '1:1 · ≥768px', ratio: 1, hint: '铭文、材质反光细节' },
    { key: '比例对照', label: '持握 / 比例对照', spec: '4:3 · ≥768px', ratio: 4 / 3, hint: '与手部或常规物体比例对照' },
    { key: '展开状态', label: '展开 / 触发状态', spec: '1:1 · ≥768px', ratio: 1, hint: '变形、发光或开启态' },
  ],
};

export function slotsForKind(kind) {
  return SLOT_DEFS[kind] || SLOT_DEFS.prop;
}

/**
 * 按固定主槽位分配现有 ref_images，老图未匹配槽位时：
 * 若封面槽为空且首图无冲突用途，优先占位封面槽，其余收入 extra。
 */
export function assignReferenceSlots(kind, refImages = [], imagePurposes = {}) {
  const slots = slotsForKind(kind);
  const byKey = new Map(slots.map((s) => [s.key, null]));
  const usedPaths = new Set();

  for (const p of refImages) {
    const purpose = (imagePurposes?.[p] || '').trim();
    if (purpose && byKey.has(purpose) && !byKey.get(purpose)) {
      byKey.set(purpose, p);
      usedPaths.add(p);
    }
  }

  const coverSlot = slots[0];
  if (coverSlot && !byKey.get(coverSlot.key) && refImages.length) {
    const firstFree = refImages.find((p) => !usedPaths.has(p) && !(imagePurposes?.[p] || '').trim());
    if (firstFree) {
      byKey.set(coverSlot.key, firstFree);
      usedPaths.add(firstFree);
    }
  }

  const extra = refImages.filter((p) => !usedPaths.has(p));
  return {
    slots: slots.map((s) => ({ ...s, path: byKey.get(s.key) || null })),
    extra,
  };
}

export function readImageSize(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const info = { width: img.naturalWidth, height: img.naturalHeight };
      URL.revokeObjectURL(url);
      resolve(info);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('无法读取图片尺寸'));
    };
    img.src = url;
  });
}

function centerCropFile(file, targetRatio) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      try {
        const w = img.naturalWidth;
        const h = img.naturalHeight;
        const curRatio = w / h;
        let sx = 0;
        let sy = 0;
        let sw = w;
        let sh = h;
        if (curRatio > targetRatio) {
          sw = Math.round(h * targetRatio);
          sx = Math.round((w - sw) / 2);
        } else {
          sh = Math.round(w / targetRatio);
          sy = Math.round((h - sh) / 2);
        }
        const canvas = document.createElement('canvas');
        canvas.width = Math.max(1, sw);
        canvas.height = Math.max(1, sh);
        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
        const outType = file.type === 'image/webp' ? 'image/webp'
          : (file.type === 'image/jpeg' ? 'image/jpeg' : 'image/png');
        canvas.toBlob((blob) => {
          URL.revokeObjectURL(url);
          if (!blob) return reject(new Error('图片裁剪失败'));
          const cropped = new File([blob], file.name, { type: outType });
          resolve({ file: cropped, width: canvas.width, height: canvas.height });
        }, outType, 0.94);
      } catch (err) {
        URL.revokeObjectURL(url);
        reject(err);
      }
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('图片解码失败'));
    };
    img.src = url;
  });
}

/**
 * 上传前像素与比例校验：
 * - 短边 < MIN_SHORT_SIDE 直接拦截并返回 null；
 * - 若指定槽位且比例偏差 > 18%，弹窗提供「居中裁剪」或「原比例上传」。
 */
export async function prepareSlotUpload(file, slot = null) {
  let dims;
  try {
    dims = await readImageSize(file);
  } catch {
    return { file, slot };
  }
  const shortSide = Math.min(dims.width, dims.height);
  if (shortSide < MIN_SHORT_SIDE) {
    showNoticeDialog(
      '图片清晰度不足',
      `<p>当前图片尺寸为 <b>${dims.width}×${dims.height}</b>，短边小于 ${MIN_SHORT_SIDE}px。</p>
       <p class="t-dim">低分辨率参考图会导致后续生图/生视频人脸或细节崩坏，请上传高清原图。</p>`,
    );
    return null;
  }

  if (!slot || !slot.ratio) return { file, slot };
  const curRatio = dims.width / dims.height;
  const diff = Math.abs(curRatio - slot.ratio) / slot.ratio;
  if (diff <= 0.18) return { file, slot };

  return await askCropChoice(file, dims, slot);
}

function showNoticeDialog(title, bodyHtml) {
  const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const dlg = document.createElement('dialog');
  dlg.className = 'asset-reference-dialog';
  dlg.innerHTML = `<form method="dialog" class="stack">
    <h2>${escapeHtml(title)}</h2>${bodyHtml}
    <div><button class="btn primary" value="cancel">知道了</button></div>
  </form>`;
  dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
  dlg.addEventListener('close', () => {
    dlg.remove();
    if (opener?.isConnected) opener.focus({ preventScroll: true });
  }, { once: true });
  document.body.append(dlg);
  dlg.showModal();
}

function askCropChoice(file, dims, slot) {
  return new Promise((resolve) => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dlg = document.createElement('dialog');
    dlg.className = 'asset-reference-dialog';
    const previewUrl = URL.createObjectURL(file);
    dlg.innerHTML = `<div class="stack">
      <h2>「${escapeHtml(slot.label)}」比例建议</h2>
      <img class="asset-reference-preview" src="${previewUrl}" alt="待传预览">
      <p>当前尺寸 <b>${dims.width}×${dims.height}</b>，该槽位推荐规格为 <b>${escapeHtml(slot.spec)}</b>。</p>
      <p class="hint">居中裁剪可保证三视图/定妆照构图统一；若原图已包含完整结构，也可保持原比例上传。</p>
      <div class="row ad-slot-acts">
        <button class="btn" type="button" data-act="cancel">取消</button>
        <button class="btn" type="button" data-act="raw">原比例上传</button>
        <button class="btn primary" type="button" data-act="crop">按推荐比例居中裁剪</button>
      </div>
    </div>`;
    let settled = false;
    const finish = (val) => {
      if (settled) return;
      settled = true;
      URL.revokeObjectURL(previewUrl);
      dlg.close();
      dlg.remove();
      if (opener?.isConnected) opener.focus({ preventScroll: true });
      resolve(val);
    };
    dlg.addEventListener('click', (e) => { if (e.target === dlg) finish(null); });
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); finish(null); });
    dlg.querySelector('[data-act="cancel"]').addEventListener('click', () => finish(null));
    dlg.querySelector('[data-act="raw"]').addEventListener('click', () => finish({ file, slot }));
    dlg.querySelector('[data-act="crop"]').addEventListener('click', async () => {
      const btn = dlg.querySelector('[data-act="crop"]');
      btn.disabled = true;
      btn.textContent = '裁剪中…';
      try {
        const out = await centerCropFile(file, slot.ratio);
        finish({ file: out.file, slot });
      } catch {
        finish({ file, slot });
      }
    });
    document.body.append(dlg);
    dlg.showModal();
  });
}

export function askReversePromptApply(res, hasExisting) {
  return new Promise((resolve) => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dlg = document.createElement('dialog');
    dlg.className = 'asset-reference-dialog';
    dlg.innerHTML = `<div class="stack">
      <h2>看图反推一致性提示词</h2>
      <p class="hint">来源：<b>${escapeHtml(res.provider)} / ${escapeHtml(res.model)}</b>（已分析 ${res.used_images?.length || 1} 张参考图）</p>
      <textarea class="ctl ad-rev-preview" rows="6">${escapeHtml(res.prompt || '')}</textarea>
      <div class="row ad-slot-acts">
        <button class="btn" type="button" data-act="cancel">取消</button>
        ${hasExisting ? '<button class="btn" type="button" data-act="append">追加到末尾</button>' : ''}
        <button class="btn primary" type="button" data-act="replace">${hasExisting ? '覆盖当前提示词' : '填入提示词'}</button>
      </div>
    </div>`;
    let settled = false;
    const finish = (mode) => {
      if (settled) return;
      settled = true;
      const text = dlg.querySelector('textarea')?.value.trim() || '';
      dlg.close();
      dlg.remove();
      if (opener?.isConnected) opener.focus({ preventScroll: true });
      resolve(mode ? { mode, text } : null);
    };
    dlg.addEventListener('click', (e) => { if (e.target === dlg) finish(null); });
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); finish(null); });
    dlg.querySelector('[data-act="cancel"]').addEventListener('click', () => finish(null));
    dlg.querySelector('[data-act="append"]')?.addEventListener('click', () => finish('append'));
    dlg.querySelector('[data-act="replace"]').addEventListener('click', () => finish('replace'));
    document.body.append(dlg);
    dlg.showModal();
  });
}
