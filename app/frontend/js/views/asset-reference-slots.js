/**
 * 参考图标准槽位与上传规范（纯前端 Canvas 校验与居中裁剪，复用 image_purposes 字段）。
 */
import { api } from '../api.js';
import { escapeHtml } from '../util.js';

export const MIN_SHORT_SIDE = 512;

export const SLOT_DEFS = {
  character: [
    { key: '面部特写', label: '面部特写（封面）', spec: '1:1 · ≥1024px', ratio: 1, minPx: 1024, hint: '默认头像封面，保五官、发饰与眼神不崩', isCover: true },
    { key: '封面定妆', label: '正面定妆照', spec: '3:4 · ≥1024px', ratio: 3 / 4, minPx: 1024, hint: '角色全身正面造型与镜头首帧基准' },
    { key: '三视图', label: '三视图（正/侧/背）', spec: '16:9 · ≥1536px', ratio: 16 / 9, minPx: 1536, hint: '角色全身正/侧/背一致性基准' },
    { key: '背面细节', label: '背面高清图', spec: '3:4 · ≥768px', ratio: 3 / 4, minPx: 768, hint: '高清背面视角，补足三视图背面细节' },
  ],
  scene: [
    { key: '全景定调', label: '全景定调（封面）', spec: '16:9 · ≥1280px', ratio: 16 / 9, minPx: 1280, hint: '场景构图与氛围总基准', isCover: true },
    { key: '机位结构', label: '机位 / 结构视角', spec: '16:9 · ≥1024px', ratio: 16 / 9, minPx: 1024, hint: '多角度空间关系参考' },
    { key: '光影时段', label: '光影 / 时段变体', spec: '16:9 · ≥1024px', ratio: 16 / 9, minPx: 1024, hint: '日夜或关键灯光状态' },
    { key: '材质细节', label: '局部材质 / 陈设', spec: '1:1 · ≥768px', ratio: 1, minPx: 768, hint: '建筑材质与陈设特写' },
  ],
  prop: [
    { key: '正面全貌', label: '正面全貌（封面）', spec: '1:1 · ≥1024px', ratio: 1, minPx: 1024, hint: '道具完整造型与配色', isCover: true },
    { key: '材质细节', label: '材质 / 纹理特写', spec: '1:1 · ≥768px', ratio: 1, minPx: 768, hint: '铭文、材质反光细节' },
    { key: '比例对照', label: '持握 / 比例对照', spec: '4:3 · ≥768px', ratio: 4 / 3, minPx: 768, hint: '与手部或常规物体比例对照' },
    { key: '展开状态', label: '展开 / 触发状态', spec: '1:1 · ≥768px', ratio: 1, minPx: 768, hint: '变形、发光或开启态' },
  ],
};

export function slotsForKind(kind) {
  return SLOT_DEFS[kind] || SLOT_DEFS.prop;
}

/**
 * 对已加载参考图按槽位规范做自动检核：
 * - bad（红）：短边 < 512px，清晰度不足；
 * - warn（黄）：未达槽位推荐长边分辨率，或宽高比偏离槽位推荐比例超过 ±15%；
 * - ok（绿）：满足规格要求。
 */
