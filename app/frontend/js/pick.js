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
let nextListId = 0;
const observers = new WeakMap();

function closeActive() {
  if (!active) return;
  active.list.hidden = true;
  active.btn.setAttribute('aria-expanded', 'false');
  active.btn.removeAttribute('aria-activedescendant');
  active = null;
}

/**
 * 自绘下拉的键盘操作。抽成一个函数，四处（项目/集/命令面板/本控件）共用——
 * 原来只有鼠标与 Escape：方向键不动、Home/End 不动、Enter 不选，键盘用户
 * 打开浮层之后只能再 Escape 回去，等于这个控件对他不可用。
 *
 * 约定（与原生 <select> 和 ARIA listbox 一致）：
 *   ↓/↑        在可选项之间移动（跳过 disabled），到头不循环；
 *   Home/End   第一项／最后一项；
 *   Enter/空格  选当前高亮项并关闭；
 *   Escape     关闭并不选（调用方已经处理，这里不重复）；
 *   Tab        关闭（焦点照常走，不拦）。
 *
 * `items` 是本次可见的选项数组，`index` 是当前高亮下标，`onMove(i)` / `onPick(i)`
 * 由调用方实现——这个助手只负责「按了哪个键该干什么」，不碰 DOM 结构。
 * 返回 true 表示这个键已被消费，调用方不必再处理。
 */
export function listboxKeys(e, items, index, onMove, onPick) {
  const n = items.length;
  if (!n) return false;
  // nextEnabled：从 start 起朝 dir 找下一个可选项，不循环（到头返回 -1）
  const nextEnabled = (start, dir) => {
    for (let i = start; i >= 0 && i < n; i += dir) {
      if (!items[i].disabled) return i;
    }
    return -1;
  };
  switch (e.key) {
    case 'ArrowDown': {
      const i = nextEnabled(index < 0 ? 0 : index + 1, 1);
      if (i >= 0) onMove(i);
      return true;
    }
    case 'ArrowUp': {
      const i = nextEnabled(index < 0 ? n - 1 : index - 1, -1);
      if (i >= 0) onMove(i);
      return true;
    }
    case 'Home': {
      const i = nextEnabled(0, 1);
      if (i >= 0) onMove(i);
      return true;
    }
    case 'End': {
      const i = nextEnabled(n - 1, -1);
      if (i >= 0) onMove(i);
      return true;
    }
    case 'Enter':
    case ' ': {
      if (index >= 0 && !items[index].disabled) onPick(index);
      return true;
    }
    case 'Tab':
      return false;   // 让焦点照常走，浮层由调用方的 closeActive 收
    default:
      return false;
  }
}

// 点外面、滚动、改窗口大小都收起：浮层用 position:fixed，页面一动就会错位。
document.addEventListener('click', (e) => {
  if (!active) return;
  if (active.wrap.contains(e.target) || active.list.contains(e.target)) return;
  closeActive();
}, true);
window.addEventListener('scroll', (e) => {
  if (active && (!(e.target instanceof Node) || !active.list.contains(e.target))) closeActive();
}, true);
window.addEventListener('resize', closeActive);

