// api.js 用的是**命名导出** `export const api = {...}`（不是默认导出，
// 也不是纯命名空间）。写成 `import * as api` 会拿到 `{ api: {...}, toJson... }`，
// 于是 `api.runtimePaths` 是 undefined——页面报 "api.runtimePaths is not a
// function" 并显示"这一页没有渲染出来"。2026-11-11 搬家时连踩两次这个坑
// （先写命名空间、再猜默认导出），真浏览器一眼可见，但 js_load_check 只验
// "模块能加载"，验不到这个；所以本页的验收必须留在浏览器脚本里。
import { api } from '../api.js';
import { alertNotify, cache } from './models/shared.js';
import { confirmDanger, escapeHtml } from '../util.js';

/* ================= 供应商密钥（0052 密文存储，2026-11-11） =================
 * 管理员在页面上配密钥，值以 AES-256-GCM 密文落库，KEK 只在环境变量里。
 *
 * 三条前端纪律（明文只许存在一次，就在那个 input 里）：
 *   1. **保存成功后立刻清空输入框**。不回填、不缓存、不写 localStorage。
 *   2. **页面永远不显示明文**，只显示指纹（sha256 前 8 位）与长度。
 *   3. 环境变量优先时明说"库里的值不生效"——否则人会以为保存失败了。
 *
 * 为什么做成"点开一行才出现输入框"而不是每行常驻 input：常驻会让整页
 * 的 DOM 里躺着 8 个 type=password 的框，浏览器密码管理器会来抢着保存，
 * 而且截屏/投屏时更容易露。点开才渲染，关闭即销毁。
 */
export function secretValuesCard() {
  const data = cache.secretValues || { items: [], kek_ready: false, kek_env: '', note: '' };
  const items = data.items || [];
  if (!items.length) return '';
  const ready = data.kek_ready;

  const rows = items.map((it) => {
    const tone = !it.present ? 'gray' : it.source === 'env' ? 'blue' : 'green';
    const srcText = !it.present ? '未配置'
      : it.source === 'env' ? '来自环境变量'
        : '来自这里保存的密文';
    return `<tr data-sv="${escapeHtml(it.env_name)}">
      <td><b>${escapeHtml(it.env_name)}</b></td>
      <td>${it.present
        ? `<code style="font-size:11px">${escapeHtml(it.preview || '')}</code>
           <div class="t-desc" style="font-size:11px">${it.length} 字符 · ${escapeHtml(srcText)}</div>`
        : '<span class="t-dim">—</span>'}</td>
      <td><span class="chip ${tone}">${escapeHtml(srcText)}</span></td>
      <td>
        <button class="btn xs" data-sv-edit="${escapeHtml(it.env_name)}">${it.present ? '更换' : '设置'}</button>
        ${it.source === 'db'
          ? `<button class="btn xs ghost" data-sv-del="${escapeHtml(it.env_name)}">删除</button>` : ''}
        <div data-sv-form="${escapeHtml(it.env_name)}" hidden style="margin-top:6px">
          <input class="ctl sm" type="password" autocomplete="new-password"
                 data-sv-input="${escapeHtml(it.env_name)}"
                 placeholder="粘贴密钥（保存后不再显示）" style="max-width:260px">
          <button class="btn xs" data-sv-save="${escapeHtml(it.env_name)}">保存</button>
          <button class="btn xs ghost" data-sv-cancel="${escapeHtml(it.env_name)}">取消</button>
        </div>
      </td>
    </tr>`;
  }).join('');

  return `<div class="card mt-card" id="mc-secret-values">
    <div class="card-head">
      <h2>供应商密钥</h2>
      <span class="sub">密钥密文存库，主密钥只在环境变量</span>
    </div>
    <div class="card-body">
      ${ready ? '' : `<p class="t-bad" style="margin-top:0">
        <b>没有配置 ${escapeHtml(data.kek_env)}，现在不能保存密钥。</b><br>
        没有主密钥就无法加密，平台**不会**退回去存明文，所以这里先禁用。
        生成方法：<code>python3 -c "import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())"</code>
        然后把结果写进环境变量并重启 API。</p>`}
      <p class="hint" style="margin-top:0">
        <b>保存后下一次执行作业生效</b>（已在队列里排队的也按新值），不用重启。
        已在跑的那一单不受影响。<br>
        <b>环境变量优先</b>：同一把密钥若进程环境里也有值，以环境为准，这里存的密文不生效。
      </p>
      <div class="table-wrap"><table class="table">
        <thead><tr><th>变量</th><th>指纹</th><th>当前生效</th><th>操作</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
    </div>
  </div>`;
}

