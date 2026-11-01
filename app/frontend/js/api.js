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

  // 模型双轨配置：读全部、整批写回
  providers: () => fetch('/api/providers').then(toJson),
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
