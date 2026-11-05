import { api } from '../api.js';
import { store } from '../store.js';
import { escapeHtml, run } from '../util.js';

/**
 * 系统设置 · 角色管理（v0.111 从「用户与角色」那一页拆出来）。
 *
 * 为什么拆：原来一页里同时摆着「用户」「新建用户」「角色权限」三张卡，
 * 而这两件事的操作者常常不是同一个人、也不是同一种动作——
 * 加人是偶尔一次，调角色权限是"这个岗位该给哪些权限点"的一次长时间判断，
 * 中间要来回对照矩阵。挤在一页时，往下滚到矩阵，上面那张用户表就看不见了，
 * 而"这个角色现在挂在谁身上"恰恰是调权限时最想同时看到的东西。
 *
 * 拆完的分工：用户管理只管人（建、停用、改角色、重置口令、删）；
 * 这一页只管角色（角色概览、权限点矩阵）。两页共用同一份角色表，
 * 但**写入口仍然各自只有一个**：改人的角色在那一页保存，改角色的权限在这里保存。
 */

const stamp = (t) => (t || '').slice(0, 16);

function groupPermissions(perms) {
  const groups = [];
  perms.forEach((p) => {
    let g = groups.find((x) => x.name === p.group_name);
    if (!g) groups.push(g = { name: p.group_name, items: [] });
    g.items.push(p);
  });
  return groups;
}

