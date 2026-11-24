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
        : '来自页面保存';
    return `<tr data-sv="${escapeHtml(it.env_name)}">
      <td><b>${escapeHtml(it.env_name)}</b></td>
      <td>${it.present
        ? `<code class="u-fs-11">${escapeHtml(it.preview || '')}</code>
           <div class="t-desc u-fs-11">${it.length} 字符</div>`
        : '<span class="t-dim">—</span>'}</td>
      <td><span class="chip ${tone}">${escapeHtml(srcText)}</span></td>
      <td><div class="sv-acts-cell">
        <button class="btn xs" data-sv-edit="${escapeHtml(it.env_name)}">${it.present ? '更换' : '设置'}</button>
        ${it.source === 'db'
          ? `<button class="btn xs ghost" data-sv-del="${escapeHtml(it.env_name)}">删除</button>` : ''}
      </div></td>
    </tr>`;
  }).join('');

  return `<div class="card mt-card" id="mc-secret-values">
    <div class="card-head">
      <h2>供应商密钥</h2>
      <span class="sub">密钥密文存库，主密钥只在环境变量</span>
    </div>
    <div class="card-body">
      ${ready ? '' : `<p class="t-bad u-mt-0">
        <b>没有配置 ${escapeHtml(data.kek_env)}，现在不能保存密钥。</b><br>
        没有主密钥就无法加密，平台**不会**退回去存明文，所以这里先禁用。
        生成方法：<code>python3 -c "import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())"</code>
        然后把结果写进环境变量并重启 API。</p>`}
      <p class="hint u-mt-0">
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

  const openSecretModal = (envName) => {
    const data = cache.secretValues || { items: [], kek_ready: false, kek_env: '' };
    const item = (data.items || []).find((x) => x.env_name === envName);
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dlg = document.createElement('dialog');
    dlg.className = 'asset-reference-dialog sv-dialog';
    const envTip = item?.source === 'env'
      ? `<p class="hint">当前正使用进程环境变量中的 <code>${escapeHtml(envName)}</code>（环境优先）；此处保存的密文会留作库内备用。</p>`
      : '<p class="hint">保存后以下一次执行作业生效（密文采用 AES-256-GCM 落库，页面永不回显明文）。</p>';
    const kekTip = data.kek_ready ? ''
      : `<p class="t-bad">未配置环境变量 <code>${escapeHtml(data.kek_env || 'PLATFORM_SECRET_KEK')}</code>，当前无法加密保存。</p>`;
    dlg.innerHTML = `<form class="stack" method="dialog">
      <h2>${item?.present ? '更换' : '设置'}供应商密钥 · <code>${escapeHtml(envName)}</code></h2>
      ${kekTip}${envTip}
      <input class="ctl sv-modal-input" type="password" autocomplete="new-password"
             data-sv-input="${escapeHtml(envName)}"
             placeholder="粘贴新密钥（保存后立即清空，不再显示）"${data.kek_ready ? '' : ' disabled'}>
      <div class="row sv-dialog-acts">
        <button class="btn" type="button" data-act="cancel">取消</button>
        <button class="btn primary" type="submit" data-act="save"${data.kek_ready ? '' : ' disabled'}>保存密文</button>
      </div>
    </form>`;
    const input = dlg.querySelector('[data-sv-input]');
    const saveBtn = dlg.querySelector('[data-act="save"]');
    const close = () => {
      if (input) input.value = '';
      dlg.close();
      dlg.remove();
      if (opener?.isConnected) opener.focus({ preventScroll: true });
    };
    dlg.addEventListener('click', (ev) => { if (ev.target === dlg) close(); });
    dlg.addEventListener('cancel', (ev) => { ev.preventDefault(); close(); });
    dlg.querySelector('[data-act="cancel"]').addEventListener('click', close);
    dlg.querySelector('form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const value = input ? input.value.trim() : '';
      if (!value) {
        alertNotify('密钥不能为空', 'err');
        input?.focus();
        return;
      }
      saveBtn.disabled = true;
      try {
        await api.saveSecretValue(envName, value);
        // **明文只在这里存在一次**：保存成功立刻清空并关闭销毁。
        if (input) input.value = '';
        close();
        await repaint();
        alertNotify(`已保存 ${envName}（密文）。下一单生效。`);
      } catch (err) {
        saveBtn.disabled = false;
        alertNotify(`保存失败：${err.message}`, 'err');
      }
    });
    document.body.append(dlg);
    dlg.showModal();
    if (data.kek_ready) input?.focus();
  };

  card.addEventListener('click', async (e) => {
    const edit = e.target.closest('[data-sv-edit]');
    const del = e.target.closest('[data-sv-del]');

    if (edit) {
      openSecretModal(edit.dataset.svEdit);
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
        alertNotify(`删除失败：${err.message}`, 'err');
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
        <div class="t-dim u-fs-11">${escapeHtml(it.env_name)}</div>
        ${it.hint ? `<div class="t-desc">${escapeHtml(it.hint)}</div>` : ''}</td>
      <td><code class="u-fs-11">${escapeHtml(shown)}</code>
        <div class="t-desc u-fs-11">${
          it.source === 'env' ? '来自环境变量（页面改不动）'
            : it.source === 'web' ? '来自这里保存的值'
              : '跟随内置默认'}</div>
        ${it.detail && it.state !== 'ok'
          ? `<div class="t-dim u-fs-11">${escapeHtml(it.detail)}</div>` : ''}</td>
      <td><span class="chip ${tone}">${STATE_TEXT[it.state] || it.state}</span></td>
      <td>${locked
        ? '<span class="chip gray">环境变量优先</span>'
        : `<input class="ctl sm" data-rp-input="${escapeHtml(it.env_name)}"
                  value="${escapeHtml(it.saved_value || '')}"
                  placeholder="/绝对路径">
           <button class="btn xs" data-rp-save="${escapeHtml(it.env_name)}">保存</button>
           ${it.saved_value ? `<button class="btn xs ghost" data-rp-clear="${escapeHtml(it.env_name)}">清除</button>` : ''}`}
        ${pending ? '<div class="t-desc u-fs-11">已保存；与当前生效值不同，说明环境变量里另有一份（环境优先）</div>' : ''}
      </td>
    </tr>`;
  }).join('');

  return `<div class="card mt-card" id="mc-runtime-paths">
    <div class="card-head">
      <h2>本地运行时路径</h2>
      <span class="sub">worker 去哪里找权重与 CLI</span>
    </div>
    <div class="card-body">
      <p class="hint u-mt-0">
        <b>保存后下一次执行作业生效</b>（已在队列里排队的也按新值），不用重启。
        已在跑的那一单不受影响。<br>
        <b>环境变量优先</b>：被环境占住的那几项标着「环境变量优先」，页面改不动。
      </p>
      <div class="table-wrap"><table class="table">
        <thead><tr><th>参数</th><th>当前生效值</th><th>状态</th><th>在这里改</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      ${data.note ? `<p class="t-desc u-mb-0">${escapeHtml(data.note)}</p>` : ''}
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
      alertNotify(`保存失败：${err.message}`, 'err');
    }
  }, { once: false });
}

/* ================= 历史废片安全清理器（M4 GC） ================= */
function fmtBytes(bytes) {
  const n = Number(bytes || 0);
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(2)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
}

export function gcCleanerCard() {
  const data = cache.gcPreview || {
    candidate_count: 0,
    reclaimable_bytes: 0,
    protected_count: 0,
    items: [],
  };
  const items = data.items || [];
  const rows = items.map((it) => `<tr>
    <td><input type="checkbox" data-gc-job="${it.job_id}" checked></td>
    <td class="mono">#${it.job_id} <span class="t-dim">P${it.project_id}</span></td>
    <td>${escapeHtml(it.capability)} <span class="chip gray">${escapeHtml(it.status)}</span></td>
    <td class="mono">${escapeHtml(fmtBytes(it.bytes))}</td>
    <td><span class="t-desc mono">${escapeHtml((it.files || []).join('、'))}</span></td>
    <td><span class="t-desc">${escapeHtml(it.reason)}</span></td>
  </tr>`).join('');

  return `<div class="card mt-card" id="mc-gc-cleaner">
    <div class="card-head">
      <h2>历史废片安全清理器（GC）</h2>
      <span class="sub">仅清理未钉住、非最新成功且未签字的历史 outputs/ 文件；保留 run.log 与数据库记录</span>
      <div class="right actions">
        <button class="btn sm" type="button" id="gc-scan-btn">扫描可回收废片</button>
        <button class="btn sm danger" type="button" id="gc-exec-btn" ${items.length ? '' : 'disabled'}>
          清理选中废片
        </button>
      </div>
    </div>
    <div class="card-body">
      <div class="gc-stats">
        <div class="gc-stat-card">
          <div class="gc-stat-val">${data.candidate_count || 0}</div>
          <div class="gc-stat-lbl">可安全清理作业</div>
        </div>
        <div class="gc-stat-card">
          <div class="gc-stat-val">${escapeHtml(fmtBytes(data.reclaimable_bytes || 0))}</div>
          <div class="gc-stat-lbl">预计释放磁盘空间</div>
        </div>
        <div class="gc-stat-card">
          <div class="gc-stat-val">${data.protected_count || 0}</div>
          <div class="gc-stat-lbl">受保护作业（绝不清理）</div>
        </div>
      </div>
      ${items.length ? `<div class="table-wrap"><table class="table">
        <thead><tr><th>选</th><th>作业</th><th>能力 / 状态</th><th>可释放</th><th>目标文件</th><th>原因</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>` : '<p class="hint u-mb-0">当前没有可安全回收的历史废片；正在运行、最新成功、已签字及分镜/成片/音频引用的作业均受严格保护。</p>'}
    </div>
  </div>`;
}

export function mountGcCleaner(root) {
  const card = root.querySelector('#mc-gc-cleaner');
  if (!card || card.__gcBound) return;
  card.__gcBound = true;

  const rerenderCard = () => {
    const host = card.parentElement;
    const idx = host ? [...host.children].indexOf(card) : -1;
    card.outerHTML = gcCleanerCard();
    if (host && idx >= 0 && host.children[idx]) mountGcCleaner(root);
  };

  card.addEventListener('click', async (e) => {
    if (e.target.closest('#gc-scan-btn')) {
      const btn = card.querySelector('#gc-scan-btn');
      if (btn) btn.disabled = true;
      try {
        cache.gcPreview = await api.systemGcPreview();
        rerenderCard();
        alertNotify(`扫描完成：发现 ${cache.gcPreview.candidate_count} 条可清理废片，可释放 ${fmtBytes(cache.gcPreview.reclaimable_bytes)}。`);
      } catch (err) {
        if (btn) btn.disabled = false;
        alertNotify(`扫描失败：${err.message}`, 'err');
      }
      return;
    }

    if (e.target.closest('#gc-exec-btn')) {
      const ids = [...card.querySelectorAll('[data-gc-job]:checked')]
        .map((el) => Number(el.dataset.gcJob))
        .filter((n) => Number.isInteger(n) && n > 0);
      if (!ids.length) {
        alertNotify('请至少勾选一条待清理作业。', 'err');
        return;
      }
      if (!(await confirmDanger({
        title: '确认安全清理历史废片',
        body: `仅删除所选 ${ids.length} 条未受保护历史作业的 outputs/ 产物文件（run.log 与数据库台账保留），操作不可撤销。`,
        okText: '确认清理',
      }))) return;

      const btn = card.querySelector('#gc-exec-btn');
      if (btn) btn.disabled = true;
      try {
        const res = await api.systemGcExecute({ job_ids: ids });
        cache.gcPreview = await api.systemGcPreview().catch(() => null);
        rerenderCard();
        alertNotify(`清理完成：处理 ${res.deleted_jobs} 条作业，删除 ${res.deleted_files} 个文件，释放 ${fmtBytes(res.reclaimed_bytes)}。`);
      } catch (err) {
        if (btn) btn.disabled = false;
        alertNotify(`清理失败：${err.message}`, 'err');
      }
    }
  });
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
    const [runtimePaths, secretValues, gcPreview] = await Promise.all([
      api.runtimePaths().catch(() => null),
      api.secretValues().catch(() => null),
      api.systemGcPreview().catch(() => null),
    ]);
    if (runtimePaths) cache.runtimePaths = runtimePaths;
    if (secretValues) cache.secretValues = secretValues;
    if (gcPreview) cache.gcPreview = gcPreview;
    return `${runtimePathsCard()}
      ${secretValuesCard()}
      ${gcCleanerCard()}`;
  },

  mount(root) {
    mountRuntimePaths(root);
    mountSecretValues(root);
    mountGcCleaner(root);
  },
};
