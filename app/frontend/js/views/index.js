/** 视图注册表：新增一页就在这里加一行。key 必须和 modules.js 的 id 一致。
 *
 *  **从这里开始改成按需加载**（2026-11-11 审计 F-06）。原来是 15 个静态
 *  import——首屏为了画一个「总览」，得先把写作、生成、成片、模型中心那些
 *  一两百 KB 的模块全部下下来并解析完。用 import() 之后每个视图只在
 *  真正打开时才取，首屏只剩总览那一份。
 *
 *  两点写法上的约束（踩过才写的）：
 *  1. 映射表里放的是**函数**而不是 import() 直接返回的 Promise：模块顶层的
 *     import() 会在加载时就发出去，那就又变回一口气全拉了。
 *  2. key 必须和 modules.js 的 id 一致（shot 这个 key 与文件名不同，保持原样）。
 */
const LOADERS = {
  overview: () => import('./overview.js'),
  production: () => import('./production.js'),
  projects: () => import('./projects.js'),
  shot: () => import('./shot-workbench.js'),
  assets: () => import('./assets.js'),
  writing: () => import('./writing.js'),
  generate: () => import('./generate.js'),
  pipeline: () => import('./pipeline.js'),
  composition: () => import('./composition.js'),
  audio: () => import('./audio.js'),
  review: () => import('./review.js'),
  knowledge: () => import('./knowledge.js'),
  agents: () => import('./agents.js'),
  models: () => import('./models.js'),
  'system-config': () => import('./system-config.js'),
};

/** 注册表里有没有这一页（路由用它决定「未知 id 退回总览」）。 */
export function hasView(id) {
  return Object.prototype.hasOwnProperty.call(LOADERS, id);
}

const cache = new Map();

/** 取一页视图对象。同一页第二次调用直接用缓存的那份，不会重复发请求。
 *
 *  返回的是**同一个**模块对象：视图文件里 module 级的变量（比如 generate.js
 *  与 asset-studio.js 里记 visibilitychange 闭包的那个）必须跨渲染保持一份，
 *  否则每次换页都是一份新的、旧的那份再也解绑不掉。
 */
export async function loadView(id) {
  const key = hasView(id) ? id : 'overview';
  if (!cache.has(key)) {
    const mod = await LOADERS[key]();
    cache.set(key, mod.default || mod);
  }
  return cache.get(key);
}

export default LOADERS;