export default {
  async render() {
    if (!store.can('user:read')) {
      return `<div class="card"><div class="empty">
        <div class="empty-t">没有权限</div><p>查看角色与权限点需要 user:read。</p>
      </div></div>`;
    }

    let users = [];
    let roles = [];
    let perms = [];
    try {
      [users, roles, perms] = await Promise.all([api.users(), api.roles(), api.permissions()]);
    } catch (err) {
      return `<div class="card"><div class="empty">
        <div class="empty-t">读不到角色</div><p>${escapeHtml(err.message)}</p>
      </div></div>`;
    }

    const canWrite = store.can('user:write');
    const members = (code) => users.filter((u) => (u.roles || []).includes(code));

    const overview = roles.map((r) => {
      const who = members(r.code);
      return `<tr>
        <td class="t-strong">${escapeHtml(r.name)}
          <span class="t-dim mono"> ${escapeHtml(r.code)}</span></td>
        <td class="c">${(r.permissions || []).length}</td>
        <td class="c">${who.length
          ? `${who.length} 个` : '<span class="t-dim">没人用</span>'}</td>
        <td class="t-dim">${who.map((u) => escapeHtml(u.display_name || u.username)).slice(0, 6).join('、')
          || '—'}</td>
        <td>${r.code === 'admin'
          ? '<span class="chip blue">内置，不可改</span>'
          : (canWrite ? '<span class="t-dim">可改</span>' : '<span class="t-dim">只读</span>')}</td>
        <td class="r">${canWrite && r.code !== 'admin'
          ? `<button class="btn sm" data-jump-matrix="${r.id}">去改权限</button>` : ''}</td>
      </tr>`;
    }).join('');

    const matrix = groupPermissions(perms).map((g) => `
      <tr class="grp-row"><td colspan="${roles.length + 1}">${escapeHtml(g.name)}</td></tr>
      ${g.items.map((p) => `
        <tr>
          <td>${escapeHtml(p.name)}<span class="t-dim mono"> ${escapeHtml(p.code)}</span></td>
          ${roles.map((r) => `
            <td><input type="checkbox" data-role="${r.id}" data-perm="${escapeHtml(p.code)}"
                       ${r.permissions.includes(p.code) ? 'checked' : ''}
                       ${r.code === 'admin' ? 'disabled' : ''}></td>`).join('')}
        </tr>`).join('')}`).join('');

    return `
      <div class="card">
        <div class="card-head"><h2>角色概览</h2>
          <span class="sub">${roles.length} 个角色 · 权限点数与"几个账号在用"都是现算</span></div>
        <div class="table-wrap">
          <table>
          <thead><tr>
            <th>角色</th><th class="c">权限点</th><th class="c">在用账号</th>
            <th>都是谁</th><th>能不能改</th><th class="r"></th>
          </tr></thead>
          <tbody>${overview}</tbody>
          </table>
        </div>
        <div class="card-body" style="border-top:1px solid var(--line)">
          <p class="note" style="margin:0"><b>内置 admin 那一列是灰的。</b>
            它不是"还没配好"，是服务端规则：admin 的权限点由代码固定，
            改它等于给自己造一个锁死门的角色。</p>
        </div>
      </div>

      <div class="card" style="margin-top:14px" id="perm-matrix-card">
        <div class="card-head"><h2>角色权限矩阵</h2>
          <span class="sub">一列一个角色，勾完点那一列顶部或底部的保存 ·
            ${perms.length} 个权限点</span></div>
        <div class="table-wrap">
          <table class="perm-matrix">
          <thead><tr>
          <th>权限点</th>
          ${roles.map((r) => `<th>
          <span>${escapeHtml(r.name)}</span>
          ${r.code === 'admin' ? '<span class="t-dim">（内置，不可改）</span>'
          : (canWrite ? `<button class="btn sm" data-save-role="${r.id}">保存</button>` : '')}
          </th>`).join('')}
          </tr></thead>
          <tbody>${matrix}</tbody>
          </table>
        </div>
        <div class="card-body" style="border-top:1px solid var(--line)">
          <p class="note" style="margin:0">勾上不等于生效，<b>按那一列的「保存」才写库</b>；
            保存后那列按钮会先变「已保存」再刷新一次。真正的拦截在服务端，
            这一页只是让你看清"这个岗位到底给了哪些权限点"。</p>
        </div>
      </div>

      <div class="card" style="margin-top:14px">
        <div class="card-head"><h2>最近登录</h2>
          <span class="sub">角色改完想看谁受影响，回「用户管理」那一页对最后登录</span></div>
        <div class="table-wrap">
          <table>
          <thead><tr><th>用户名</th><th>显示名</th><th>角色</th><th>最后登录</th></tr></thead>
          <tbody>${users.slice(0, 12).map((u) => `<tr>
            <td class="mono">${escapeHtml(u.username)}</td>
            <td>${escapeHtml(u.display_name)}</td>
            <td>${(u.roles || []).map((c) => `<span class="chip plain">${escapeHtml(c)}</span>`).join(' ') || '—'}</td>
            <td class="t-dim mono">${stamp(u.last_login_at) || '—'}</td>
          </tr>`).join('')}</tbody>
          </table>
        </div>
      </div>`;
  },

  mount(root) {
    const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));

    root.querySelectorAll('[data-save-role]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const roleId = Number(btn.dataset.saveRole);
        const codes = [...root.querySelectorAll(`input[data-role="${roleId}"]`)]
          .filter((el) => el.checked)
          .map((el) => el.dataset.perm);
        btn.disabled = true;
        const ok = await run('保存', () => api.setRolePermissions(roleId, codes));
        btn.disabled = false;
        if (!ok) return;
        btn.textContent = '已保存';
        setTimeout(refresh, 400);
      });
    });

    // 「去改权限」只做一件事：滚到那一列。不新开弹窗、不复制一份状态——
    // 矩阵就是唯一的那份，复制出来就会有两套勾选互相打架。
    root.querySelectorAll('[data-jump-matrix]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const card = root.querySelector('#perm-matrix-card');
        if (!card) return;
        card.scrollIntoView({ block: 'start' });
        window.scrollBy(0, -70);
        const roleId = Number(btn.dataset.jumpMatrix);
        card.querySelector(`input[data-role="${roleId}"]`)?.focus();
      });
    });
  },
};
