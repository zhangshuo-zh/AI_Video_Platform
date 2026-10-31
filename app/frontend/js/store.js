/**
 * 前端唯一的状态源：项目列表、类型字典、当前选中的项目。
 * 视图只读 store，改状态一律走这里再派发 app:refresh。
 */
import { api } from './api.js';

export const store = {
  projects: [],
  currentId: null,
  // 登录态：user 是后端 /api/auth/me 给的那一份，permissions 是权限点数组。
  // 界面按 can() 决定显不显示某个操作；真正的拦截在服务端，前端这层只是别让人白点。
  user: null,
  permissions: [],
  roles: [],

  /** 拉当前用户。抛出的 error 带 status：401 表示没登录 */
  async loadMe() {
    const me = await api.me();
    this.setSession(me);
    return me;
  },

  setSession(me) {
    this.user = me.user;
    this.permissions = me.permissions;
    this.roles = me.roles || [];
  },

  clearSession() {
    this.user = null;
    this.permissions = [];
    this.roles = [];
  },

  can(code) {
    return this.permissions.includes(code);
  },

  /** 管理员以上对全部项目放行；这条要和后端 access.is_manager 保持一致 */
  get isManager() {
    return !!this.user?.is_superuser || this.roles.includes('admin');
  },

  /** 顶栏那个"当前项目"下拉只列在产的：归档的意思就是别再挡在眼前。
      项目中心那页要列已归档才能找回，它直接用 store.projects 自己筛。 */
  get activeProjects() {
    return this.projects.filter((p) => p.status !== 'archived');
  },

  /** 拉一次列表和类型字典；首次加载/新建项目后调用 */
  async load() {
    this.projects = await api.projects();
    // 当前项目被删或还没选过时，退到第一个
    if (!this.projects.some((p) => p.id === this.currentId)) {
      this.currentId = this.projects.length ? this.projects[0].id : null;
    }
    return this;
  },

  get current() {
    return this.projects.find((p) => p.id === this.currentId) || null;
  },

  select(id) {
    this.currentId = id;
    // 上报"打开过"只为让列表下次按最近使用排序。不 await、失败也不吭声：
    // 切换项目这件事不该被一次写时间的请求卡住
    if (id) api.openProject(id).catch(() => {});
    window.dispatchEvent(new CustomEvent('app:refresh'));
  },
};
