import { chatgptUsageHtml, mountChatGPTUsage } from './chatgpt-usage.js';
import { api } from '../api.js';
import { store } from '../store.js';
import { escapeHtml } from '../util.js';
import { applyPicks } from '../pick.js';

export function chatgptPlanCard() {
  if (!store.can('job:submit')) return '';
  return `<div class="card mb-card" data-chatgpt-plan>
    <div class="card-head"><h2>ChatGPT 制作计划草案</h2><span class="sub">个人套餐</span></div>
    <div class="card-body chatgpt-plan-body">
      <p>根据制作目标拆解工具与步骤，预览确认后可创建运行。</p>
      <div class="row"><span data-cg-account role="status">正在读取连接状态…</span>
        <button class="btn sm" data-cg-connect data-oai-siwc-button>Continue with ChatGPT</button>
        <button class="btn sm" data-cg-switch hidden>更换账号</button>
        <button class="btn sm" data-cg-status>刷新连接</button>
        <button class="btn sm" data-cg-disconnect hidden>断开连接</button></div>
      <a data-cg-authorize target="_blank" rel="noopener noreferrer" hidden>打开 ChatGPT 授权页面 →</a>
      <div class="chatgpt-plan-fields">
        <label>模型<select class="ctl" data-cg-model aria-label="ChatGPT 模型"><option value="">连接后选择</option></select></label>
        <label>范围<select class="ctl" data-cg-episode aria-label="计划范围"><option value="">整个项目</option></select></label>
      </div>
      <label>制作目标<textarea class="ctl" data-cg-goal rows="3" maxlength="2000"
        placeholder="例：盘点当前集缺少的资产，并安排人物履历与分镜准备步骤"></textarea></label>
      <label class="chatgpt-plan-consent"><input type="checkbox" data-cg-consent>
        <span>同意发送目标、集号、镜头数和平台工具清单给 OpenAI，并使用个人 ChatGPT 套餐额度。</span></label>
      <div class="row"><button class="btn primary sm" data-cg-generate disabled>生成计划草案</button>
        <button class="btn sm" data-cg-recover hidden>查询上次结果</button>
        <a href="https://chatgpt.com/settings/usage" target="_blank" rel="noopener noreferrer">查看套餐用量 ↗</a></div>
      <p class="note" data-cg-notice role="status"></p>
      <div data-cg-result hidden></div>
      <button class="btn sm" data-cg-accept hidden>确认计划，创建暂停的运行</button>
      ${chatgptUsageHtml()}
    </div></div>`;
}