/** 挂载密钥卡：设置 / 保存 / 删除 / 取消，全部 root 委托。 */
export function mountSecretValues(root) {
  const card = root.querySelector('#mc-secret-values');
  if (!card || card.__svBound) return;
  card.__svBound = true;

  const repaint = async () => {
    cache.secretValues = await api.secretValues();
    const host = card.parentElement;
    const idx = [...host.children].indexOf(card);
    card.outerHTML = secretValuesCard();
    const fresh = host.children[idx];
    if (fresh) mountSecretValues(root);
  };

  card.addEventListener('click', async (e) => {
    const edit = e.target.closest('[data-sv-edit]');
    const cancel = e.target.closest('[data-sv-cancel]');
    const save = e.target.closest('[data-sv-save]');
    const del = e.target.closest('[data-sv-del]');

    if (edit || cancel) {
      const name = (edit || cancel).dataset.svEdit || (edit || cancel).dataset.svCancel;
      const form = card.querySelector(`[data-sv-form="${name}"]`);
      if (form) form.hidden = cancel ? true : !form.hidden;
      if (edit) {
        const input = card.querySelector(`[data-sv-input="${name}"]`);
        if (input) input.focus();
      }
      return;
    }

    if (del) {
      const name = del.dataset.svDel;
      if (!(await confirmDanger({
        title: '删除这把密钥？',
        body: `将删除库里保存的 <code>${escapeHtml(name)}</code> 密文。`
              + '删除后如果环境变量里也没有，相应能力就无法调用。',
        okText: '删除',
      }))) return;
      del.disabled = true;
      try {
        cache.secretValues = await api.deleteSecretValue(name) && cache.secretValues;
        await repaint();
        alertNotify(`已删除库里保存的 ${name}。`);
      } catch (err) {
        del.disabled = false;
        alertNotify(`删除失败：${err.message}`);
      }
      return;
    }

    if (save) {
      const name = save.dataset.svSave;
      const input = card.querySelector(`[data-sv-input="${name}"]`);
      const value = input ? input.value.trim() : '';
      if (!value) { alertNotify('密钥不能为空'); return; }
      save.disabled = true;
      try {
        await api.saveSecretValue(name, value);
        // **明文只在这里存在一次**：保存成功立刻清空，连变量也置空，
        // 让这块内存尽早被回收（JS 无法强制擦除字符串，但能缩小窗口）。
        if (input) input.value = '';
        await repaint();
        alertNotify(`已保存 ${name}（密文）。下一单生效。`);
      } catch (err) {
        save.disabled = false;
        alertNotify(`保存失败：${err.message}`);
      }
    }
  });
}

export function runtimePathsCard() {
  const data = cache.runtimePaths || { items: [], note: '' };
  const items = data.items || [];
  if (!items.length) return '';

  const STATE_TONE = { ok: 'green', missing: 'red', unknown: 'gray' };
  const STATE_TEXT = { ok: '可用', missing: '路径不在', unknown: '未配置' };

  const rows = items.map((it) => {
    const locked = it.locked;
    const pending = it.saved_value && it.saved_value !== it.effective;
    const tone = STATE_TONE[it.state] || 'gray';
    const shown = it.effective || '（未配置）';
    return `<tr data-rp="${escapeHtml(it.env_name)}">
      <td><b>${escapeHtml(it.label)}</b>
        <div class="t-dim" style="font-size:11px">${escapeHtml(it.env_name)}</div>
        ${it.hint ? `<div class="t-desc">${escapeHtml(it.hint)}</div>` : ''}</td>
      <td><code style="font-size:11px">${escapeHtml(shown)}</code>
        <div class="t-desc" style="font-size:11px">${
          it.source === 'env' ? '来自环境变量（页面改不动）'
            : it.source === 'web' ? '来自这里保存的值'
              : '跟随内置默认'}</div>
        ${it.detail && it.state !== 'ok'
          ? `<div class="t-dim" style="font-size:11px">${escapeHtml(it.detail)}</div>` : ''}</td>
      <td><span class="chip ${tone}">${STATE_TEXT[it.state] || it.state}</span></td>
      <td>${locked
        ? '<span class="chip gray">环境变量优先</span>'
        : `<input class="ctl sm" data-rp-input="${escapeHtml(it.env_name)}"
                  value="${escapeHtml(it.saved_value || '')}"
                  placeholder="/绝对路径">
           <button class="btn xs" data-rp-save="${escapeHtml(it.env_name)}">保存</button>
           ${it.saved_value ? `<button class="btn xs ghost" data-rp-clear="${escapeHtml(it.env_name)}">清除</button>` : ''}`}
        ${pending ? '<div class="t-desc" style="font-size:11px">已保存；与当前生效值不同，说明环境变量里另有一份（环境优先）</div>' : ''}
      </td>
    </tr>`;
  }).join('');

  return `<div class="card mt-card" id="mc-runtime-paths">
    <div class="card-head">
      <h2>本地运行时路径</h2>
      <span class="sub">worker 去哪里找权重与 CLI</span>
    </div>
    <div class="card-body">
      <p class="hint" style="margin-top:0">
        <b>保存后下一次执行作业生效</b>（已在队列里排队的也按新值），不用重启。
        已在跑的那一单不受影响。<br>
        <b>环境变量优先</b>：被环境占住的那几项标着「环境变量优先」，页面改不动。
      </p>
      <div class="table-wrap"><table class="table">
        <thead><tr><th>参数</th><th>当前生效值</th><th>状态</th><th>在这里改</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      ${data.note ? `<p class="t-desc" style="margin-bottom:0">${escapeHtml(data.note)}</p>` : ''}
    </div>
  </div>`;
}

