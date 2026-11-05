/** 异常诊断那一块（任务书 §14）。从生成中心的作业抽屉抽出来共用。
 *
 * 为什么单独一个模块：七字段目录（`error_catalog.py`）本来就是为了"界面各处说同一句话"
 * 才存在的。如果生成中心画一套、产线节点再画一套，两处迟早一处显示「可以直接重试」
 * 另一处显示「不要直接重试」——而那正是这一层要避免的事。
 * 判据与文案都在服务端，这里只负责摆出来；认不出的码服务端会明说「未登记」，
 * 这里就照原样把执行器的原话摆出来，不补一个听起来合理的解释。
 */
import { escapeHtml } from '../util.js';

function kv(label, value) {
  return `<div class="kv"><dt>${label}</dt><dd>${value}</dd></div>`;
}

/** `d` 是一条诊断载荷：`{error_code, error_message, diagnosis, uncertain?, provider_request_id?}`。
 *  作业那边由 `GET /api/jobs/{id}/diagnosis` 给，产线节点那边直接挂在节点对象上——
 *  两处形状一样，所以这一块不用区分来源。 */
export function diagnosisHtml(d, title = '这一条为什么这样，接下来怎么办') {
  if (!d || !d.diagnosis) return '';
  const g = d.diagnosis;
  const chip = (on, yes, no) =>
    `<span class="chip${on ? ' red' : ''}">${escapeHtml(on ? yes : no)}</span>`;
  return `<h3 class="drawer-h">${escapeHtml(title)}</h3>
    <div class="kv-grid">
      ${kv('类型', escapeHtml(g.label) + (g.known ? '' : '（这个码还没进目录）'))}
      ${kv('发生了什么', escapeHtml(g.explain))}
      ${kv('影响范围', escapeHtml(g.impact))}
      ${kv('建议做法', escapeHtml(g.action))}
      ${kv('技术详情', escapeHtml(g.technical))}
      ${kv('执行器原话', escapeHtml(d.error_message || '—'))}
    </div>
    <p class="modal-meta">${chip(g.retryable, '可以直接重试', '不要直接重试')}
      ${chip(g.may_cost, '重试可能再产生费用', '不产生额外费用')}
      ${chip(g.needs_human, '需要人工处理', '可以自己接着做')}
      ${d.uncertain ? `<span class="chip red">云端结果未知｜回执 ${escapeHtml(d.provider_request_id || '—')}</span>` : ''}
      ${g.retry_hint ? `<span class="chip amber">${escapeHtml(g.retry_hint)}</span>` : ''}</p>`;
}