export function mountChatGPTPlan(root, onAccepted) {
  const card = root.querySelector('[data-chatgpt-plan]');
  if (!card) return;
  const projectId = store.current.id;
  const refreshUsage = mountChatGPTUsage(card, projectId);
  const $ = (name) => card.querySelector(`[data-cg-${name}]`);
  const note = (text) => { if (card.isConnected) $('notice').textContent = text; };
  const key = `fv.chatgpt.draft.${store.user?.id}.${projectId}`;
  let lastId = null;
  try { lastId = sessionStorage.getItem(key); } catch { /* 本次会话仍可工作 */ }
  let connected = false;
  let busy = false;
  let revision = 0;
  const ready = () => { $('generate').disabled = busy || !connected || !$('model').value || !$('consent').checked || $('goal').value.trim().length < 4; };
  const paintDraft = (draft) => {
    if (!card.isConnected) return;
    $('accept').hidden = draft.state !== 'ready';
    $('result').hidden = draft.state !== 'ready';
    if (draft.state === 'ready') {
      $('result').innerHTML = `<p class="note">${escapeHtml(draft.goal)} · ${escapeHtml(draft.model)}</p><ol class="chatgpt-plan-steps">${draft.steps.map((step) => `<li>
        <b>${escapeHtml(step.tool_label || step.tool)}</b><span class="chip ${step.risk === 'read' ? 'gray' : 'amber'}">${escapeHtml(step.risk_label || step.risk)}</span>
        <p>${escapeHtml(step.why || '')}</p><details><summary>查看参数</summary><pre>${escapeHtml(JSON.stringify(step.args, null, 2))}</pre></details></li>`).join('')}</ol>`;
      note('草案已生成。确认后创建暂停的运行，进入运行详情检查并恢复执行。');
    } else {
      note(draft.error || '本次生成尚未得到完整结果，可以稍后查询。');
    }
  };
  const recover = async () => {
    try { paintDraft(await api.chatgptDraft(lastId)); } catch (e) { note(e.message); }
  };
  const connection = async () => {
    const seq = ++revision;
    try {
      const state = await api.chatgptConnection();
      if (!card.isConnected || seq !== revision) return;
      connected = state.connected && state.sharing;
      $('account').textContent = state.error || (state.pending ? '等待授权，请完成后刷新连接' : connected ? `已连接 ${state.account || 'ChatGPT'}` : state.connected ? '已登录，请重新授权使用套餐' : '尚未连接');
      if (!state.pending) { $('authorize').hidden = true; $('authorize').removeAttribute('href'); }
      $('disconnect').hidden = !state.connected;
      $('switch').hidden = !state.registered;
      if (connected) {
        const models = await api.chatgptModels();
        if (!card.isConnected || seq !== revision) return;
        const selected = $('model').value;
        $('model').innerHTML = '<option value="">请选择模型</option>' + models.items.map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`).join('');
        if (models.items.some((m) => m.id === selected)) $('model').value = selected;
        applyPicks(card);
        note(models.items.length ? '' : '此账号尚未返回可用模型，请在 ChatGPT 中检查授权与套餐。');
      } else {
        $('model').innerHTML = '<option value="">连接后选择</option>';
      }
    } catch (e) { note(e.message); if (seq === revision) connected = false; }
    if (card.isConnected && seq === revision) ready();
  };
  const authorize = async (newAccount) => {
    $('connect').disabled = true;
    try {
      const out = await api.chatgptAuthorize({ new_account: newAccount });
      if (!card.isConnected) return;
      $('authorize').href = out.url;
      $('authorize').hidden = false;
      note('请点击授权链接，在部署平台的电脑上完成登录，然后刷新连接。');
    } catch (e) { note(e.message); }
    finally { $('connect').disabled = false; }
  };
  $('connect').addEventListener('click', () => authorize(false));
  $('switch').addEventListener('click', () => authorize(true));
  $('status').addEventListener('click', connection);
  $('disconnect').addEventListener('click', async () => {
    try { const out = await api.chatgptDisconnect(); await connection(); note(out.note); }
    catch (e) { note(e.message); }
  });
  ['goal', 'model', 'episode', 'consent'].forEach((name) => $(name).addEventListener(name === 'goal' ? 'input' : 'change', ready));
  $('generate').addEventListener('click', async () => {
    if (busy) return;
    busy = true;
    ready();
    $('accept').hidden = $('result').hidden = true;
    lastId = crypto.randomUUID();
    try { sessionStorage.setItem(key, lastId); } catch { /* 仅记录非敏感请求编号 */ }
    $('recover').hidden = false;
    note('正在生成计划草案…');
    try {
      paintDraft(await api.chatgptGenerate({ request_id: lastId, project_id: projectId,
        episode_id: $('episode').value ? Number($('episode').value) : null,
        goal: $('goal').value.trim(), model: $('model').value, confirmed: $('consent').checked }));
    } catch (e) { note(`${e.message}。可查询上次结果，重新生成将使用新的请求。`); }
    finally { busy = false; if (card.isConnected) { ready(); refreshUsage(); } }
  });
  $('recover').hidden = !lastId;
  $('recover').addEventListener('click', recover);
  $('accept').addEventListener('click', async () => {
    $('accept').disabled = true;
    try {
      const out = await api.chatgptAccept(lastId);
      if (card.isConnected) onAccepted(out.run_id);
    } catch (e) { note(e.message); }
    finally { $('accept').disabled = false; }
  });
  api.episodes(projectId).then((rows) => {
    if (!card.isConnected) return;
    $('episode').innerHTML += rows.map((e) => `<option value="${e.id}">第 ${e.seq} 集${e.title ? ' · ' + escapeHtml(e.title) : ''}</option>`).join('');
    applyPicks(card);
  }).catch((e) => note(`集列表读取失败：${e.message}`));
  connection();
  if (lastId) recover();
}
