/**
 * 二级导航选中项。放在独立模块里，是因为它要同时被
 * 侧栏（点击）和视图（读当前项）用到，放在任一侧都会变成循环依赖。
 */
const state = new Map();

export function setSub(viewId, index) {
  state.set(viewId, index);
}

/** 没点过时返回 undefined，让调用方自己决定默认值 */
export function getSub(viewId) {
  return state.get(viewId);
}
