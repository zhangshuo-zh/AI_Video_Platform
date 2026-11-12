import { api } from '../api.js';
import { store } from '../store.js';
import { bindModalDismiss, confirmDanger, escapeHtml, run } from '../util.js';
// 页内那一排二级切换器由 system-config.js 统一画与绑：
// 两页共用一个头部，这里再绑一次会让一次点击触发两遍重渲染

/**
 * 系统设置 · 用户管理（v0.111 起「角色权限矩阵」拆去了「角色管理」）。
 * 只管人：新建、停用／启用、改角色、重置口令、删除（含名下项目的处置）。
 * 界面上按权限点收口，真正的拦截在服务端——这里只是别让人白点。
 */

const stamp = (t) => (t || '').slice(0, 16);

// render 里填，mount 里用：弹窗里的角色复选框要跟着同一份角色表
let ROLES = [];
// 删除用户时"转移给谁"的候选，用 render 拿到的列表，不再跑一趟接口
let USERS = [];

function roleChecks(prefix, roles, selected) {
  return roles.map((r) => `
    <label class="chk">
      <input type="checkbox" data-${prefix}="${escapeHtml(r.code)}"
             ${selected.includes(r.code) ? 'checked' : ''}>
      <span>${escapeHtml(r.name)}</span>
    </label>`).join('');
}

