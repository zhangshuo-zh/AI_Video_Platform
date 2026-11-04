import usersPanel from './system-users.js';

/**
 * 系统设置（v0.99 起只剩一件事：用户与角色）。
 *
 * 原来这里有两个二级页：「模型与密钥」和「用户与角色」。
 * 前者整页搬去了「模型中心 · 引擎管理」——十行能力轨、允许参数、密钥变量名、
 * 健康、验证列、环境检查、验证记录弹窗、整批保存，一行功能都没减，
 * 只是入口收成一个：模型这件事不该同时有两个东家（模块单一所有权）。
 *
 * 所以这个文件现在只做转发。留着它、而不是把 system-users.js 直接注册成
 * 'system-config'，是为了让旧地址 `#system-config` 与 `#system-config/0`
 * 仍然打得开——菜单调整不该让任何人手里的书签失效。
 * 旧深链 `#system-config/1` 会被 router 的下标钳位（router.js:39）送回第 0 页。
 */
export default {
  subIndex: 0,

  render() {
    return usersPanel.render();
  },

  mount(root) {
    usersPanel.mount?.(root);
  },
};
