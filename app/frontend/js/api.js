/** 后端接口薄封装：前端只认这些函数，不直接拼 URL。 */

async function toJson(res) {
  const text = await res.text();
  if (!res.ok) {
    // 后端的 detail 是给人看的一句话，优先用它；解析不了才退回原始文本。
    // 状态码挂在 error 上：登录闸门要靠 401 区分"没登录"和"登录失败"。
    let detail = text;
    try { detail = JSON.parse(text).detail ?? text; } catch { /* 不是 JSON 就原样 */ }
    const err = new Error(detail);
    err.status = res.status;
    // 401 的统一出口：会话过期（默认 12 小时）时人往往正在填一个表单，
    // 原来只有启动那一次会认 401，之后每个操作都只弹一句"失败：未登录"，
    // 既没地方重登、也解释不了为什么突然干不了活。
    // 登录接口自己的 401 是"口令错了"，不能拿它去挂过期闸门。
    if (res.status === 401 && !/\/api\/auth\/(login|password)$/.test(res.url || '')) {
      window.dispatchEvent(new CustomEvent('app:unauthorized'));
    }
    throw err;
  }
  // 204 没有响应体，直接 JSON.parse 会炸
  return text ? JSON.parse(text) : null;
}

export const api = {
  me: () => fetch('/api/auth/me').then(toJson),
  login: (username, password) => fetch('/api/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  }).then(toJson),
  logout: () => fetch('/api/auth/logout', { method: 'POST' }).then(toJson),
  changePassword: (oldPassword, newPassword) => fetch('/api/auth/password', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  }).then(toJson),
  // status：'active' 在产 / 'archived' 已归档 / 'all' 全部（默认）。
  // 项目中心要在那三者之间切，所以一次捞全再由页面筛；顶栏下拉只认在产。
  projects: (status = 'all') => fetch(`/api/projects?status=${encodeURIComponent(status)}`).then(toJson),
  openProject: (id) => fetch(`/api/projects/${id}/open`, { method: 'POST' }).then(toJson),
  setProjectStatus: (id, status) => fetch(`/api/projects/${id}/status`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
  }).then(toJson),
  // 批量：归档 archive / 恢复 restore / 加标签 tag
  batchProjects: (body) => fetch('/api/projects/batch', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  batchDeleteProjects: (ids, confirm) => fetch('/api/projects/batch-delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, confirm }),
  }).then(toJson),

  // 用户与角色（系统设置 · 用户与角色页）
  users: () => fetch('/api/users').then(toJson),
  roles: () => fetch('/api/roles').then(toJson),
  permissions: () => fetch('/api/permissions').then(toJson),
  createUser: (body) => fetch('/api/users', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  updateUser: (id, body) => fetch(`/api/users/${id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  setUserRoles: (id, roles) => fetch(`/api/users/${id}/roles`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ roles }),
  }).then(toJson),
  resetUserPassword: (id, newPassword) => fetch(`/api/users/${id}/password`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ new_password: newPassword }),
  }).then(toJson),
  setRolePermissions: (id, permissions) => fetch(`/api/roles/${id}/permissions`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ permissions }),
  }).then(toJson),
  deleteUser: (id, body) => fetch(`/api/users/${id}`, {
    method: 'DELETE', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),

  // 项目协作人（只有所有人与管理员能读写这张名单）
  memberCandidates: () => fetch('/api/member-candidates').then(toJson),
  projectMembers: (id) => fetch(`/api/projects/${id}/members`).then(toJson),
  addProjectMember: (id, userId) => fetch(`/api/projects/${id}/members`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId }),
  }).then(toJson),
  removeProjectMember: (id, userId) => fetch(`/api/projects/${id}/members/${userId}`, {
    method: 'DELETE',
  }).then(toJson),
  // 转移所有权。规则在后端：接手人必须是当前协作人，交完自己降为协作人
  transferProject: (id, userId) => fetch(`/api/projects/${id}/transfer`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId }),
  }).then(toJson),
  health: () => fetch('/api/health').then(toJson),
  updateProject: (id, body) => fetch(`/api/projects/${id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  projectSummary: (id) => fetch(`/api/projects/${id}/summary`).then(toJson),
  // 原来这里自己写了一段 `if (!res.ok) throw new Error(status + ' ' + statusText)`，
  // 把后端那句给人看的 detail 换成了 "409 Conflict"——同一个页面里其他 40 个函数
  // 都走 toJson，只有它不听话。改回 toJson：错误照样抛，但说的是人话。
  deleteProject: (id, confirmName) => fetch(
    `/api/projects/${id}?confirm=${encodeURIComponent(confirmName)}`,
    { method: 'DELETE' }).then(toJson),
  createProject: (body) => fetch('/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),

  // worker 在线状态与队列长度（顶栏/侧栏/首页共用）
  workerStatus: () => fetch('/api/worker/status').then(toJson),

  // 模型双轨配置：读全部、整批写回
  providers: () => fetch('/api/providers').then(toJson),
  providerManifest: () => fetch('/api/providers/manifest').then(toJson),
  currentProviders: () => fetch('/api/providers/current').then(toJson),
  saveProviders: (rows) => fetch('/api/providers', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(rows),
  }).then(toJson),

  // 创作单元 / 分镜 / 资产
  episodes: (pid) => fetch(`/api/projects/${pid}/episodes`).then(toJson),
  createEpisode: (pid, title) => fetch(`/api/projects/${pid}/episodes`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  }).then(toJson),
  // 这三个 DELETE 原来漏了 .then(toJson)，返回的是原始 Response：
  // 后端 403/409/500 也照样"成功"，页面刷新一下就像真删掉了。
  // 204 没响应体，toJson 会回 null，调用方本来也不看返回值。
  deleteEpisode: (id) => fetch(`/api/episodes/${id}`, { method: 'DELETE' }).then(toJson),
  setEpisodeStatus: (id, status) => fetch(`/api/episodes/${id}/status`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
  }).then(toJson),

  shots: (eid) => fetch(`/api/episodes/${eid}/shots`).then(toJson),
  createShot: (eid, body) => fetch(`/api/episodes/${eid}/shots`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  deleteShot: (id) => fetch(`/api/shots/${id}`, { method: 'DELETE' }).then(toJson),

  // 文档（W05）：元数据在库里、正文在工作区里、版本在 Git 里。
  // 前端只传文档 id，一个接口都不接受路径 —— 路径由服务端生成并钉在库里，
  // "非法路径不可读写"这件事才有落脚的地方。
  documents: (pid, kind) => fetch(
    `/api/projects/${pid}/documents${kind ? `?kind=${encodeURIComponent(kind)}` : ''}`).then(toJson),
  createDocument: (pid, body) => fetch(`/api/projects/${pid}/documents`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  document: (id) => fetch(`/api/documents/${id}`).then(toJson),
  // 改标题与挂靠的分集。**不含路径**：改名不挪文件，历史是按路径记的
  updateDocument: (id, body) => fetch(`/api/documents/${id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  setDocumentStatus: (id, status) => fetch(`/api/documents/${id}/status`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
  }).then(toJson),
  deleteDocument: (id) => fetch(`/api/documents/${id}`, { method: 'DELETE' }).then(toJson),
  docContent: (id) => fetch(`/api/documents/${id}/content`).then(toJson),
  // expected_version 必填：不带版本后端直接 422，前端也不给"最后一次保存覆盖前一次"的便道
  saveDocContent: (id, content, expectedVersion) => fetch(`/api/documents/${id}/content`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, expected_version: expectedVersion }),
  }).then(toJson),
  // 拿锁不报错也拿不到就是 held：那是界面要显示的状态，不是要弹的失败
  lockDocument: (id) => fetch(`/api/documents/${id}/lock`, { method: 'POST' }).then(toJson),
  unlockDocument: (id) => fetch(`/api/documents/${id}/lock`, { method: 'DELETE' }).then(toJson),
  docVersions: (id) => fetch(`/api/documents/${id}/versions`).then(toJson),
  docDiff: (id, commit) => fetch(`/api/documents/${id}/versions/${encodeURIComponent(commit)}/diff`).then(toJson),

  // 资产：scope 决定看自有／全局池／两者都要。默认 all——镜头要引用全局资产时
  // 得先搜得到它，否则"跨项目复用"这句话在界面上无处可点
  assets: (pid, kind, scope) => fetch(
    `/api/projects/${pid}/assets?` + new URLSearchParams(
      { ...(kind ? { kind } : {}), ...(scope && scope !== 'all' ? { scope } : {}) })).then(toJson),
  createAsset: (pid, body) => fetch(`/api/projects/${pid}/assets`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  asset: (id) => fetch(`/api/assets/${id}`).then(toJson),
  // 整体提交：名称／类型／简介／提示词／别名／标签／范围一次改完。
  // 同一个字段不留第二个编辑入口，所以没有"只改标签"这种半截接口
  updateAsset: (id, body) => fetch(`/api/assets/${id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  setAssetCover: (id, path) => fetch(`/api/assets/${id}/cover`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  }).then(toJson),
  assetShots: (id) => fetch(`/api/assets/${id}/shots`).then(toJson),
  deleteAsset: (id) => fetch(`/api/assets/${id}`, { method: 'DELETE' }).then(toJson),
  uploadAssetImage: (id, file) => {
    const form = new FormData();
    form.append('file', file);
    return fetch(`/api/assets/${id}/images`, { method: 'POST', body: form }).then(toJson);
  },
  deleteAssetImage: (id, path) => fetch(
    `/api/assets/${id}/images?path=${encodeURIComponent(path)}`, { method: 'DELETE' }).then(toJson),
};
