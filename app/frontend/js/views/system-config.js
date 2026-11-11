import usersPanel from './system-users.js';
import rolesPanel from './system-roles.js';
import runtimePanel from './system-runtime.js';
import { getSub, setSub } from '../navstate.js';
import { bindSubChips, subChipRow } from '../util.js';

/**
 * 系统设置（v0.111 起两个二级页：用户管理／角色管理）。
 *
 * 历史：这里原本有「模型与密钥」和「用户与角色」两页。前者整页搬去了
 * 「模型中心 · 引擎管理」——一行功能都没减，只是入口收成一个（模块单一所有权）。
 * 后者这一版再拆一次：加人与调角色权限是两种动作、常常不是同一个时刻，
 * 挤在一页时往下滚就看不见上面，而"这个角色挂在谁身上"正是调权限时要对照的东西。
 *
 * 留着这个文件而不是把两个面板直接注册进 views/index.js，是为了让旧地址
 * `#system-config` 与 `#system-config/0` 仍然打得开——菜单调整不该让书签失效。
 * 旧深链 `#system-config/1` 现在有了真内容（角色管理），不再被钳回第 0 页。
 */

const SUBS = ['用户管理', '角色管理', '参数管理'];

export default {
  async render() {
    const sub = getSub('system-config') ?? 0;
    const panel = sub === 2 ? runtimePanel : sub === 1 ? rolesPanel : usersPanel;
    const body = await panel.render();
    return `<section class="view on">
      <div class="page-head">
        <h1>系统设置</h1>
        <p>${SUBS[sub]} · 平台级配置；密钥只显示指纹，值永远不出后端</p>
        <div class="right">${subChipRow(SUBS, sub)}</div>
      </div>
      ${body}
    </section>`;
  },

  mount(root) {
    bindSubChips(root, (i) => {
      setSub('system-config', i);
      window.dispatchEvent(new CustomEvent('app:refresh'));
    });
    const sub = getSub('system-config') ?? 0;
    // 三页各自挂：写成 `sub === 1 ? roles : users` 时第三页**永远不挂载**
    // ——DOM 画出来了、按钮点下去没反应，而 render 那条路完全正常，
    // 于是"页面渲染成功"这一点会骗过所有不看点击的检查。
    const panel = sub === 2 ? runtimePanel : sub === 1 ? rolesPanel : usersPanel;
    panel.mount?.(root);
  },
};