export default {
  async render() {
    if (!store.can('user:read')) {
      return `<div class="card"><div class="empty">
        <div class="empty-t">没有权限</div><p>查看用户需要 user:read。</p>
      </div></div>`;
    }

    let users = [];
    let roles = [];
    try {
      [users, roles] = await Promise.all([api.users(), api.roles()]);
    } catch (err) {
      return `<div class="card"><div class="empty">
        <div class="empty-t">读不到用户</div><p>${escapeHtml(err.message)}</p>
      </div></div>`;
    }

    const roleName = (code) => roles.find((r) => r.code === code)?.name || code;
    ROLES = roles;
    USERS = users;
    const canWrite = store.can('user:write');
    const canAssign = store.can('user:assign');
    const meId = store.user?.id;

    // 超级用户那一行不给任何写按钮：服务端也拦（users.py 的 _guard_target），
    // 这里只是别让人点了才被拒
    const userActions = (u) => {
      if (u.is_superuser) return '<span class="t-dim">受保护，不可改</span>';
      const parts = [];
      if (canAssign && u.id !== meId) {
        parts.push(`<button class="btn sm" data-edit-roles="${u.id}">改角色</button>`);
      }
      if (canWrite) {
        parts.push(`<button class="btn sm" data-reset="${u.id}">重置口令</button>`);
      }
      if (canWrite && u.id !== meId) {
        parts.push(`<button class="btn sm ${u.is_active ? 'danger' : ''}" data-toggle="${u.id}"
                           data-active="${u.is_active ? 1 : 0}">${u.is_active ? '停用' : '启用'}</button>`);
        parts.push(`<button class="btn sm danger" data-del-user="${u.id}">删除</button>`);
      }
      return parts.join(' ');
    };

    const userRows = users.map((u) => `
      <tr data-roles="${escapeHtml(JSON.stringify(u.roles))}">
        <td class="t-strong mono">${escapeHtml(u.username)}</td>
        <td>${escapeHtml(u.display_name)}</td>
        <td>${u.roles.map((c) => `<span class="chip plain">${escapeHtml(roleName(c))}</span>`).join(' ')
          || '<span class="t-dim">—</span>'}
          ${u.is_superuser ? '<span class="chip blue">超级用户</span>' : ''}</td>
        <td>${u.is_active ? '正常' : '<b class="t-bad">已停用</b>'}
          ${u.must_change_password ? '<span class="t-dim">待改密</span>' : ''}</td>
        <td class="t-dim mono">${stamp(u.last_login_at) || '—'}</td>
        <td class="r">${userActions(u)}</td>
      </tr>`).join('');

    return `
      <div class="card">
        <div class="card-head"><h2>用户</h2>
          <span class="sub">${users.length} 个 · 只列出不高于你自己层级的账号</span></div>
        <div class="table-wrap">
          <table>
          <thead><tr>
          <th>用户名</th><th>显示名</th><th>角色</th><th>状态</th><th>最后登录</th><th class="r"></th>
          </tr></thead>
          <tbody>${userRows}</tbody>
          </table>
        </div>
      </div>

      ${canWrite ? `
      <div class="card mt-card" >
        <div class="card-head"><h2>新建用户</h2>
          <span class="sub">初始口令由你定，对方第一次登录必须改</span></div>
        <div class="card-body">
          <div class="form-grid">
            <label for="nu-username">用户名</label>
            <input id="nu-username" placeholder="字母数字 _ . -" autocomplete="off">

            <label for="nu-display">显示名</label>
            <input id="nu-display" placeholder="例如 阿张">

            <label for="nu-pass">初始口令</label>
            <input id="nu-pass" type="password" placeholder="至少 6 位"
                   autocomplete="new-password">

            ${canAssign ? `<span class="pf-label">角色</span>
            <div class="role-pick">${roleChecks('new-role', roles, ['viewer'])}</div>` : ''}

            <div class="pf-actions">
              <button class="btn primary" id="nu-create">新建</button>
            </div>
          </div>
        </div>
      </div>` : ''}

      <div class="modal" id="roles-modal" hidden>
        <div class="modal-box">
          <div class="modal-head neutral">改角色</div>
          <div class="modal-body">
            <p class="t-dim" id="rm-who"></p>
            <div class="row" style="gap:14px;flex-wrap:wrap" id="rm-roles"></div>
            <p class="modal-meta">这里改的是"这个人挂哪些角色"；
              角色本身有哪些权限点，去「角色管理」那一页改。</p>
          </div>
          <div class="modal-actions">
            <button class="btn" id="rm-cancel">取消</button>
            <button class="btn primary" id="rm-save">保存</button>
          </div>
        </div>
      </div>

      <div class="modal" id="pw-modal" hidden>
        <div class="modal-box">
          <div class="modal-head neutral">重置口令</div>
          <div class="modal-body">
            <p class="t-dim" id="pw-who"></p>
            <label class="modal-label" for="pw-new">新口令（至少 6 位）</label>
            <input id="pw-new" class="ctl" type="password" autocomplete="new-password">
            <p class="modal-meta">设完对方下次登录会被要求立刻改掉，现有会话也一并作废。</p>
          </div>
          <div class="modal-actions">
            <button class="btn" id="pw-cancel">取消</button>
            <button class="btn primary" id="pw-save">保存</button>
          </div>
        </div>
      </div>
      <div class="modal" id="del-user-modal" hidden>
        <div class="modal-box">
          <div class="modal-head">删除用户</div>
          <div class="modal-body">
            <p class="modal-warn">删掉恢复不了，该账号的会话立即失效。</p>
            <p id="du-who" class="t-dim"></p>
            <label class="modal-label">名下的项目怎么处理</label>
            <label class="chk" style="display:flex;margin-bottom:8px">
              <input type="radio" name="du-mode" value="transfer" checked>
              <span>转移给</span>
            </label>
            <select id="du-target" class="ctl" style="width:100%"></select>
            <label class="chk" style="display:flex;margin-top:12px">
              <input type="radio" name="du-mode" value="delete">
              <span>连同名下项目一起删除</span>
            </label>
            <p class="modal-meta">
              他作为协作人的关系会自动清掉；作为所有人的项目必须先处理，不能留悬空项目。
            </p>
          </div>
          <div class="modal-actions">
            <button class="btn" id="du-cancel">取消</button>
            <button class="btn danger-solid" id="du-ok">永久删除</button>
          </div>
        </div>
      </div>`;
  },

  mount(root) {
    const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));

    /* ---------- 新建用户 ---------- */
    const createBtn = root.querySelector('#nu-create');
    if (createBtn) {
      createBtn.addEventListener('click', async () => {
        const username = root.querySelector('#nu-username').value.trim();
        const display = root.querySelector('#nu-display').value.trim();
        const password = root.querySelector('#nu-pass').value;
        const roleList = root.querySelector('.role-pick');
        const roles = roleList
          ? [...roleList.querySelectorAll('input:checked')].map((el) => el.dataset['new-role'])
          : [];
        if (!username) { root.querySelector('#nu-username').focus(); return; }
        if (password.length < 6) { root.querySelector('#nu-pass').focus(); return; }

                const ok = await run('新建用户', () => api.createUser({
          username, display_name: display, password, roles,
        }), { btn: createBtn });
        if (!ok) return;
        refresh();
      });
      // 新建用户这一栏原来只能点按钮：输入完口令回车没反应，
      // 而它下面每个表单错误都是"聚焦到该填的那一格"，回车提交才成一套
      ['#nu-username', '#nu-display', '#nu-pass'].forEach((sel) => {
        root.querySelector(sel)?.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') { e.preventDefault(); createBtn.click(); }
        });
      });
    }

    /* ---------- 停用 / 启用 ---------- */
    root.querySelectorAll('[data-toggle]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const id = Number(btn.dataset.toggle);
        const next = btn.dataset.active !== '1';
        // 停用不是不可逆（还能启用回来），所以按钮用主色而不是红色，
        // 但后果要说清楚：对方会立刻掉线
        if (!next && !(await confirmDanger({
          title: '停用账号', danger: false, okText: '停用',
          body: '停用后该账号<b>立刻掉线</b>，重新启用前无法登录。',
        }))) return;
        const ok = await run(next ? '启用' : '停用', () => api.updateUser(id, { is_active: next }), { btn: btn });
        if (!ok) return;
        refresh();
      });
    });

    /* ---------- 改角色 ---------- */
    const rolesModal = root.querySelector('#roles-modal');
    const rmRoles = root.querySelector('#rm-roles');
    let editingUser = null;
    const closeRoles = () => { rolesModal.hidden = true; editingUser = null; };

    root.querySelectorAll('[data-edit-roles]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const row = btn.closest('tr');
        editingUser = Number(btn.dataset.editRoles);
        root.querySelector('#rm-who').textContent =
          `${row.children[0].textContent.trim()}（${row.children[1].textContent.trim()}）`;
        // 当前角色直接取行上的 data-roles，不再跑一趟接口
        const selected = JSON.parse(row.dataset.roles || '[]');
        rmRoles.innerHTML = roleChecks('edit-role', ROLES, selected);
        rolesModal.hidden = false;
      });
    });

    root.querySelector('#rm-save').addEventListener('click', async () => {
      if (editingUser === null) return;
      const roles = [...rmRoles.querySelectorAll('input:checked')].map((el) => el.dataset['edit-role']);
      const saveBtn = root.querySelector('#rm-save');
      const ok = await run('保存', () => api.setUserRoles(editingUser, roles), { btn: saveBtn });
      if (!ok) return;
      closeRoles();
      refresh();
    });
    root.querySelector('#rm-cancel').addEventListener('click', closeRoles);
    rolesModal.addEventListener('click', (e) => { if (e.target === rolesModal) closeRoles(); });
    bindModalDismiss(rolesModal, closeRoles);

    /* ---------- 重置口令 ---------- */
    const pwModal = root.querySelector('#pw-modal');
    let pwUser = null;
    const closePw = () => { pwModal.hidden = true; pwUser = null; };

    root.querySelectorAll('[data-reset]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const row = btn.closest('tr');
        pwUser = Number(btn.dataset.reset);
        root.querySelector('#pw-who').textContent = row.children[0].textContent.trim();
        root.querySelector('#pw-new').value = '';
        pwModal.hidden = false;
        root.querySelector('#pw-new').focus();
      });
    });

    root.querySelector('#pw-save').addEventListener('click', async () => {
      if (pwUser === null) return;
      const value = root.querySelector('#pw-new').value;
      if (value.length < 6) { root.querySelector('#pw-new').focus(); return; }
      const saveBtn = root.querySelector('#pw-save');
      const ok = await run('重置口令', () => api.resetUserPassword(pwUser, value), { btn: saveBtn });
      if (!ok) return;
      closePw();
      refresh();
    });
    root.querySelector('#pw-cancel').addEventListener('click', closePw);
    // 重置口令只有一个输入框，回车不提交就得手点一下，键盘用户白跑一趟
    root.querySelector('#pw-new').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); root.querySelector('#pw-save').click(); }
    });
    pwModal.addEventListener('click', (e) => { if (e.target === pwModal) closePw(); });
    bindModalDismiss(pwModal, closePw);

    /* ---------- 删除用户 ----------
       名下有项目就必须表态：转移给谁，或者连同项目一起删。
       两个都不给的话服务端直接 422，这里也把"转移"在没候选时禁掉。 */
    const delModal = root.querySelector('#del-user-modal');
    const duTarget = root.querySelector('#du-target');
    const duOk = root.querySelector('#du-ok');
    const duTransfer = root.querySelector('input[name="du-mode"][value="transfer"]');
    const duDelete = root.querySelector('input[name="du-mode"][value="delete"]');
    let delUser = null;

    const closeDel = () => { delModal.hidden = true; delUser = null; duOk.disabled = false; };

    root.querySelectorAll('[data-del-user]').forEach((btn) => {
      btn.addEventListener('click', () => {
        delUser = Number(btn.dataset.delUser);
        const row = btn.closest('tr');
        root.querySelector('#du-who').textContent =
          `账号：${row.children[0].textContent.trim()}`;
        // meId 是 render 里的局部变量，mount 这层拿不到，直接用 store
        const others = USERS.filter((u) => u.id !== delUser
          && u.id !== store.user?.id && u.is_active);
        duTarget.innerHTML = others.length
          ? others.map((u) => `<option value="${u.id}">${escapeHtml(u.display_name)}（${
              escapeHtml(u.username)}）</option>`).join('')
          : '<option value="">没有可移交的人</option>';
        duTarget.disabled = !others.length;
        duTransfer.disabled = !others.length;
        if (!others.length) duDelete.checked = true;
        else duTransfer.checked = true;
        delModal.hidden = false;
      });
    });

    duOk.addEventListener('click', async () => {
      if (delUser === null) return;
      const mode = delModal.querySelector('input[name="du-mode"]:checked').value;
      const ok = await run('删除', () => api.deleteUser(delUser, mode === 'delete'
        ? { delete_projects: true }
        : { transfer_to: Number(duTarget.value) }), { btn: duOk });
      if (!ok) return;
      closeDel();
      refresh();
    });

    root.querySelector('#du-cancel').addEventListener('click', closeDel);
    delModal.addEventListener('click', (e) => { if (e.target === delModal) closeDel(); });
    bindModalDismiss(delModal, closeDel);
  },
};
