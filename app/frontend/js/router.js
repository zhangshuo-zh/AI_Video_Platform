/** 路由：按 id 渲染视图并同步侧栏高亮、地址栏 hash。 */
import views from './views/index.js';
import { getSub, setSub } from './navstate.js';
import { MODULES } from './modules.js';

let currentId = null;
let firstPaint = true;

export function currentViewId() {
  return currentId;
}

export async function renderView(id) {
  const view = views[id] || views.overview;

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
  // 首屏不淡入：浏览器本身已经有一次白→内容的过程，再叠一层只显得更慢。
  // 给挂载点打标记，CSS 据此跳过入场动画；用户主动切页时才淡。
  root.classList.toggle('cold', firstPaint);
  firstPaint = false;

  root.innerHTML = await view.render();
  view.mount?.(root);          // 视图自己绑事件，路由不掺和

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
  const target = '#' + id + (subIndex === undefined ? '' : '/' + subIndex);
  if (location.hash !== target) history.replaceState(null, '', target);
  window.scrollTo(0, 0);
}
