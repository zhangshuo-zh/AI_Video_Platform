/**
 * 动作审批（0041）。审核中心的第三栏：审的不是内容，是"允不允许发生一次动作"。
 *
 * 为什么这一栏住在审核中心而不是智能体中心：
 * 签字只有这一个写入口（不变量 3）。智能体中心可以显示"等你审批 3 步"，
 * 但按钮必须把人送到这里来签——否则一个月后平台上会有两个"待审批"，
 * 而它们互不知道对方签过什么。
 *
 * 与另两栏的唯一区别是版本令牌：
 *   文档 → 正文 sha256；产物 → 产物清单摘要；动作 → 那一步的参数指纹。
 * 参数一改指纹就变，旧批准自然失配，和"正文改一个字旧签字作废"同一条道理。
 *
 * 这里**不执行任何东西**。批准写完就结束，推进由 worker 的节拍来读这一行；
 * 所以批完之后界面说的话是"下一拍会执行"，而不是"已执行"。
 */
import { api } from '../api.js';
import { escapeHtml, run } from '../util.js';
import { DECISION_TONE } from '../validation.js';

const RISK_TONE = { read: 'gray', write: 'amber', paid: 'amber', formal: 'red' };
const DECISIONS = [
  { key: 'approved', label: '批准', cls: 'primary' },
  { key: 'rejected', label: '驳回', cls: '' },
  { key: 'returned', label: '退回', cls: '' },
];

/** 一屏数据缓在这里：mount 里的交互不重新发请求（同 models.js 那条做法）。 */
let cache = { queue: [], anchor: 'execute' };
let openStepId = null;
let note = '';
// 签字成功的那一句回执。存成模块变量而不是直接写进 DOM：重绘会换掉整块节点，
// 写进旧节点的文字人看不到（见签字那一支的注解）。下一次打开另一步时清掉。
let flash = null;

export function actionsTabAvailable() {
  return true;
}

