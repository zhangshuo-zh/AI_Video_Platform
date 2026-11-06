/**
 * 夜间主题的自绘下拉。
 *
 * 为什么需要这一层：macOS 上 `<select>` 展开的选项列表由系统菜单绘制，
 * `color-scheme: dark`（app.css 的 `[data-theme=night]`）只改控件本身，
 * 改不到那一层——系统外观切成深色也一样，实测仍是浅灰菜单。
 *
 * 做法：只在夜间把原生 select 藏起来，用「按钮 + 深色浮层」代替；
 * 选值仍写回原生 `<select>` 并派发 `change`，所以各视图既有的
 * `onchange`／`fillPicks` 一行都不用改。白天继续用原生控件。
 */
import { escapeHtml } from './util.js';

const MARK = 'fvPick';
let active = null;

function closeActive() {
  if (!active) return;
  active.list.hidden = true;
  active.btn.setAttribute('aria-expanded', 'false');
  active = null;
}

// 点外面、滚动、改窗口大小都收起：浮层用 position:fixed，页面一动就会错位。
document.addEventListener('click', (e) => {
  if (!active) return;
  if (active.wrap.contains(e.target) || active.list.contains(e.target)) return;
  closeActive();
}, true);
window.addEventListener('scroll', closeActive, true);
window.addEventListener('resize', closeActive);

export function enhanceSelect(sel) {
  if (!sel || sel.dataset[MARK]) return;
  sel.dataset[MARK] = '1';

  const wrap = document.createElement('span');
  wrap.className = 'fv-pick';
  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'fv-pick-btn';
  btn.setAttribute('aria-haspopup', 'listbox');
  btn.setAttribute('aria-expanded', 'false');
  wrap.appendChild(btn);

  const list = document.createElement('div');
  list.className = 'fv-pick-list';
  list.setAttribute('role', 'listbox');
  list.hidden = true;
  wrap.appendChild(list);

  const paint = () => {
    list.innerHTML = [...sel.options].map((o, i) => (
      `<button type="button" role="option" class="fv-pick-opt${o.selected ? ' on' : ''}"`
      + ` data-i="${i}"${o.disabled ? ' disabled' : ''}`
      + `${o.selected ? ' aria-selected="true"' : ''}>${escapeHtml(o.textContent)}</button>`
    )).join('') || '<span class="fv-pick-none">没有可选项</span>';
  };

  const sync = () => {
    const opt = sel.options[sel.selectedIndex];
    btn.textContent = opt ? opt.textContent : '';
    btn.disabled = sel.disabled;
    // 有些表单按分支隐藏原生 select（编辑时切成手填），外壳必须跟着藏。
    wrap.hidden = sel.hidden;
    if (!list.hidden) paint();
  };

  const open = () => {
    if (sel.disabled) return;
    if (active && active.sel === sel) { closeActive(); return; }
    closeActive();
    paint();
    list.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    // 先按宽度与方位摆好再量高度：放不下时向上翻。
    const r = btn.getBoundingClientRect();
    list.style.minWidth = `${Math.max(Math.round(r.width), 160)}px`;
    list.style.left = `${Math.round(r.left)}px`;
    const h = list.offsetHeight;
    const below = window.innerHeight - r.bottom - 8;
    list.style.top = (h > below && r.top > h + 8)
      ? `${Math.round(r.top - h - 4)}px`
      : `${Math.round(r.bottom + 4)}px`;
    active = { sel, btn, list, wrap };
  };

  btn.addEventListener('click', open);
  list.addEventListener('click', (e) => {
    const hit = e.target.closest('.fv-pick-opt');
    if (!hit || hit.disabled) return;
    sel.selectedIndex = Number(hit.dataset.i);
    // 走原生事件，视图里既有的 onchange／fillPicks 照旧收到。
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    closeActive();
    sync();
  });
  sel.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeActive(); });

  // options 重建、值被程序化改写、禁用/隐藏切换都要让按钮跟上。
  new MutationObserver(sync).observe(sel, {
    childList: true, subtree: true, attributes: true,
    attributeFilter: ['selected', 'disabled', 'hidden', 'label'],
  });
  sync();
}

export function applyPicks(root = document) {
  if (document.documentElement.dataset.theme !== 'night') return;
  root.querySelectorAll('select.ctl:not([data-fv-pick])').forEach(enhanceSelect);
}

export function restorePicks(root = document) {
  closeActive();
  root.querySelectorAll('[data-fv-pick]').forEach((sel) => {
      const wrap = sel.closest('.fv-pick');
      if (wrap) {
        wrap.parentNode.insertBefore(sel, wrap);
        wrap.remove();
      }
      delete sel.dataset[MARK];
    });
}
