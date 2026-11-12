/** 路由：按 id 渲染视图并同步侧栏高亮、地址栏 hash。 */
import { loadView, hasView } from './views/index.js';
import { getParam, getSub, setParam, setSub } from './navstate.js';
import { MODULES } from './modules.js';
import { escapeHtml, announce } from './util.js';
import { applyPicks } from './pick.js';

let currentId = null;
let firstPaint = true;
// 每一次渲染的序号。`view.render()` 是 async 的（十几个视图里都有 await），
// 快速切页时先发出的那一次可能后回来，把已经不该显示的旧页面盖回屏幕上——
// 各视图自己判 `isConnected` 只能挡住"往死节点上写"，挡不住"晚到的整页覆盖新页"。
// 这里是所有渲染的唯一入口，所以在这一处判一次，十四个视图一起受益。
let renderSeq = 0;

/**
 * 重绘会替换整个 `#view-root`，焦点与滚动位置默认一起丢。
 * 保存"人在哪、看到哪"，渲染后放回去——多数刷新（保存、切页签、轮询）
 * 并不是人想换地方，只是数据变了。
 */
function captureFocus(root) {
  const el = document.activeElement;
  if (!el || el === document.body || !root.contains(el)) return null;
  let sel = el.id ? `#${CSS.escape(el.id)}` : '';
  if (!sel) {
    for (const attr of ['data-k', 'data-field', 'data-act', 'data-i', 'name']) {
      const v = el.getAttribute(attr);
      if (v) { sel = `[${attr}="${CSS.escape(v)}"]`; break; }
    }
  }
  if (!sel) return null;
  const snap = { sel, start: null, end: null };
  try {
    snap.start = el.selectionStart;
    snap.end = el.selectionEnd;
  } catch { /* 不是文本控件 */ }
  return snap;
}

function restoreFocus(root, snap) {
  if (!snap) return;
  const el = root.querySelector(snap.sel);
  if (!el) return;
  // preventScroll 让 focus 不再把页面拽回去；滚动位置由下面单独恢复。
  try { el.focus({ preventScroll: true }); } catch { el.focus(); }
  if (snap.start != null && typeof el.setSelectionRange === 'function') {
    try { el.setSelectionRange(snap.start, snap.end); } catch { /* 类型不支持选区 */ }
  }
}

export function currentViewId() {
  return currentId;
}

/** 打开某模块的详情：地址变成 #assets/1/a57，刷新与粘贴都回得到那一条。 */
export function openDetail(viewId, param) {
  setParam(viewId, param);
  return renderView(viewId);
}

export function closeDetail(viewId) {
  setParam(viewId, undefined);
  return renderView(viewId);
}

