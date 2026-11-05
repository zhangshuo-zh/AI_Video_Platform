/** 一处「跳到哪」的公共实现。
 *
 * 为什么要单独一个模块：任务书 §12.1 要求"点击问题直接定位到具体镜头、素材或配置"。
 * 这件事以前在每个页面各写一遍（生成中心一处、工作台一处、项目工作台又一处），
 * 于是每一页支持的定位参数都不一样——服务端明明在 `locate` 里带了 `shot_id`，
 * 到某页就被丢掉，人只能回到列表自己找。集中一份之后，服务端加一种定位，
 * 所有入口一起受益。
 *
 * 定位表的取值口径见后端 `precheck.py` 的 `_item()` 与 `_shot_locate()`：
 * `module`／`sub` 说去哪儿，`shot_id`／`asset_id`／`job_id` 说去了要打开哪一个。
 */
import { store } from './store.js';
import { setParam, setSub } from './navstate.js';
import { renderView } from './router.js';

/** 认不出的 entry 就什么也不做——按钮点了没反应比跳错地方好。 */
export function gotoEntry(entry) {
  if (!entry || !entry.module) return false;
  if (entry.sub !== undefined && entry.sub !== null) setSub(entry.module, Number(entry.sub));
  if (entry.module === 'shot' && entry.shot_id) {
    setParam('shot', Number(entry.shot_id));
  } else if (entry.module === 'assets' && entry.asset_id) {
    setParam('assets', Number(entry.asset_id));
  } else if (entry.module === 'generate' && entry.job_id) {
    setParam('generate', Number(entry.job_id));
  }
  renderView(entry.module);
  // 带着集号跳转时把顶栏上下文也对齐过去：页面装着第 3 集、顶栏写着第 1 集，
  // 是这类「跳过去看」最容易出现的自相矛盾。放在 render 之后是有意的——
  // `selectEpisode` 会派发一次刷新，先落目标页再刷新，才不会出现「闪回原页」。
  if (entry.episode_id && Number(entry.episode_id) !== store.currentEpisodeId) {
    store.selectEpisode(Number(entry.episode_id));
  }
  return true;
}

/** 把服务端 locate 原样接过来用（少一处字段名对不上的可能）。 */
export function gotoLocate(locate) {
  return gotoEntry(locate);
}
