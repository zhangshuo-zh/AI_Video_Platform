/**
 * 前端唯一的状态源：项目列表、类型字典、当前选中的项目。
 * 视图只读 store，改状态一律走这里再派发 app:refresh。
 */
import { api } from './api.js';

export const store = {
  projects: [],
  currentId: null,
  // 0037：当前集。真相在 `user_context` 那一行，这里只是它的缓存——
  // 各页再各自记一个"我选中的集"，就会出现顶栏第 3 集、工作台第 1 集。
  episodes: [],
  currentEpisodeId: null,
  // 登录态：user 是后端 /api/auth/me 给的那一份，permissions 是权限点数组。
  // 界面按 can() 决定显不显示某个操作；真正的拦截在服务端，前端这层只是别让人白点。
  user: null,
  permissions: [],
  roles: [],
  // worker 在线状态与队列长度：顶栏、侧栏脚、首页共用这一份，别各拉各的
  worker: null,

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

  /** 拉 worker 状态。读不到就当离线——状态条不该因为一次失败把主界面弄坏 */
  async loadWorker() {
    try {
      this.worker = await api.workerStatus();
    } catch {
      this.worker = null;
    }
    return this.worker;
  },

  /** 拉一次列表和类型字典；首次加载/新建项目后调用 */
  async load() {
    this.projects = await api.projects();
    // 当前项目被删、还没选过、或**刚被归档**时重新挑一个：
    // 列表按"最后打开"排，projects[0] 完全可能是归档项目；顶栏菜单又只列在产，
    // 当前项目留着归档的话，会出现"面包屑写着 A、下拉里找不到 A"的两套事实。
    // 只剩归档的就宁可不选（顶栏照旧提示"未选择项目"）
    const cur = this.projects.find((p) => p.id === this.currentId);
    if (!cur || cur.status === 'archived') {
      const first = this.activeProjects[0] || null;
      this.currentId = first ? first.id : null;
    }
    return this;
  },

  get current() {
    return this.projects.find((p) => p.id === this.currentId) || null;
  },

  get currentEpisode() {
    return this.episodes.find((e) => e.id === this.currentEpisodeId) || null;
  },

  /** 读服务端上下文。项目换了就顺带把这一集的清单拉一次——顶栏那个下拉要用它。 */
  async loadContext() {
    try {
      const ctx = await api.context();
      // 上下文里的项目看不见时，后端已经把它抹成 null；这里跟着清本地缓存，
      // 别出现"服务端说没项目、顶栏还挂着旧项目名"
      if (ctx.project_id && ctx.project_id !== this.currentId
          && this.projects.some((p) => p.id === ctx.project_id)) {
        this.currentId = ctx.project_id;
      }
      if (!ctx.project_id) this.currentEpisodeId = null;
      else await this.loadEpisodes(ctx.project_id);
      this.currentEpisodeId = this.episodes.some((e) => e.id === ctx.episode_id)
        ? ctx.episode_id : null;
    } catch {
      // 读不到上下文不影响能用：退回"没选集"，各页照旧按自己那套读数据
      this.currentEpisodeId = null;
    }
    return this;
  },

  async loadEpisodes(pid) {
    if (!pid) { this.episodes = []; this.currentEpisodeId = null; return []; }
    try {
      this.episodes = await api.episodes(pid);
    } catch {
      this.episodes = [];
    }
    return this.episodes;
  },

  /** 换集：先本地重绘，再上报服务端。失败要回滚，否则顶栏说的和服务端存的不是一回事。 */
  selectEpisode(id) {
    const before = this.currentEpisodeId;
    this.currentEpisodeId = id;
    window.dispatchEvent(new CustomEvent('app:refresh'));
    api.setContext(this.currentId, id)
      .catch(() => {
        this.currentEpisodeId = before;
        window.dispatchEvent(new CustomEvent('app:refresh'));
        window.dispatchEvent(new CustomEvent('app:toast', {
          detail: { text: '这一集没记住：上下文写回失败，可能是项目已不可见', kind: 'err' } }));
      });
  },

  select(id) {
    this.currentId = id;
    // 换项目＝换上下文：当前集必须清空，否则会拿旧项目的集去算新项目的进度
    this.episodes = [];
    this.currentEpisodeId = null;
    // 先立刻重绘：切项目这件事不该等网络
    window.dispatchEvent(new CustomEvent('app:refresh'));
    if (!id) return;
    api.setContext(id, null).catch(() => {});
    this.loadEpisodes(id).then(() => window.dispatchEvent(new CustomEvent('app:refresh')));
    // 上报"打开过"只为让列表下次按最近使用排序，失败也不吭声。
    // 但 v0.62 起"最后打开"成了界面上看得见的一列：只写库不回读，
    // 人点完一行看到的还是破折号，等于界面在说谎 ⇒ 成功后静默回读一次再重绘。
    // 项目几十个、2—5 个人用，多一个 GET 换界面自洽，值。
    api.openProject(id).then(() => this.load())
      .then(() => window.dispatchEvent(new CustomEvent('app:refresh')))
      .catch(() => {});
  },
};
