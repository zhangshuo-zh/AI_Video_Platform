import { store } from '../store.js';
import { getSub, setSub } from '../navstate.js';

/**
 * 资产中心三个工作区的公共件。
 *
 * 单独一个模块是因为三个页面都要知道"自己是第几个工作区"，而
 * `assets.js` 是它们的调度者——让 studio 反向 import assets.js 会形成
 * 循环依赖（ES Module 的环里顶层取值可能还是 undefined）。
 *
 * 顺序必须与 `modules.js` 里 assets.subs 一字不差：侧栏高亮与页内切换器
 * 读的是同一个下标，反着写的结果是"高亮资产库、页面摆的是候选"。
 */
export const WORKSPACES = [
  { key: 'library', label: '资产总览' },
  { key: 'studio', label: '资产创作' },
  { key: 'triage', label: '文本提取' },
];

export const WS_LABELS = WORKSPACES.map((w) => w.label);

/** 当前工作区下标。二级导航越界由 router 在渲染前钳回，这里只兜 undefined。 */
export const wsIndex = () => Math.min(WORKSPACES.length - 1, Math.max(0, getSub('assets') ?? 0));

export const wsKey = () => WORKSPACES[wsIndex()].key;

export function setWsIndex(i) { setSub('assets', i); }

/**
 * 「到创作工作区改**这一条**」的交棒。
 *
 * 为什么不走 navstate 的 params：`#assets/1/a57` 里那个 `a57` 是**详情页**的深链，
 * 渲染分发看到它就整页交给 asset-detail.js 了。创作页要的是"选中目标资产那一格"，
 * 是同一模块里的另一个状态——把两件事挤进一个参数，结果就是上一轮那种
 * 「点了『去资产创作改』却停在资产库详情」的错觉。
 *
 * 也不进地址栏：它是一次交棒，刷新之后不该再弹一次（和分镜→生成中心的
 * `jobPrefill` 同一个道理，见 navstate.js 顶上那段注释）。
 */
let studioTarget = null;
let studioProjectId = null;

export function setStudioTarget(assetId) {
  studioTarget = Number(assetId) || null;
  studioProjectId = null;      // 由调用方在下一个渲染里认领
}

/** 取走并清空：创作页只认一次，避免下次进来还被上一条资产拽回去。 */
export function takeStudioTarget(projectId) {
  if (studioTarget === null) return null;
  if (studioProjectId === null) studioProjectId = projectId;
  if (projectId !== studioProjectId) { studioTarget = null; return null; }
  const value = studioTarget;
  studioTarget = null;
  return value;
}

/**
 * 「现在有没有人在推队列」那一枚 chip。资产库与资产创作共用一份写法：
 * 同一件事在两个工作区说两种话，人就又开始猜了。
 *
 * 读的是 `store.worker`（app.js 每 20 秒刷一次、顶栏也是它），不在这里再发一次请求。
 * 读不到就明说读不到——这一格宁可空着，也不演一个"在线"出来。
 */
export function workerChip() {
  const w = store.worker;
  if (!w) return '<span class="chip gray">Worker 状态未知</span>';
  if (w.online) {
    const q = w.queue || {};
    return `<span class="chip green">Worker 在线</span>`
      + (q.queued || q.running
        ? `<span class="t-dim">排队 ${q.queued ?? 0} · 运行 ${q.running ?? 0}</span>` : '');
  }
  return '<span class="chip red">Worker 离线</span>'
    + '<span class="t-dim">作业会排着等它，不会自己开跑</span>';
}

/**
 * 「这条资产还缺什么」的清单。详情页与分诊页用它。
 *
 * 和资产库卡片上那颗「缺参考图／缺提示词」徽章**不是同一个口径**，也不是重复实现：
 * 列表接口不返回人物档案与形态，那里只能算到图与提示词两项；这一份多出来的
 * 档案／简介／形态三项是详情页本来就已经读回来的数据。
 * 两处都用"缺就是缺"这同一个判据，只是可算的集合不同——把列表也拉去读档案
 * 就是每个资产多两趟回库（v0.97 刚为这类 N+1 收过一轮）。
 */
export function missingOf(asset, profile, variants) {
  if (!asset) return [];
  const out = [];
  if (!(asset.ref_images || []).length) out.push('参考图（第一张就是封面）');
  if (!(asset.consistency_prompt || '').trim()) out.push('一致性提示词');
  if (!(asset.summary || '').trim()) out.push('简介');
  if (asset.kind === 'character') {
    const written = profile && (profile.biography || profile.personality
      || profile.growth_arc || profile.text_prompt);
    if (!written) out.push('人物档案（履历／性格／成长过程）');
    if (!(variants || []).length) out.push('至少一个服饰形态');
  } else if (!(variants || []).length) {
    out.push(asset.kind === 'scene' ? '至少一个视角形态' : '至少一个状态形态');
  }
  return out;
}