export function evaluateReferenceImage(width, height, slot = null) {
  const shortSide = Math.min(width, height);
  const longSide = Math.max(width, height);
  if (shortSide < MIN_SHORT_SIDE) {
    return {
      level: 'bad',
      lowRes: true,
      ratioMismatch: false,
      reasons: [`短边 ${shortSide}px < ${MIN_SHORT_SIDE}px，清晰度不足，建议重传高清图或 AI 超分提清`],
    };
  }
  const reasons = [];
  let lowRes = false;
  let ratioMismatch = false;
  if (slot?.minPx && longSide < slot.minPx) {
    lowRes = true;
    reasons.push(`长边 ${longSide}px < 推荐 ${slot.minPx}px`);
  }
  if (slot?.ratio && height > 0) {
    const curRatio = width / height;
    const diff = Math.abs(curRatio / slot.ratio - 1);
    if (diff > 0.15) {
      ratioMismatch = true;
      const ratioLabel = (slot.spec || '').split('·')[0].trim() || '推荐比例';
      reasons.push(`比例偏离推荐 ${ratioLabel}`);
    }
  }
  if (reasons.length) {
    return { level: 'warn', lowRes, ratioMismatch, reasons };
  }
  return { level: 'ok', lowRes: false, ratioMismatch: false, reasons: [] };
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

function computeCropBox(w, h, targetRatio) {
  if (!targetRatio) return { x: 0, y: 0, w, h };
  const curRatio = w / h;
  let x = 0;
  let y = 0;
  let cw = w;
  let ch = h;
  if (curRatio > targetRatio) {
    cw = Math.round(h * targetRatio);
    x = Math.round((w - cw) / 2);
  } else {
    ch = Math.round(w / targetRatio);
    y = Math.round((h - ch) / 2);
  }
  return { x, y, w: Math.max(1, cw), h: Math.max(1, ch) };
}

function cropFileByBox(file, box) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      try {
        const sx = Math.max(0, Math.min(img.naturalWidth - 1, Math.round(box.x)));
        const sy = Math.max(0, Math.min(img.naturalHeight - 1, Math.round(box.y)));
        const sw = Math.max(1, Math.min(img.naturalWidth - sx, Math.round(box.w)));
        const sh = Math.max(1, Math.min(img.naturalHeight - sy, Math.round(box.h)));
        const canvas = document.createElement('canvas');
        canvas.width = sw;
        canvas.height = sh;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
        const outType = file.type === 'image/webp' ? 'image/webp'
          : (file.type === 'image/jpeg' ? 'image/jpeg' : 'image/png');
        canvas.toBlob((blob) => {
          URL.revokeObjectURL(url);
          if (!blob) return reject(new Error('图片裁剪失败'));
          const cropped = new File([blob], file.name, { type: outType });
          resolve({ file: cropped, width: sw, height: sh });
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
 * 上传前像素校验与交互式可视化裁剪：
 * - 短边 < MIN_SHORT_SIDE 直接拦截并返回 null；
 * - 弹出交互式裁剪弹窗，支持拖拽移动与四角缩放自选裁剪区域，也可直接原图上传。
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

  return await askInteractiveCrop(file, dims, slot);
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

function askInteractiveCrop(file, dims, slot) {
  return new Promise((resolve) => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dlg = document.createElement('dialog');
    dlg.className = 'asset-reference-dialog ad-crop-dialog';
    const previewUrl = URL.createObjectURL(file);

    const presets = [];
    if (slot?.ratio) {
      const shortSpec = (slot.spec || '').split('·')[0].trim() || '推荐';
      presets.push({ id: 'slot', label: `推荐 ${shortSpec}`, ratio: slot.ratio });
    }
    presets.push(
      { id: '3:4', label: '3:4 竖版', ratio: 3 / 4 },
      { id: '16:9', label: '16:9 横版', ratio: 16 / 9 },
      { id: '1:1', label: '1:1 方图', ratio: 1 },
      { id: 'free', label: '自由比例', ratio: null },
    );

    let activePreset = presets[0];
    let lockedRatio = activePreset.ratio;
    let box = computeCropBox(dims.width, dims.height, lockedRatio);
    const minCropPx = Math.min(64, Math.min(dims.width, dims.height));

    const titleText = slot ? `裁剪并上传到「${slot.label}」` : '上传参考图（可选区域裁剪）';
    const ratioBtnsHtml = presets.map((p, i) => (
      `<button type="button" class="btn xs${i === 0 ? ' primary' : ''}" data-ratio-id="${escapeHtml(p.id)}">${escapeHtml(p.label)}</button>`
    )).join('');

    dlg.innerHTML = `<div class="stack">
      <div class="ad-crop-head">
        <h2>${escapeHtml(titleText)}</h2>
        <span class="t-dim">拖选框移位 · 拖四角调大小 · 滚轮缩放画布</span>
      </div>
      <div class="ad-crop-ratios">
        ${ratioBtnsHtml}
        <button type="button" class="btn xs" data-act="reset">居中重置</button>
        <span class="ad-crop-zoom-group">
          <button type="button" class="btn xs" data-zoom="out" title="缩小视图">−</button>
          <button type="button" class="btn xs" data-zoom="reset" data-zoom-label title="重置缩放为 100%">100%</button>
          <button type="button" class="btn xs" data-zoom="in" title="放大视图">＋</button>
        </span>
      </div>
      <div class="ad-crop-stage-wrap" data-crop-wrap>
        <div class="ad-crop-stage" data-crop-stage>
          <img class="ad-crop-img" src="${previewUrl}" alt="裁剪预览" draggable="false">
          <div class="ad-crop-box" data-crop-box>
            <span class="ad-crop-grid" aria-hidden="true"></span>
            <span class="ad-crop-handle nw" data-handle="nw"></span>
            <span class="ad-crop-handle ne" data-handle="ne"></span>
            <span class="ad-crop-handle sw" data-handle="sw"></span>
            <span class="ad-crop-handle se" data-handle="se"></span>
          </div>
        </div>
      </div>
      <p class="ad-crop-meta" data-crop-meta></p>
      <div class="row ad-slot-acts">
        <button class="btn" type="button" data-act="cancel">取消</button>
        <button class="btn" type="button" data-act="raw">原图直接上传</button>
        <button class="btn primary" type="button" data-act="crop">✂️ 裁剪并上传</button>
      </div>
    </div>`;

    const wrap = dlg.querySelector('[data-crop-wrap]');
    const stage = dlg.querySelector('[data-crop-stage]');
    const imgEl = dlg.querySelector('.ad-crop-img');
    const boxEl = dlg.querySelector('[data-crop-box]');
    const metaEl = dlg.querySelector('[data-crop-meta]');
    const zoomLabel = dlg.querySelector('[data-zoom-label]');

    let zoom = 1;
    let baseW = 0;
    let baseH = 0;

    const measureBase = () => {
      if (baseW > 0 && baseH > 0) return true;
      const maxW = Math.max(200, (wrap.clientWidth || 680) - 24);
      const maxH = Math.max(200, Math.round(window.innerHeight * 0.54));
      const scale = Math.min(maxW / dims.width, maxH / dims.height, 1);
      baseW = Math.max(1, Math.round(dims.width * scale));
      baseH = Math.max(1, Math.round(dims.height * scale));
      return true;
    };

    const applyZoom = (nextZoom, focusClientX = null, focusClientY = null) => {
      measureBase();
      const clamped = Math.max(1, Math.min(4, Number(nextZoom.toFixed(2))));
      const prevW = imgEl.clientWidth || baseW;
      const prevH = imgEl.clientHeight || baseH;
      const wrapRect = wrap.getBoundingClientRect();
      const stageRect = stage.getBoundingClientRect();
      const relX = focusClientX !== null
        ? (focusClientX - stageRect.left) / prevW
        : (wrap.scrollLeft + wrapRect.width / 2 - (stageRect.left - wrapRect.left + wrap.scrollLeft)) / prevW;
      const relY = focusClientY !== null
        ? (focusClientY - stageRect.top) / prevH
        : (wrap.scrollTop + wrapRect.height / 2 - (stageRect.top - wrapRect.top + wrap.scrollTop)) / prevH;

      zoom = clamped;
      const newW = Math.round(baseW * zoom);
      const newH = Math.round(baseH * zoom);
      imgEl.style.width = `${newW}px`;
      imgEl.style.height = `${newH}px`;
      wrap.classList.toggle('is-zoomed', zoom > 1.01);
      if (zoomLabel) zoomLabel.textContent = `${Math.round(zoom * 100)}%`;
      paint();

      if (zoom > 1.01) {
        const anchorX = focusClientX !== null ? (focusClientX - wrapRect.left) : wrapRect.width / 2;
        const anchorY = focusClientY !== null ? (focusClientY - wrapRect.top) : wrapRect.height / 2;
        wrap.scrollLeft = Math.max(0, relX * newW + 12 - anchorX);
        wrap.scrollTop = Math.max(0, relY * newH + 12 - anchorY);
      } else {
        wrap.scrollLeft = 0;
        wrap.scrollTop = 0;
      }
    };

    const clampBox = (next) => {
      let { x, y, w, h } = next;
      w = Math.max(minCropPx, Math.min(dims.width, w));
      h = Math.max(minCropPx, Math.min(dims.height, h));
      if (lockedRatio) {
        if (w / h > lockedRatio) w = h * lockedRatio;
        else h = w / lockedRatio;
      }
      x = Math.max(0, Math.min(dims.width - w, x));
      y = Math.max(0, Math.min(dims.height - h, y));
      return { x, y, w, h };
    };

    const paint = () => {
      const sw = imgEl.clientWidth;
      const sh = imgEl.clientHeight;
      if (!sw || !sh) return;
      const sx = sw / dims.width;
      const sy = sh / dims.height;
      boxEl.style.left = `${(box.x * sx).toFixed(2)}px`;
      boxEl.style.top = `${(box.y * sy).toFixed(2)}px`;
      boxEl.style.width = `${(box.w * sx).toFixed(2)}px`;
      boxEl.style.height = `${(box.h * sy).toFixed(2)}px`;
      const rw = Math.round(box.w);
      const rh = Math.round(box.h);
      const tooSmall = Math.min(rw, rh) < MIN_SHORT_SIDE;
      metaEl.innerHTML = `原图 <b>${dims.width}×${dims.height}</b> → 裁剪输出 <b>${rw}×${rh} px</b>`
        + (slot?.spec ? `（推荐 ${escapeHtml(slot.spec)}）` : '')
        + (tooSmall ? ` <span class="ad-crop-warn">· 提示：短边小于 ${MIN_SHORT_SIDE}px，建议放大取景框以保清晰度</span>` : '');
    };

    imgEl.addEventListener('load', () => applyZoom(1), { once: true });

    wrap.addEventListener('wheel', (e) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.15 : (1 / 1.15);
      applyZoom(zoom * factor, e.clientX, e.clientY);
    }, { passive: false });

    dlg.querySelector('[data-zoom="in"]').addEventListener('click', () => applyZoom(zoom * 1.25));
    dlg.querySelector('[data-zoom="out"]').addEventListener('click', () => applyZoom(zoom / 1.25));
    dlg.querySelector('[data-zoom="reset"]').addEventListener('click', () => applyZoom(1));

    let pan = null;
    wrap.addEventListener('pointerdown', (e) => {
      if (e.target.closest('[data-crop-box]') || e.target.closest('[data-handle]')) return;
      if (zoom <= 1.01) return;
      e.preventDefault();
      wrap.setPointerCapture(e.pointerId);
      pan = {
        pointerId: e.pointerId,
        startX: e.clientX,
        startY: e.clientY,
        scrollLeft: wrap.scrollLeft,
        scrollTop: wrap.scrollTop,
      };
    });
    wrap.addEventListener('pointermove', (e) => {
      if (!pan || e.pointerId !== pan.pointerId) return;
      wrap.scrollLeft = pan.scrollLeft - (e.clientX - pan.startX);
      wrap.scrollTop = pan.scrollTop - (e.clientY - pan.startY);
    });
    const endPan = (e) => {
      if (!pan || e.pointerId !== pan.pointerId) return;
      try { wrap.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
      pan = null;
    };
    wrap.addEventListener('pointerup', endPan);
    wrap.addEventListener('pointercancel', endPan);

    let drag = null;
    stage.addEventListener('pointerdown', (e) => {
      const handleEl = e.target.closest('[data-handle]');
      const inBox = e.target.closest('[data-crop-box]');
      if (!handleEl && !inBox) return;
      const sw = imgEl.clientWidth;
      const sh = imgEl.clientHeight;
      if (!sw || !sh) return;
      e.preventDefault();
      stage.setPointerCapture(e.pointerId);
      drag = {
        pointerId: e.pointerId,
        mode: handleEl ? handleEl.dataset.handle : 'move',
        startX: e.clientX,
        startY: e.clientY,
        scaleX: dims.width / sw,
        scaleY: dims.height / sh,
        orig: { ...box },
      };
    });

    stage.addEventListener('pointermove', (e) => {
      if (!drag || e.pointerId !== drag.pointerId) return;
      const dx = (e.clientX - drag.startX) * drag.scaleX;
      const dy = (e.clientY - drag.startY) * drag.scaleY;
      const o = drag.orig;

      if (drag.mode === 'move') {
        box = clampBox({ x: o.x + dx, y: o.y + dy, w: o.w, h: o.h });
        paint();
        return;
      }

      const right = o.x + o.w;
      const bottom = o.y + o.h;
      let nx = o.x;
      let ny = o.y;
      let nw = o.w;
      let nh = o.h;

      if (drag.mode.includes('e')) nw = o.w + dx;
      if (drag.mode.includes('s')) nh = o.h + dy;
      if (drag.mode.includes('w')) {
        nw = o.w - dx;
        nx = right - nw;
      }
      if (drag.mode.includes('n')) {
        nh = o.h - dy;
        ny = bottom - nh;
      }

      if (lockedRatio) {
        const anchorRight = drag.mode.includes('w');
        const anchorBottom = drag.mode.includes('n');
        const maxW = anchorRight ? right : (dims.width - o.x);
        const maxH = anchorBottom ? bottom : (dims.height - o.y);
        const maxByRatioW = Math.min(maxW, maxH * lockedRatio);
        const minByRatioW = Math.max(minCropPx, minCropPx * lockedRatio);
        const desiredW = Math.abs(dx) >= Math.abs(dy) * lockedRatio ? nw : nh * lockedRatio;
        nw = Math.max(minByRatioW, Math.min(maxByRatioW, desiredW));
        nh = nw / lockedRatio;
        nx = anchorRight ? (right - nw) : o.x;
        ny = anchorBottom ? (bottom - nh) : o.y;
      } else {
        if (drag.mode.includes('w')) {
          nw = Math.max(minCropPx, Math.min(right, nw));
          nx = right - nw;
        } else {
          nw = Math.max(minCropPx, Math.min(dims.width - nx, nw));
        }
        if (drag.mode.includes('n')) {
          nh = Math.max(minCropPx, Math.min(bottom, nh));
          ny = bottom - nh;
        } else {
          nh = Math.max(minCropPx, Math.min(dims.height - ny, nh));
        }
      }

      box = clampBox({ x: nx, y: ny, w: nw, h: nh });
      paint();
    });

    const endDrag = (e) => {
      if (!drag || e.pointerId !== drag.pointerId) return;
      try { stage.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
      drag = null;
    };
    stage.addEventListener('pointerup', endDrag);
    stage.addEventListener('pointercancel', endDrag);

    dlg.querySelectorAll('[data-ratio-id]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const found = presets.find((p) => p.id === btn.dataset.ratioId);
        if (!found) return;
        activePreset = found;
        lockedRatio = found.ratio;
        dlg.querySelectorAll('[data-ratio-id]').forEach((b) => b.classList.toggle('primary', b === btn));
        box = computeCropBox(dims.width, dims.height, lockedRatio);
        paint();
      });
    });

    dlg.querySelector('[data-act="reset"]').addEventListener('click', () => {
      box = computeCropBox(dims.width, dims.height, lockedRatio);
      paint();
    });

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
        const out = await cropFileByBox(file, box);
        finish({ file: out.file, slot });
      } catch {
        finish({ file, slot });
      }
    });
    document.body.append(dlg);
    dlg.showModal();
    requestAnimationFrame(() => applyZoom(1));
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

function sampleCornerColor(img) {
  const probe = document.createElement('canvas');
  const pw = Math.min(img.naturalWidth, 128);
  const ph = Math.min(img.naturalHeight, 128);
  probe.width = Math.max(1, pw);
  probe.height = Math.max(1, ph);
  const ctx = probe.getContext('2d');
  ctx.drawImage(img, 0, 0, probe.width, probe.height);
  const data = ctx.getImageData(0, 0, probe.width, probe.height).data;
  const patch = Math.min(4, probe.width, probe.height);
  const corners = [
    [0, 0],
    [probe.width - patch, 0],
    [0, probe.height - patch],
    [probe.width - patch, probe.height - patch],
  ];
  let r = 0;
  let g = 0;
  let b = 0;
  let count = 0;
  for (const [cx, cy] of corners) {
    for (let dy = 0; dy < patch; dy += 1) {
      for (let dx = 0; dx < patch; dx += 1) {
        const idx = ((cy + dy) * probe.width + (cx + dx)) * 4;
        if (data[idx + 3] < 20) continue;
        r += data[idx];
        g += data[idx + 1];
        b += data[idx + 2];
        count += 1;
      }
    }
  }
  if (!count) return '#ffffff';
  const hex = (v) => Math.max(0, Math.min(255, Math.round(v / count))).toString(16).padStart(2, '0');
  return `#${hex(r)}${hex(g)}${hex(b)}`;
}

function computePaddedSize(w, h, targetRatio) {
  const cur = w / h;
  if (cur < targetRatio) {
    return { outW: Math.round(h * targetRatio), outH: h };
  }
  return { outW: w, outH: Math.round(w / targetRatio) };
}

function renderPaddedFile(img, targetRatio, fillColor, fileName, mimeType) {
  return new Promise((resolve, reject) => {
    const { outW, outH } = computePaddedSize(img.naturalWidth, img.naturalHeight, targetRatio);
    const canvas = document.createElement('canvas');
    canvas.width = outW;
    canvas.height = outH;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = fillColor;
    ctx.fillRect(0, 0, outW, outH);
    const dx = Math.round((outW - img.naturalWidth) / 2);
    const dy = Math.round((outH - img.naturalHeight) / 2);
    ctx.drawImage(img, dx, dy, img.naturalWidth, img.naturalHeight);
    const outType = mimeType === 'image/webp' ? 'image/webp'
      : (mimeType === 'image/jpeg' ? 'image/jpeg' : 'image/png');
    canvas.toBlob((blob) => {
      if (!blob) return reject(new Error('补边生成失败'));
      resolve({ file: new File([blob], fileName, { type: outType }), width: outW, height: outH });
    }, outType, 0.94);
  });
}

/**
 * 标黄「比例偏离」参考图一键修正弹窗：
 * - 支持智能采底色补边（不裁主体、0 失真对齐推荐比例），可切换 自动采样 / 纯白 / 纯黑；
 * - 也支持一键切换到交互式裁剪弹窗重裁。
 */
export async function askFixReferenceImage(imageUrl, slot) {
  if (!slot?.ratio) return null;
  const blob = await api.fetchFileBlob(imageUrl);
  const ext = blob.type === 'image/jpeg' ? 'jpg' : (blob.type === 'image/webp' ? 'webp' : 'png');
  const rawFile = new File([blob], `reference.${ext}`, { type: blob.type || 'image/png' });
  const objUrl = URL.createObjectURL(blob);
  let img;
  try {
    img = await new Promise((resolve, reject) => {
      const el = new Image();
      el.onload = () => resolve(el);
      el.onerror = () => reject(new Error('参考图解码失败'));
      el.src = objUrl;
    });
  } catch (err) {
    URL.revokeObjectURL(objUrl);
    throw err;
  }

  const autoColor = sampleCornerColor(img);
  const { outW, outH } = computePaddedSize(img.naturalWidth, img.naturalHeight, slot.ratio);
  const ratioLabel = (slot.spec || '').split('·')[0].trim() || '推荐比例';

  const choice = await new Promise((resolve) => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dlg = document.createElement('dialog');
    dlg.className = 'asset-reference-dialog ad-fix-dialog';
    let bgColor = autoColor;

    dlg.innerHTML = `<div class="stack">
      <div class="ad-crop-head">
        <h2>✨ 修正「${escapeHtml(slot.label)}」比例</h2>
        <span class="t-dim">无损补边对齐 ${escapeHtml(ratioLabel)}，不裁切人物主体</span>
      </div>
      <div class="ad-crop-ratios">
        <span class="t-dim" style="font-size:12px">补边底色：</span>
        <button type="button" class="btn xs primary" data-bg="${escapeHtml(autoColor)}"><span class="ad-color-dot" style="background:${escapeHtml(autoColor)}"></span>自动采样</button>
        <button type="button" class="btn xs" data-bg="#ffffff"><span class="ad-color-dot" style="background:#ffffff"></span>纯白</button>
        <button type="button" class="btn xs" data-bg="#111111"><span class="ad-color-dot" style="background:#111111"></span>纯黑</button>
      </div>
      <div class="ad-fix-preview-wrap">
        <canvas class="ad-fix-canvas" data-fix-canvas></canvas>
      </div>
      <p class="ad-crop-meta">原图 <b>${img.naturalWidth}×${img.naturalHeight}</b> → 补边输出 <b>${outW}×${outH} px</b>（对齐 ${escapeHtml(ratioLabel)}）</p>
      <div class="row ad-slot-acts">
        <button class="btn" type="button" data-act="cancel">取消</button>
        <button class="btn" type="button" data-act="recrop">✂️ 改用交互裁剪</button>
        <button class="btn primary" type="button" data-act="pad">✨ 补边对齐并替换</button>
      </div>
    </div>`;

    const canvas = dlg.querySelector('[data-fix-canvas]');
    const paintPreview = () => {
      const scale = Math.min(560 / outW, 380 / outH, 1);
      const cw = Math.max(1, Math.round(outW * scale));
      const ch = Math.max(1, Math.round(outH * scale));
      canvas.width = cw;
      canvas.height = ch;
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = bgColor;
      ctx.fillRect(0, 0, cw, ch);
      const dw = Math.round(img.naturalWidth * scale);
      const dh = Math.round(img.naturalHeight * scale);
      ctx.drawImage(img, Math.round((cw - dw) / 2), Math.round((ch - dh) / 2), dw, dh);
    };

    dlg.querySelectorAll('[data-bg]').forEach((btn) => {
      btn.addEventListener('click', () => {
        bgColor = btn.dataset.bg;
        dlg.querySelectorAll('[data-bg]').forEach((b) => b.classList.toggle('primary', b === btn));
        paintPreview();
      });
    });

    let settled = false;
    const finish = (val) => {
      if (settled) return;
      settled = true;
      dlg.close();
      dlg.remove();
      if (opener?.isConnected) opener.focus({ preventScroll: true });
      resolve(val);
    };
    dlg.addEventListener('click', (e) => { if (e.target === dlg) finish(null); });
    dlg.addEventListener('cancel', (e) => { e.preventDefault(); finish(null); });
    dlg.querySelector('[data-act="cancel"]').addEventListener('click', () => finish(null));
    dlg.querySelector('[data-act="recrop"]').addEventListener('click', () => finish({ mode: 'recrop' }));
    dlg.querySelector('[data-act="pad"]').addEventListener('click', async () => {
      const btn = dlg.querySelector('[data-act="pad"]');
      btn.disabled = true;
      btn.textContent = '生成中…';
      try {
        const res = await renderPaddedFile(img, slot.ratio, bgColor, rawFile.name, rawFile.type);
        finish({ mode: 'pad', file: res.file });
      } catch {
        finish(null);
      }
    });
    document.body.append(dlg);
    dlg.showModal();
    paintPreview();
  });

  URL.revokeObjectURL(objUrl);
  if (!choice) return null;
  if (choice.mode === 'pad') return choice.file;
  if (choice.mode === 'recrop') {
    const cropped = await askInteractiveCrop(rawFile, { width: img.naturalWidth, height: img.naturalHeight }, slot);
    return cropped ? cropped.file : null;
  }
  return null;
}
