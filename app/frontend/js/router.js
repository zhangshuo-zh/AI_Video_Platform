/** 路由：按 id 渲染视图并同步侧栏高亮、地址栏 hash。 */
import views from './views/index.js';
import { getParam, getSub, setParam, setSub } from './navstate.js';
import { MODULES } from './modules.js';
import { escapeHtml } from './util.js';
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
  const view = views[id] || views.overview;

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

  // 视图 render 抛错必须在这里落地，不能让它变成一条未捕获的 Promise 拒绝：
  // 那样页面会**停在上一屏不动**（root.innerHTML 根本没执行），人看到的是
  // "点了没反应"，控制台里才有一行红字。宁可摆一张"这一页没渲染出来"的卡片。
  let html;
  try {
    html = await view.render();
  } catch (err) {
    if (seq !== renderSeq) return;
    console.error(`[router] ${id} 渲染失败`, err);
    root.classList.toggle('cold', false);
    root.innerHTML = `<section class="view on">
      <div class="page-head"><h1>${escapeHtml(MODULES.find((m) => m.id === id)?.label || id)}</h1></div>
      <div class="card"><div class="empty">
        <div class="empty-t">这一页没有渲染出来</div>
        <p>${escapeHtml(err && err.message ? err.message : String(err))}</p>
        <p class="hint">数据接口出错时页面会走到这里；刷新一次浏览器（Cmd+R）通常能恢复。</p>
      </div></div></section>`;
    return;
  }
  if (seq !== renderSeq) return;      // 已经有一次更晚的导航在跑了，这份作废
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
    // 内容高度可能晚一点才长回来（列表在 mount 里异步填），一次 scrollTo
    // 会被浏览器钳到当前最大滚动位置。所以最多重试 6 次、每次 50ms，
    // 到位就停——比"滚回顶部"和"死等"都好。
    let tries = 0;
    const restoreScroll = () => {
      window.scrollTo(0, scrollY);
      if (Math.abs(window.scrollY - scrollY) > 1 && tries < 6) {
        tries += 1;
        setTimeout(restoreScroll, 50);
      }
    };
    restoreScroll();
    requestAnimationFrame(restoreScroll);
  }

  currentId = id;
  document.body.dataset.view = id;
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