/** 挂载：保存 / 清除两件事，都用 root 委托（本仓做法，见 345910b 那条守卫）。 */
export function mountRuntimePaths(root) {
  const card = root.querySelector('#mc-runtime-paths');
  if (!card || card.__rpBound) return;
  card.__rpBound = true;
  card.addEventListener('click', async (e) => {
    const saveBtn = e.target.closest('[data-rp-save]');
    const clearBtn = e.target.closest('[data-rp-clear]');
    if (!saveBtn && !clearBtn) return;
    const envName = (saveBtn || clearBtn).dataset.rpSave || (saveBtn || clearBtn).dataset.rpClear;
    const input = card.querySelector(`[data-rp-input="${envName}"]`);
    const path = clearBtn ? '' : (input ? input.value.trim() : '');
    const btn = saveBtn || clearBtn;
    btn.disabled = true;
    try {
      cache.runtimePaths = await api.saveRuntimePath(envName, path);
      // 拿新数据重画整张卡：保存后 note 与 pending 标记都变了，
      // 只改一行会让「worker 需重启」那句来不及出现。
      const host = card.parentElement;
      const idx = [...host.children].indexOf(card);
      card.outerHTML = runtimePathsCard();
      const fresh = host.children[idx];
      if (fresh) mountRuntimePaths(root);
      alertNotify(path ? `已保存 ${envName}，下一单即生效。`
        : `已清除 ${envName} 的页面配置。`);
    } catch (err) {
      btn.disabled = false;
      alertNotify(`保存失败：${err.message}`);
    }
  }, { once: false });
}

/* ================= 子页出口（挂到「系统设置 · 参数管理」） =================
 * 这两张卡原来在引擎管理页，由 `models.js` 的 `sub === 1` 分支挂载；
 * 搬过来之后挂载点归本文件自己，`models.js` 那边不再知道它们存在。
 */
export default {
  async render() {
    // **两块数据由本页自己取**，不依赖模型中心那套 SUB_NEEDS。
    // 搬到这一页之后，"子页 1 才取"的旧逻辑不再成立——留着会渲染出
    // 两张空表（卡片结构在、行数为 0），而 js_load_check 与单测都发现不了
    // （它们只看模块能不能加载）。这正是本仓踩过两次的坑，第三次不再犯。
    //
    // 读不到就保留旧缓存/空态、不挡整页：这两张是配置面，不是这一屏的主内容。
    const [runtimePaths, secretValues] = await Promise.all([
      api.runtimePaths().catch(() => null),
      api.secretValues().catch(() => null),
    ]);
    if (runtimePaths) cache.runtimePaths = runtimePaths;
    if (secretValues) cache.secretValues = secretValues;
    return `${runtimePathsCard()}
      ${secretValuesCard()}`;
  },

  mount(root) {
    mountRuntimePaths(root);
    mountSecretValues(root);
  },
};
