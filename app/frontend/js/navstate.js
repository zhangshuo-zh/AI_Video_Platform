/**
 * 二级导航选中项 ＋ 详情页深链参数。放在独立模块里，是因为它要同时被
 * 侧栏（点击）和视图（读当前项）用到，放在任一侧都会变成循环依赖。
 */
const state = new Map();
// 详情参数单独一张表：它和"当前在第几个二级标签"是两件事，
// 混在一个 Map 里会让 #assets/1（分类）和 #assets/1/a57（详情）看起来像同一种东西
const params = new Map();

export function setSub(viewId, index) {
  state.set(viewId, index);
}

/** 没点过时返回 undefined，让调用方自己决定默认值 */
export function getSub(viewId) {
  return state.get(viewId);
}

/** 详情深链：#assets/1/a57 里那个 57。undefined 表示停在列表。 */
export function setParam(viewId, value) {
  if (value === undefined || value === null || value === '') params.delete(viewId);
  else params.set(viewId, value);
}

export function getParam(viewId) {
  return params.get(viewId);
}

// 跨模块的"一次性草稿"：分镜点「生成」时把项目/集/镜/提示词交给生成中心。
// 不走 params：params 会进地址栏（#generate/0/a[object Object]），
// 而且刷新后不应该再弹一次提交框——它只是一次交棒。
let jobPrefill = null;

export function setJobPrefill(value) {
  jobPrefill = value || null;
}

export function takeJobPrefill() {
  const value = jobPrefill;
  jobPrefill = null;
  return value;
}