export async function renderView(id) {
  const seq = ++renderSeq;
  // 视图按需加载（2026-11-11 审计 F-06）。这一句必须在**任何** DOM 改动之前
  // await：取模块要走一次网络，中途人可能已经点了别处，等它回来再看 seq。
  // 所以「拿视图」放在最前面，拿不到就不必再动屏幕。
  //
  // `hasView` 判一次是保留原来的语义：未知 id 退回总览（不是空白页）。
  // loadView 内部也做了同样的兜底，这里判是为了下面取 label 时也走同一条路。
  const viewId = hasView(id) ? id : 'overview';
  let view;
  try {
    view = await loadView(viewId);
  } catch (err) {
    // 模块取不下来（网络断了、文件名写错）与 render() 抛错是两件事，但表现
    // 都是「点了没反应」。走同一个兜底卡片，别让人对着上一屏发呆。
    if (seq !== renderSeq) return;
    console.error(`[router] ${id} 视图模块加载失败`, err);
    const root0 = document.getElementById('view-root');
    root0.classList.toggle('cold', false);
    root0.innerHTML = `<section class="view on">
      <div class="page-head"><h1>${escapeHtml(MODULES.find((m) => m.id === id)?.label || id)}</h1></div>
      <div class="card"><div class="empty">
        <div class="empty-t">这一页没有加载出来</div>
        <p>${escapeHtml(err && err.message ? err.message : String(err))}</p>
        <p class="hint">页面代码没能取回来，请检查网络后重试。</p>
      <button class="btn" data-page-retry>重新加载本页</button>
      </div></div></section>`;
    document.querySelector('[data-page-retry]')?.addEventListener('click', () => renderView(id));
    return;
  }
  // 取模块期间又发生了一次导航：这一次作废，不要覆盖更新的那一屏。
  if (seq !== renderSeq) return;

  // 未知 id 已经在这里被换成 overview 了，下面所有落点都该用换过的那个：
  // 原来只有"画哪一屏"用了兜底值，而 `body[data-view]`、侧栏高亮、地址栏
  // 写的仍是那个不存在的 id——人看到的页面是总览、外壳状态却认着另一个名字。
  // （2026-11-11 审计 F-06 一并修掉：`#nope-not-a-view` 实测 body 上写的就是
  //  nope-not-a-view。）CSS 与调试都读这个属性，它得和屏幕上那一屏一致。
  id = viewId;

  // 换模块时把上一个模块的详情参数清掉：从资产详情跳去创作中心再跳回来，
  // 应该回到列表，而不是莫名其妙落在那条旧资产上（它可能已经被删了）。
  if (currentId && currentId !== id) setParam(currentId, undefined);

  // 深链可能带着已经不存在的二级页下标（撤掉成本账之后的 #projects/2 就是这种），
  // 在渲染之前钳回第一项，视图和侧栏高亮看到的才是同一个值。
  const subCount = (MODULES.find((m) => m.id === id)?.subs || []).length;
  if (!subCount) {
    // 模块根本没有二级页（项目中心 v0.46 起合并成一页）：
    // 清掉残留下标，地址栏下面才会归一成 #projects 而不是一直挂着 #projects/1
    setSub(id, undefined);
  } else if (getSub(id) !== undefined && getSub(id) >= subCount) {
    setSub(id, 0);
  }

  const root = document.getElementById('view-root');

  // 同视图刷新（保存、轮询、app:refresh、切项目）保留焦点与滚动；换视图才归零。
  // **必须算在 cold 之前**：入场动画的判据要用到它（见下）。
  const sameView = currentId === id;

  // 入场动画只在"人主动换了一页"时播。三档写清：
  //   首屏（firstPaint）  不播——浏览器本身已有一次白→内容，叠加只会更慢；
  //   同视图重绘（sameView）不播——保存、轮询、**切项目**都会走到这里，
  //                       播一遍就是闪一下（整块从透明+裁切变不透明），
  //                       而切项目一次连发两三条 refresh，肉眼就是连续闪屏；
  //   换视图              播——这时整页内容确实换了，淡入帮人接住这次跳转。
  // 原来这里只判 `firstPaint`：首屏之后它恒为 false，于是每一次同视图刷新
  // 都会重播 `view-in`（`.cold` 只压在首屏），切项目就闪。
  root.classList.toggle('cold', firstPaint || sameView);
  firstPaint = false;

  const focusSnap = sameView ? captureFocus(root) : null;
  const scrollY = sameView ? window.scrollY : 0;
  const innerTop = sameView ? root.scrollTop : 0;
  const innerLeft = sameView ? root.scrollLeft : 0;

  // 这个坐标是在 `await view.render()` **之前**取的，而 render 要走一次网络。
  // 人在这几百毫秒里很可能自己滚了一下——那一动比手里这个旧坐标新，是他的
  // 意图。落地时若无条件写回，就是在人已经看别处之后把页面拽回原处。
  // 所以只认**手势**（滚轮/触摸/键盘），程序性滚动不算：内容替换会让浏览器
  // 自己钳一次 scrollY，那不是人干的，不能当成"他滚了"。
  let movedWhileWaiting = false;
  const noteMove = () => { movedWhileWaiting = true; };
  const dropMoveWatch = () => {
    window.removeEventListener('wheel', noteMove);
    window.removeEventListener('touchmove', noteMove);
    window.removeEventListener('keydown', noteMove);
  };
  if (sameView) {
    window.addEventListener('wheel', noteMove, { passive: true });
    window.addEventListener('touchmove', noteMove, { passive: true });
    window.addEventListener('keydown', noteMove);
  }

  // 视图 render 抛错必须在这里落地，不能让它变成一条未捕获的 Promise 拒绝：
  // 那样页面会**停在上一屏不动**（root.innerHTML 根本没执行），人看到的是
  // "点了没反应"，控制台里才有一行红字。宁可摆一张"这一页没渲染出来"的卡片。
  let html;
  try {
    html = await view.render();
  } catch (err) {
    dropMoveWatch();
    if (seq !== renderSeq) return;
    console.error(`[router] ${id} 渲染失败`, err);
    root.classList.toggle('cold', false);
    root.innerHTML = `<section class="view on">
      <div class="page-head"><h1>${escapeHtml(MODULES.find((m) => m.id === id)?.label || id)}</h1></div>
      <div class="card"><div class="empty">
        <div class="empty-t">这一页没有渲染出来</div>
        <p>${escapeHtml(err && err.message ? err.message : String(err))}</p>
        <p class="hint">暂时无法读取页面数据，请检查连接后重试。</p>
      <button class="btn" data-page-retry>重新加载本页</button>
      </div></div></section>`;
    document.querySelector('[data-page-retry]')?.addEventListener('click', () => renderView(id));
    return;
  }
  if (seq !== renderSeq) { dropMoveWatch(); return; }  // 已有更晚的导航在跑，这份作废
  root.innerHTML = html;
  view.mount?.(root);          // 视图自己绑事件，路由不掺和
  // 夜间主题的把 `<select>` 换成自绘下拉，必须**在这一次渲染里就做完**。
  // 原来只靠 `app.js` 的 MutationObserver 补，带 80ms 去抖——于是每次重绘
  // （包括切项目）都先摆出原生 select、80ms 后才换成自绘的那个，两者宽度不同
  // （项目中心那个排序下拉：原生 112px、自绘 146px），列表跟着跳一次再跳回来。
  // 人看到的就是"列表先一个大小，后来又变成另一个大小"。
  // 这里同步补一次，把它压进同一帧；Observer 仍留着兜底（弹窗异步填充、
  // 视图 mount 之后再插 select 的那些地方）。
  applyPicks(root);

  root.scrollTop = innerTop;
  root.scrollLeft = innerLeft;
  restoreFocus(root, focusSnap);
  if (sameView) {
    // 一次到位。原来这里是个 6 次 × 50ms 的重试循环：那时列表由 mount 异步
    // 填内容，页面先矮着，scrollTo 当场被浏览器钳到"当前最大滚动位置"，
    // 只能反复试到内容长够为止——最长拖 300ms，而且每一跳都落在肉眼可见的
    // 位置，人看到的是页面自己往下"蹭"了三四下。
    //
    // 现在异步填充的地方摆的是骨架占位（util.js 的 `skeleton()`），高度当场
    // 就是最终的形状，一次 scrollTo 就已经滚得到位——那个循环失去了存在理由。
    // 留着它反而有害：它会对着**已经正确**的位置反复写 window.scrollY，
    // 人在这 300ms 里主动滚动会被它拽回去。
    //
    // 同一个道理也适用于"一次到位"：位置是 render 之前量的，人在这段时间里
    // 自己滚过就别再写——他手上的动作比这个旧坐标新。
    if (!movedWhileWaiting) window.scrollTo(0, scrollY);
  }
  dropMoveWatch();

  currentId = id;
  document.body.dataset.view = id;
  // 换页要报一次：单页应用里地址变了、整块内容换了，读屏却可能一个字都不说，
  // 人还以为按钮没生效。`app:go` 才是"人主动换页"的入口，同视图重绘不报——
  // 保存一次就念一遍页面名，那是噪音。
  if (!sameView) {
    // 名字只从 MODULES 取：视图对象上没挂名字（那几个文件导出的是
    // render/mount，不是导航元数据），侧栏、首页、命令面板读的也是它。
    const label = MODULES.find((m) => m.id === id)?.label || id;
    announce(`已进入${label}`);
  }
  document.querySelectorAll('.nav-item').forEach((el) => {
    el.classList.toggle('on', el.dataset.v === id);
  });

  // 二级导航的高亮由视图自己声明（subIndex），默认第一个
  const group = document.querySelector(`.nav-item[data-v="${id}"]`)?.closest('.nav-group');
  if (group) {
    group.querySelectorAll('.sub').forEach((el, i) => {
      el.classList.toggle('on', i === (getSub(id) ?? view.subIndex ?? 0));
    });
  }
  const subIndex = getSub(id);
  const param = getParam(id);
  // 有详情参数时二级下标不能省——地址是 #<view>/<sub>/a<id> 三段式，
  // 少了中间那段，回来时就没法同时恢复"在哪个分类下"
  let target = '#' + id;
  if (param !== undefined) target += '/' + (subIndex ?? 0) + '/a' + param;
  else if (subIndex !== undefined) target += '/' + subIndex;
  if (location.hash !== target) history.replaceState(null, '', target);
}
