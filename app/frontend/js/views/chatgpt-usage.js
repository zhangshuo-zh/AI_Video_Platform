import { api } from '../api.js';
import { escapeHtml } from '../util.js';

const STATES = { succeeded: '成功', failed: '失败', unconfirmed: '未确认', generating: '生成中' };
const number = (value) => value == null ? '未记录' : Number(value).toLocaleString('zh-CN');

export function chatgptUsageHtml() {
  return `<details class="chatgpt-usage" data-cg-usage>
    <summary>本平台使用统计</summary>
    <div class="chatgpt-usage-body">
      <div class="chatgpt-usage-controls">
        <label>时间<select class="ctl" data-usage-days><option value="7">最近 7 天</option><option value="30">最近 30 天</option></select></label>
        <label>范围<select class="ctl" data-usage-scope><option value="all">我的可见项目</option><option value="current">当前项目</option></select></label>
        <button class="btn sm" data-usage-refresh>刷新统计</button>
      </div>
      <p class="hint">统计本人在本平台发起的 ChatGPT 草案请求，按北京时间汇总。</p>
      <div data-usage-content role="region" aria-label="ChatGPT 使用统计"></div>
    </div>
  </details>`;
}

function statsHtml(data) {
  const empty = data.total === 0;
  const max = Math.max(1, ...data.daily.map((row) => row.total));
  const trend = data.daily.map((day, index) => {
    const label = `${day.date} · ${day.total} 次 · ` + Object.entries(STATES).map(([state, text]) => `${text} ${day[state]}`).join('，');
    const tick = data.days === 7 || index % 5 === 0 || index === data.daily.length - 1;
    return `<li tabindex="0" aria-label="${escapeHtml(label)}" title="${escapeHtml(label)}">
      <span class="usage-day-count">${data.days === 7 ? day.total : ''}</span>
      <div class="usage-day-track"><div class="usage-day-stack" style="height:${day.total / max * 100}%">
        ${Object.keys(STATES).map((state) => `<span class="usage-${state}" style="height:${day.total ? day[state] / day.total * 100 : 0}%"></span>`).join('')}
      </div></div><span class="usage-day-label">${tick ? day.date.slice(5).replace('-', '/') : ''}</span></li>`;
  }).join('');
  const models = data.models.map((row) => `<li><div><b>${escapeHtml(row.model)}</b><span>${number(row.total)} 次 · ${Math.round(row.total / data.total * 100)}%</span></div>
    <div class="usage-model-track"><span style="width:${row.total / data.total * 100}%"></span></div></li>`).join('')
    || '<li class="usage-model-empty"><p class="hint">暂无模型调用记录</p><div class="usage-model-track"></div></li>';
  return `<div class="usage-summary">
      <div><b>${number(data.total)}</b><span>草案请求</span></div>
      <div><b>${empty ? '—' : Math.round(data.succeeded / data.total * 100) + '%'}</b><span>草案成功率</span></div>
      <div><b>${data.avg_duration_ms == null ? '未记录' : (data.avg_duration_ms / 1000).toFixed(1) + ' 秒'}</b><span>平均请求耗时</span></div>
    </div>
    <div class="usage-chart-head"><h3>每日请求</h3><ul class="usage-legend">${Object.entries(STATES).map(([state, text]) => `<li><i class="usage-${state}"></i>${text} ${number(data[state])}</li>`).join('')}</ul></div>
    <div class="usage-chart${empty ? ' is-empty' : ''}"><ol class="usage-trend">${trend}</ol>
      ${empty ? '<p class="usage-chart-empty">这段时间暂无草案请求</p>' : ''}</div>
    <div class="usage-details-grid"><div><h3>模型分布</h3><ul class="usage-models">${models}</ul></div>
      <div><h3>已报告 Token</h3><dl class="usage-tokens"><div><dt>输入</dt><dd>${number(data.input_tokens)}</dd></div><div><dt>输出</dt><dd>${number(data.output_tokens)}</dd></div></dl>
      <p class="hint">输入已记录 ${data.input_samples}/${data.total} 次，输出已记录 ${data.output_samples}/${data.total} 次；耗时已记录 ${data.duration_samples}/${data.total} 次。</p></div></div>
    ${data.skipped_records ? `<p class="hint">另有 ${data.skipped_records} 条本机记录暂时无法读取。</p>` : ''}`;
}

export function mountChatGPTUsage(card, projectId) {
  const panel = card.querySelector('[data-cg-usage]');
  const content = panel.querySelector('[data-usage-content]');
  const refresh = panel.querySelector('[data-usage-refresh]');
  let seq = 0;
  let loaded = false;
  const load = async () => {
    if (!panel.open || !panel.isConnected) return;
    const current = ++seq;
    loaded = true;
    refresh.disabled = true;
    content.setAttribute('aria-busy', 'true');
    content.textContent = '正在读取使用统计…';
    const query = new URLSearchParams({ days: panel.querySelector('[data-usage-days]').value });
    if (panel.querySelector('[data-usage-scope]').value === 'current') query.set('project_id', projectId);
    try {
      const data = await api.chatgptUsage(query.toString());
      if (!panel.isConnected || current !== seq) return;
      content.innerHTML = statsHtml(data);
    } catch (e) {
      if (!panel.isConnected || current !== seq) return;
      content.textContent = `使用统计读取失败：${e.message}。请点击刷新统计重试。`;
    } finally {
      if (panel.isConnected && current === seq) {
        refresh.disabled = false;
        content.removeAttribute('aria-busy');
      }
    }
  };
  panel.addEventListener('toggle', () => { if (panel.open && !loaded) load(); });
  panel.querySelectorAll('select').forEach((select) => select.addEventListener('change', load));
  refresh.addEventListener('click', load);
  return () => { loaded = false; load(); };
}