export async function renderActions(project) {
  let queue = [];
  let anchor = 'execute';
  let err = null;
  try {
    const data = await api.agentActionReviews(project.id);
    queue = data.items || [];
    anchor = data.anchor || 'execute';
  } catch (e) { err = e; }
  cache = { queue, anchor };
  if (openStepId && !queue.some((s) => s.id === openStepId)) openStepId = null;

  const rows = queue.map((s) => `
    <tr data-agent-step="${s.id}" class="${s.id === openStepId ? 'sel' : ''}">
      <td class="num">${s.seq}</td>
      <td><b>${escapeHtml(s.tool_label)}</b>
        <span class="t-desc">${escapeHtml(s.args_preview || '（无参数）')}</span></td>
      <td><span class="chip ${RISK_TONE[s.risk] || 'gray'}">${escapeHtml(s.risk_label)}</span></td>
      <td>${escapeHtml(s.goal)}
        ${s.episode_id ? `<span class="t-desc">集 #${s.episode_id}</span>` : ''}</td>
      <td><span class="chip ${s.backend === 'cloud' ? 'amber' : 'gray'}">
        ${escapeHtml(s.backend === 'cloud' ? '云端' : (s.backend === 'local' ? '本地' : '未定'))}</span>
        <span class="t-desc">${escapeHtml([s.provider, s.model].filter(Boolean).join(' / ') || '—')}</span></td>
      <td class="num"><span class="t-desc">${escapeHtml(s.approval_expires_at || '—')}</span></td>
      <td class="r"><button class="btn sm ${s.id === openStepId ? '' : 'primary'}"
        data-open-action="${s.id}">${s.id === openStepId ? '收起' : '查看并签'}</button></td>
    </tr>`).join('');

  // 签字回执画在**这一栏顶部**，不画在详情卡里：签完那一步就离开待批队列，
  // 详情卡整块消失——回执写进卡片等于"签成了，但你看不到它说的话"。
  // 这也是它存在模块变量里的原因（`flash` 的注解）。
  const flashBar = flash
    ? `<div class="card"><div class="bar ${flash.tone}">${escapeHtml(flash.text)}`
      + `<button class="btn xs" data-dismiss-flash style="margin-left:8px">知道了</button>`
      + '</div></div>'
    : '';
  return `
    ${err ? `<div class="card"><div class="bar bad">读不到待审批动作：${escapeHtml(err.message)}</div></div>` : ''}
    ${flashBar}
    <div class="card">
      <div class="card-head"><h2>等你审批的动作</h2>
        <span class="sub">批准的是那一组参数：参数一改指纹就变，旧批准自动作废</span></div>
      ${queue.length ? `<div class="table-wrap"><table>
        <thead><tr><th class="num">步</th><th>工具与参数</th><th>风险</th><th>目标</th>
          <th>走哪条轨</th><th class="num">审批有效期</th><th class="r"></th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
        : `<div class="empty"><div class="empty-t">这一屏没有等人批的动作</div>
          <p>智能体只读的步骤会当场做完；要花钱或改正式业务事实的，才会停在这一栏等你。</p>
          <p class="hint">批完不会立刻执行——worker 的下一拍才会真的动。</p></div>`}
    </div>
    ${openStepId ? '<div id="action-detail" class="card mt-card" >'
      + '<div class="card-body"><p class="hint">正在读这一步的参数…</p></div></div>' : ''}`;
}

/** 详情那张卡：把"要送出去什么、按什么计价、绑的是哪个指纹"一次说全。 */
function detailHtml(d) {
  const s = d.step;
  const args = Object.entries(d.args || {}).map(([k, v]) =>
    `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(typeof v === 'string' ? v : JSON.stringify(v))}</dd>`
  ).join('') || '<dt>—</dt><dd>这个工具不收参数</dd>';
  const signed = (d.signed || []).map((r) =>
    `<span class="chip ${DECISION_TONE[r.decision] || 'gray'}">${escapeHtml(r.decision)}
      ${escapeHtml(r.signed_at || '')}</span>`).join(' ') || '<span class="t-desc">还没有签字记录</span>';
  return `
    <div class="card-head"><h2>第 ${s.seq} 步：${escapeHtml(s.tool_label)}</h2>
      <span class="sub">运行目标：${escapeHtml(d.goal)}</span></div>
    <div class="card-body">
      <dl class="kv">${args}</dl>
      <p class="modal-meta">
        <b>往外送什么：</b>${escapeHtml(d.data_out || '（这一档没有登记）')}<br>
        <b>计价：</b>${escapeHtml(d.cost_note || '—')}<br>
        <b>怎么验收：</b>${escapeHtml(d.verify || '—')}<br>
        <b>参数指纹：</b><code>${escapeHtml(d.args_hash)}</code>
        ${signed ? '' : ''}（批准会绑在这一串上）<br>
        <b>有效期到：</b>${escapeHtml(d.expires_at || '未设')}</p>
      <p class="modal-meta">${escapeHtml(d.binding || '')}</p>
      ${d.paid ? `<p class="bar warn" style="border:1px solid var(--warn-line);
          background:var(--warn-soft);padding:8px 12px;border-radius:10px">
        <b>这一步会花钱。</b>${escapeHtml(d.cloud_confirm_note || '')}</p>` : ''}
      <p class="hint">已签记录：${signed}</p>
      <label class="modal-label mt-card" for="action-note" >批注（可选，会跟着签字落库）</label>
      <textarea class="ctl" id="action-note" rows="2"
        placeholder="为什么批／为什么驳，写在这里。复审的人看的是这一句。">${escapeHtml(note)}</textarea>
      <div class="modal-actions">
        ${DECISIONS.map((x) => `<button class="btn ${x.cls}" data-sign="${x.key}">${x.label}</button>`).join('')}
      </div>
      <p class="bar bad" id="action-err" hidden></p>
    </div>`;
}

export async function bindActions(root, refresh) {
  // 回执可以手动关掉：它是「刚才那一下的结果」，不是常驻状态。
  root.querySelector('[data-dismiss-flash]')?.addEventListener('click', () => {
    flash = null;
    refresh();
  });
  root.querySelectorAll('[data-open-action]').forEach((btn) => {
    btn.addEventListener('click', () => run('读取这一步', async () => {
      const id = Number(btn.dataset.openAction);
      openStepId = openStepId === id ? null : id;
      // 点开另一步＝在看另一件事，上一条回执不该跟过来
      flash = null;
      await refresh();
    }));
  });
  const box = root.querySelector('#action-detail');
  if (!box || !openStepId) return;
  const detail = await api.agentStepReview(openStepId);
  box.outerHTML = `<div class="card mt-card" id="action-detail" >${detailHtml(detail)}</div>`;
  const holder = document.getElementById('action-detail');
  const noteEl = holder.querySelector('#action-note');
  noteEl.addEventListener('input', () => { note = noteEl.value; });
  const errEl = holder.querySelector('#action-err');
  holder.querySelectorAll('[data-sign]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      // 锚点用服务端给的那个，不在前端写死字面量：
      // 将来多一个可签点时，前端不该还在签 `execute`
      errEl.hidden = true;
      let signReceipt = '';
      const ok = await run('签字', async () => {
        const out = await api.signAgentStep(openStepId, {
          anchor: cache.anchor, decision: btn.dataset.sign, note: noteEl.value.trim(),
        });
        // 服务端回的 note 是给人看的一句话（"批准已记下，worker 的下一拍会执行"），
        // 原来它**一个字都没被显示**：`run()` 只在失败时说话，成功时静默刷新。
        // 于是人签完只看到列表刷新，不知道签成了没有、接下来该等什么——
        // 那正是《系统设计说明书》§16 第 28 条登记的那处"界面没做到它说的那句话"。
        // 现在收下它，签完留在这一栏顶部；失败了才换成错误话术。
        signReceipt = (out && out.note) || '';
        note = '';
      });
      // run() 已经把原话弹出来了；这里再留一行是因为弹窗关掉之后人还会回到这一屏，
      // 那时唯一的提示应该是页面上的这一句，而不是"刚才好像弹过什么东西"。
      //
      // **成功那一支要写进模块变量、由重绘带回来**：上面 `await refresh()` 已经把
      // 整块重画了，此刻 `errEl` 指向的是**已经被替换掉**的那个节点——往里写文字
      // 写的是一个已经不在文档里的元素，人什么也看不见（第一版就是这么写的，
      // 真浏览器验收当场抓到「回执为空」）。失败那一支反而没这个问题：
      // 失败时 `refresh()` 没跑，节点还活着。
      errEl.hidden = false;
      if (ok) {
        flash = { tone: 'ok', text: signReceipt || '签名已记下。这一栏会在下次刷新时更新。' };
        await refresh();
      } else {
        errEl.className = 'bar bad';
        errEl.textContent = '这一笔没有签成。原因已经说过一次，'
                          + '改一改再试；参数被动过（指纹变了）就会重新回到待审批。';
      }
    });
  });
}