export function enhanceSelect(sel) {
  if (!sel || sel.dataset[MARK]) return;
  sel.dataset[MARK] = '1';

  const wrap = document.createElement('span');
  wrap.className = 'fv-pick';
  // **宽高都照抄原生 `<select>`**，别让外壳按内容自由伸缩。
  //
  // 两处都会让布局跳一下，合起来就是切项目时「列表先一个大小、后又变一个」：
  //   宽度——`.fv-pick` 是 `width:100%`，在 flex 容器里按内容撑开；原生控件
  //         多半带固定宽度（项目中心排序下拉 `.proj-sort{width:112px}`），
  //         实测整块从 318 缩到 296；
  //   高度——原生靠浏览器默认行高约 31px，自绘按钮 `padding:8px 10px` 约 38px，
  //         卡片头从 56 涨到 63，下面整块跟着下移 7px。
  // 量出来写进 inline-style，替换前后就一致了。
  const cs = getComputedStyle(sel);
  if (cs.width && cs.width !== 'auto') {
    wrap.style.width = cs.width;
    wrap.style.flex = sel.style.flex || 'none';
  }
  if (cs.height && cs.height !== 'auto') wrap.style.height = cs.height;
  if (cs.minWidth && cs.minWidth !== '0px') wrap.style.minWidth = cs.minWidth;
  if (cs.maxWidth && cs.maxWidth !== 'none') wrap.style.maxWidth = cs.maxWidth;
  const selHeight = cs.height && cs.height !== 'auto' ? cs.height : '';
  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'fv-pick-btn';
  btn.setAttribute('role', 'combobox');
  btn.setAttribute('aria-haspopup', 'listbox');
  btn.setAttribute('aria-expanded', 'false');
  // 高度与原生控件一致：外壳定了高，按钮填满它（box-sizing:border-box 见 CSS）。
  if (selHeight) btn.style.height = selHeight;
  wrap.appendChild(btn);

  const list = document.createElement('div');
  list.className = 'fv-pick-list';
  list.id = `fv-pick-list-${++nextListId}`;
  btn.setAttribute('aria-controls', list.id);
  list.setAttribute('role', 'listbox');
  list.hidden = true;
  // 挂载到 body：彻底免疫任何祖先容器设置 backdrop-filter/transform/contain 导致的定位基准错乱
  document.body.appendChild(list);
  wrap._fvList = list;

  const paint = () => {
    // 高亮项要用 id 被 aria-activedescendant 指认（键盘移动时读屏要念出来）；
    // aria-selected 只标「已选中的值」，aria-activedescendant 标「复选框中此刻
    // 停在哪一项」，两者不是一个概念，不能互相顶替。
    const hi = active && active.sel === sel ? active.index : -1;
    list.innerHTML = [...sel.options].map((o, i) => (
      `<button type="button" role="option" tabindex="-1" id="${list.id}-opt-${i}" class="fv-pick-opt${o.selected ? ' on' : ''}${i === hi ? ' hi' : ''}"`
      + ` data-i="${i}"${o.disabled ? ' disabled' : ''}`
      + `${o.selected ? ' aria-selected="true"' : ''}`
      + `${i === hi ? ' aria-current="true"' : ''}>${escapeHtml(o.textContent)}</button>`
    )).join('') || '<span class="fv-pick-none">没有可选项</span>';
  };

  const sync = () => {
    const opt = sel.options[sel.selectedIndex];
    btn.textContent = opt ? opt.textContent : '';
    btn.disabled = sel.disabled;
    const label = sel.getAttribute('aria-label');
    if (label) btn.setAttribute('aria-label', label);
    else btn.removeAttribute('aria-label');
    // 有些表单按分支隐藏原生 select（编辑时切成手填），外壳必须跟着藏。
    wrap.hidden = sel.hidden;
    if (!list.hidden) paint();
  };

  /** 浮层该多宽：至少与按钮等宽，按钮装不下最长选项时再放宽到刚好够。
   *
   * 原来这里写死 `${Math.max(width, 160)}px`——160 是个拍出来的下限，
   * 当时想的是「按钮太窄时浮层别挤成一团」。但它对**按钮本来就够宽**的场合
   * 是纯多余：模型仓库那两个筛选下拉 120px，浮层被顶到 160px，看着比控件宽一截，
   * 而里面的字（全部类型/文本/图像…）根本没溢出。
   *
   * 现在改成「按钮宽度」与「最长选项文字宽度」取大：按钮够宽就等宽（浮层与控件
   * 对齐，最好看），按钮太窄才加宽到文字放得下（不会截断选项）。
   * 量文字用 canvas，不往 DOM 里塞隐藏元素——那会在每次打开时触发一次重排。
   */
  const measureText = (text) => {
    const cs = getComputedStyle(list);
    const font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    const ctx = measureText.ctx || (measureText.ctx = (() => {
      const c = document.createElement('canvas');
      return c.getContext('2d');
    })());
    ctx.font = font;
    return Math.ceil(ctx.measureText(text).width);
  };

  const open = () => {
    if (!sel.isConnected) { closeActive(); list.remove(); return; }
    if (sel.disabled) return;
    if (active && active.sel === sel) { closeActive(); return; }
    closeActive();
    active = { sel, btn, list, wrap, index: sel.selectedIndex };
    paint();
    if (sel.selectedIndex >= 0) btn.setAttribute('aria-activedescendant', `${list.id}-opt-${sel.selectedIndex}`);
    list.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    // 先按宽度与方位摆好再量高度：放不下时向上翻。
    const r = btn.getBoundingClientRect();
    // 最长那条选项要多宽（含左右内边距 10+10 与圆角余量 4）
    const longest = Math.max(0, ...[...sel.options]
      .map((o) => measureText(o.textContent || ''))) + 24;
    const gutter = 8;
    const width = Math.min(Math.max(Math.round(r.width), longest), document.documentElement.clientWidth - 2 * gutter);
    list.style.minWidth = list.style.width = `${width}px`;
    list.style.left = `${Math.max(gutter, Math.min(r.left, document.documentElement.clientWidth - gutter - width))}px`;
    list.style.maxHeight = '';
    const below = window.innerHeight - r.bottom - gutter - 4;
    const above = r.top - gutter - 4;
    const upwards = list.offsetHeight > below && above > below;
    list.style.maxHeight = `${Math.max(0, Math.min(list.offsetHeight, upwards ? above : below))}px`;
    list.style.top = `${upwards ? r.top - list.offsetHeight - 4 : r.bottom + 4}px`;

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
    btn.focus();
  });
  btn.addEventListener('keydown', (e) => {
    if (active?.sel !== sel) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        open();
      }
      return;
    }
    if (e.key === 'Tab') { closeActive(); return; }
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      closeActive();
      return;
    }
    const opts = [...sel.options];
    const handled = listboxKeys(e, opts, active.index,
      (i) => {
        active.index = i;
        paint();
        active.list.querySelector(`[data-i="${i}"]`)?.scrollIntoView({ block: 'nearest' });
        btn.setAttribute('aria-activedescendant', `${list.id}-opt-${i}`);
      },
      (i) => {
        sel.selectedIndex = i;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        closeActive();
        sync();
        btn.focus();
      });
    if (handled) e.preventDefault();
  });

  // options 重建、值被程序化改写、禁用/隐藏切换都要让按钮跟上。
  //
  // **光靠 MutationObserver 不够**（2026-11-07 修）：`sel.value = x` 改的是
  // **属性（property）**，不是 HTML 特性（attribute），MutationObserver 一条
  // 都不报。于是所有"程序化设值"的下拉按钮都停在旧文字上——生成中心的
  // `refreshModeSelect()`／`refreshTemplateSelect()`／`applyProfile()`、
  // 模型中心的筛选与编辑态预填，全是这个写法。夜间主题下表现为
  // "控件里写着 A、程序却按 B 在跑"，比显示错了更糟：人会以为自己没选中。
  //
  // 所以再补两条：
  //   ① 劫持这个 select 实例的 `value`／`selectedIndex` 访问器，写进去就叫一声 sync；
  //   ② 视图那一侧仍可 dispatchEvent('change') 通知业务逻辑，两件事互不替代
  //      （change 是"值变了请处理"，这里是"按钮的字要重画"）。
  // 劫持在实例上而不是 `HTMLSelectElement.prototype`：后者会动到全站每一个
  // select，包括没被增强过的，收益是零、风险是全站。
  const syncAccessor = (prop) => {
    const desc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, prop);
    if (!desc || !desc.set) return;
    Object.defineProperty(sel, prop, {
      configurable: true,
      enumerable: true,
      get() { return desc.get.call(this); },
      set(v) {
        desc.set.call(this, v);
        // 原生 setter 对**不合法的值**是"忽略"（不是抛错、也不是清零）：
        // 那样按钮该显示的是当前真实那一项，sync() 读 selectedIndex 正好对上。
        sync();
      },
    });
  };
  syncAccessor('value');
  syncAccessor('selectedIndex');

  const observer = new MutationObserver(sync);
  observers.set(sel, observer);
  observer.observe(sel, {
    childList: true, subtree: true, attributes: true,
    attributeFilter: ['selected', 'disabled', 'hidden', 'label', 'aria-label'],
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
      observers.get(sel)?.disconnect();
      observers.delete(sel);
      const wrap = sel.closest('.fv-pick');
      if (wrap) {
        wrap._fvList?.remove();
        wrap.parentNode.insertBefore(sel, wrap);
        wrap.remove();
      }
      // 切回白天主题时这个 select 要变回"没有额外行为的原生控件"。
      // 上面那两条实例级访问器必须撤掉：留着它等于把"每次设值都重画一个
      // 已经被删掉的按钮"带进下一个主题，白跑一遍 sync()。
      delete sel.value;
      delete sel.selectedIndex;
      delete sel.dataset[MARK];
    });
}
