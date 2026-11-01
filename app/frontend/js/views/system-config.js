import { api } from '../api.js';
import { store } from '../store.js';
import { bindSubChips, escapeHtml, run, subChipRow } from '../util.js';
import { getSub, setSub } from '../navstate.js';
import usersPanel from './system-users.js';

/**
 * 系统设置：两个二级页。
 *   0 模型与密钥 —— 五类能力 × 本地/云端
 *   1 用户与角色 —— 另一份文件（system-users.js），这里只做转发
 * 只有 provider 与 model 可编辑；密钥只显示"配没配"，值永远不出后端。
 *
 * 两个子页的抬头都摆一排页内切换器：窄屏侧栏二级标签会收起，
 * 而且只有 user:read 的人进来第一屏是"没有权限"——那时切换器就是他唯一的路。
 */
const SUBS = ['模型与密钥', '用户与角色'];
export default {
  async render() {
    if ((getSub('system-config') ?? 0) === 1) return usersPanel.render();
    // 侧栏入口已经按权限隐藏了，但地址可以直接粘进来，所以这里再挡一道
    if (!store.can('system:config')) {
      return `<section class="view on">
        <div class="page-head"><h1>系统设置</h1><p>模型与密钥</p>
          <div class="right">${subChipRow(SUBS, 0)}</div></div>
        <div class="card"><div class="empty">
        <div class="empty-t">没有权限</div>
        <p>系统设置只有管理员能打开；要改模型配置，找管理员。
           右边切到「用户与角色」还能看用户名单（你有 user:read）。</p>
      </div></div></section>`;
    }

    let rows = [];
    let error = '';
    try {
      rows = await api.providers();
    } catch (err) {
      error = err.message;
    }

    if (error) {
      return `<section class="view on"><div class="card"><div class="empty">
        <div class="empty-t">读不到配置</div><p>${escapeHtml(error)}</p></div></div></section>`;
    }

    // 占位提示只是举例，不代表平台已经适配这些厂商
    const HINT = {
      local: { text: 'ollama', image: 'mflux', video: 'FastMetal / mlx-video', tts: 'VoxCPM2', music: 'ACE-Step' },
      cloud: { text: 'volc / minimax', image: 'jimeng / wanx', video: 'wan / kling / seedance', tts: 'volc / minimax', music: '厂商代号' },
    };
    const body = rows.map((r) => `
      <tr data-id="${r.id}" data-cap="${r.capability}">
        <td class="t-strong">${r.capability_label}</td>
        <td><span class="chip ${r.backend === 'local' ? 'plain' : 'blue'}">${r.backend_label}</span></td>
        <td><input class="ctl sm" data-f="provider" value="${escapeHtml(r.provider)}"
                   placeholder="${HINT[r.backend][r.capability] || ''}"></td>
        <td><input class="ctl sm" data-f="model" value="${escapeHtml(r.model)}" placeholder="模型 ID"></td>
        <td>${r.has_key
          ? '<span class="chip green">已配置</span>'
          : '<span class="chip gray">未配置</span>'}</td>
        <td class="c"><input type="checkbox" data-f="enabled" ${r.enabled ? 'checked' : ''}></td>
        <td class="c"><input type="radio" name="def-${r.capability}" data-f="default" ${r.is_default ? 'checked' : ''}></td>
      </tr>`).join('');

    return `
    <section class="view on">
      <div class="page-head">
        <h1>系统设置</h1>
        <p>每类能力两条轨，提交作业时默认走打勾的那条，也可以临时改</p>
        <div class="right">
          ${subChipRow(SUBS, 0)}
          <span class="chip plain" id="save-state" hidden>已保存</span>
          <button class="btn primary" id="save-providers">保存</button>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>模型配置</h2>
          <span class="sub">密钥不进数据库，只读环境变量 FV_KEY_&lt;PROVIDER&gt;</span>
        </div>
        <div class="table-wrap">
          <table>
          <thead><tr>
          <th>能力</th><th>后端</th><th>Provider</th><th>模型</th><th>密钥</th>
          <th class="c">启用</th><th class="c">默认</th>
          </tr></thead>
          <tbody>${body}</tbody>
          </table>
        </div>
        <div class="card-body" style="border-top:1px solid var(--line)">
          <p class="note">
            <b>怎么配</b>：把云厂商给的 key 写进启动脚本或 <code>.env</code>，变量名按 provider 拼——
            provider 填 <code>volc</code> 就是 <code>FV_KEY_VOLC</code>，填 <code>minimax</code> 就是
            <code>FV_KEY_MINIMAX</code>。改完环境变量要重启服务才会刷新"已配置"这一列。
          </p>
        </div>
      </div>
    </section>`;
  },

  mount(root) {
    // 切换器要在任何提前 return 之前绑上：无权限那一屏最需要它
    bindSubChips(root, (i) => { setSub('system-config', i); window.dispatchEvent(new CustomEvent('app:refresh')); });
    if ((getSub('system-config') ?? 0) === 1) { usersPanel.mount?.(root); return; }
    const saveBtn = root.querySelector('#save-providers');
    const stateChip = root.querySelector('#save-state');

    saveBtn.addEventListener('click', async () => {
      const rows = [...root.querySelectorAll('tbody tr')].map((tr) => ({
        id: Number(tr.dataset.id),
        provider: tr.querySelector('[data-f="provider"]').value,
        model: tr.querySelector('[data-f="model"]').value,
        enabled: tr.querySelector('[data-f="enabled"]').checked,
        is_default: tr.querySelector('[data-f="default"]').checked,
      }));

      saveBtn.disabled = true;
      // 后端会明确说哪类能力少了或多了默认，run 会原样透出来
      const ok = await run('保存', () => api.saveProviders(rows));
      saveBtn.disabled = false;
      if (ok) {
        stateChip.hidden = false;
        setTimeout(() => { stateChip.hidden = true; }, 2000);
      }
    });
  },
};
