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
  // 转移所有权。规则在后端：接手人要启用且层级不高于发起人（v0.81 起不必先是协作人），交完自己降为协作人
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

  // 生成中心：队列、成本账、参数模板。
  // 状态可以逗号分隔（"进行中"＝三个状态一起看），其余筛选空值就不带。
  jobs: (params = {}) => fetch('/api/jobs?' + new URLSearchParams(
    Object.fromEntries(Object.entries(params).filter(([, v]) => v !== '' && v != null)),
  )).then(toJson),
  job: (id) => fetch(`/api/jobs/${id}`).then(toJson),
  submitJob: (body) => fetch('/api/jobs', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  cancelJob: (id) => fetch(`/api/jobs/${id}/cancel`, { method: 'POST' }).then(toJson),
  retryJob: (id) => fetch(`/api/jobs/${id}/retry`, { method: 'POST' }).then(toJson),
  // 重试前那一屏：只读，不创建任何东西。云端重试会多一笔预计计费，
  // 所以界面先拿这份预览弹确认，人看清"会继承什么、可能再花多少"再点。
  retryPreview: (id) => fetch(`/api/jobs/${id}/retry-preview`).then(toJson),
  // 人工核对一条待核对的云端作业。decision：succeeded／failed 必须带实际金额，
  // pending 只留备注。不重新提交、不改提示词与参数快照。
  reconcileJob: (id, body) => fetch(`/api/jobs/${id}/reconcile`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  // 把一条云端作业显式送进"待核对"。供应商说成功、产物却打不开时用——
  // 那笔钱可能已经花了，先挂起来核对，比让系统替人决定重试更安全。
  markAttention: (id, reason) => fetch(`/api/jobs/${id}/attention`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  }).then(toJson),
  jobLogs: (id) => fetch(`/api/jobs/${id}/logs`).then(toJson),
  jobArtifacts: (id) => fetch(`/api/jobs/${id}/artifacts`).then(toJson),
  // 产物走浏览器直连（图片/视频能直接预览）；它不是 JSON，不能过 toJson
  jobArtifactUrl: (id, index) => `/api/jobs/${id}/artifacts/${index}`,
  jobCosts: (projectId) => fetch('/api/jobs/costs'
    + (projectId ? `?project_id=${projectId}` : '')).then(toJson),
  providerOptions: () => fetch('/api/providers/available').then(toJson),
  generationProfiles: (projectId, capability) => fetch('/api/generation-profiles?'
    + new URLSearchParams({ ...(projectId ? { project_id: projectId } : {}),
      ...(capability ? { capability } : {}) })).then(toJson),
  createGenerationProfile: (body) => fetch('/api/generation-profiles', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  updateGenerationProfile: (id, body) => fetch(`/api/generation-profiles/${id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  deleteGenerationProfile: (id) => fetch(`/api/generation-profiles/${id}`,
    { method: 'DELETE' }).then(toJson),

  // 模型双轨配置：读全部、整批写回
  providers: () => fetch('/api/providers').then(toJson),
  providerManifest: () => fetch('/api/providers/manifest').then(toJson),
  currentProviders: () => fetch('/api/providers/current').then(toJson),
  saveProviders: (rows) => fetch('/api/providers', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(rows),
  }).then(toJson),
  // 模型验证（P0）：四种出处（环境检查／模拟／真实调用／目标机）各记各的，
  // 谁也不能替谁升级。历史单独一张表，只追加不改。
  providerValidations: (limit = 50) => fetch(
    `/api/providers/validations?limit=${limit}`).then(toJson),
  // 环境检查：不发网络、不加载权重，所以永远不能替代真实调用验证。
  validateProvider: (body) => fetch('/api/providers/validate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  // 从一条已成功的作业登记真实调用凭据。只有管理员能点——这一格升级是有后果的判断。
  validateJob: (jobId, body) => fetch(`/api/providers/validate-job/${jobId}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),

  // 模型中心（0035）：登记层与策略层。
  // 目录是"清单 × 登记"的联表，所以只有这一个读接口就够——前端不再自己拼第二套口径。
  models: () => fetch('/api/models').then(toJson),
  modelStats: () => fetch('/api/models/stats').then(toJson),
  createModel: (body) => fetch('/api/models', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  patchModel: (id, body) => fetch(`/api/models/${id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  // 撤登记必须带 confirm：这一步只删登记行，权重文件与历史作业都不动
  removeModel: (id) => fetch(`/api/models/${id}?confirm=${encodeURIComponent('撤登记')}`,
    { method: 'DELETE' }).then(toJson),
  checkModel: (id) => fetch(`/api/models/${id}/check`, { method: 'POST' }).then(toJson),
  modelPolicies: () => fetch('/api/model-policies').then(toJson),
  saveModelPolicies: (rows) => fetch('/api/model-policies', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(rows),
  }).then(toJson),
  // 场景候选：只读、登录即可。返回里带 auto_switch=false——备用轨不会自动顶上。
  resolveScene: (key) => fetch(`/api/model-policies/resolve/${encodeURIComponent(key)}`).then(toJson),

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
  // 整体改一镜：内容／提示词／时长／引用资产；镜号与所属集不在可改范围
  updateShot: (id, body) => fetch(`/api/shots/${id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
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
  // 文档 AI（W11）：计价 → 提交 Job → 轮询预览 → 应用。
  // 选择文字一起带上，服务端拿它做偏移的第二重校验（emoji 会让码点与 UTF-16 差一截）。
  docAiOptions: (id, body) => fetch(
    `/api/documents/${id}/ai-options?` + new URLSearchParams({
      operation: body.operation, scope: body.scope,
      start: String(body.start ?? 0), end: String(body.end ?? 0),
      selection_text: body.selection_text || '',
    })).then(toJson),
  docAiSubmit: (id, body) => fetch(`/api/documents/${id}/ai-jobs`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  docAiJob: (id, jobId) => fetch(`/api/documents/${id}/ai-jobs/${jobId}`).then(toJson),
  docAiApply: (id, jobId) => fetch(
    `/api/documents/${id}/ai-jobs/${jobId}/apply`, { method: 'POST' }).then(toJson),

  // 文本抽取（W13）：计价是同一条文本默认轨；候选要人确认才入资产。
  extractOptions: (pid, docId) => fetch(
    `/api/projects/${pid}/extract/options?doc_id=${docId}`).then(toJson),
  extractRuns: (pid) => fetch(`/api/projects/${pid}/extract/runs`).then(toJson),
  extractSubmit: (pid, body) => fetch(`/api/projects/${pid}/extract/runs`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  extractRun: (runId) => fetch(`/api/extract/runs/${runId}`).then(toJson),
  extractItems: (runId, params = {}) => fetch(
    `/api/extract/runs/${runId}/items?` + new URLSearchParams(
      Object.fromEntries(Object.entries(params).filter(([, v]) => v)))).then(toJson),
  extractAccept: (itemId, body = {}) => fetch(`/api/extract/items/${itemId}/accept`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  extractMerge: (itemId, assetId) => fetch(`/api/extract/items/${itemId}/merge`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ asset_id: assetId }),
  }).then(toJson),
  extractReject: (itemId, reason = '') => fetch(`/api/extract/items/${itemId}/reject`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  }).then(toJson),

  // 审核中心（W14）：校验报告按正文版本冻结，签字按锚点落
  reviewQueue: (pid) => fetch(`/api/projects/${pid}/reviews`).then(toJson),
  reviewGate: (docId) => fetch(`/api/documents/${docId}/review-gate`).then(toJson),
  runReviewGate: (docId) => fetch(`/api/documents/${docId}/review-gate`, {
    method: 'POST' }).then(toJson),
  reviewSign: (docId, body) => fetch(`/api/documents/${docId}/review-sign`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  // 产物审核（0025／D1）：审的是一条已成功作业的产物，签字仍只在审核中心落
  jobReviews: (pid) => fetch(`/api/projects/${pid}/job-reviews`).then(toJson),
  jobReview: (jobId) => fetch(`/api/jobs/${jobId}/review`).then(toJson),
  jobReviewSign: (jobId, body) => fetch(`/api/jobs/${jobId}/review-sign`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),

  // 产线中心（0026／阶段 C）：只做编排。不建第二条队列、不记第二本账——
  // 这里拿到的 job_id／review_id 都是引用，成本与产物仍去生成中心读。
  // 发起运行之前的生产前检查：只读，blocking／warnings／info 三档
  preflight: (pid, episodeId, templateId) => fetch(
    `/api/projects/${pid}/preflight?` + new URLSearchParams({
      episode_id: episodeId, ...(templateId ? { template_id: templateId } : {}),
    })).then(toJson),
  pipelineTemplates: () => fetch('/api/pipeline/templates').then(toJson),
  pipelineTemplate: (id) => fetch(`/api/pipeline/templates/${id}`).then(toJson),
  pipelineRuns: (pid) => fetch(`/api/projects/${pid}/pipeline/runs`).then(toJson),
  pipelineRun: (id) => fetch(`/api/pipeline/runs/${id}`).then(toJson),
  createPipelineRun: (pid, body) => fetch(`/api/projects/${pid}/pipeline/runs`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  advancePipelineRun: (id) => fetch(`/api/pipeline/runs/${id}/advance`,
    { method: 'POST' }).then(toJson),
  pausePipelineRun: (id) => fetch(`/api/pipeline/runs/${id}/pause`,
    { method: 'POST' }).then(toJson),
  resumePipelineRun: (id) => fetch(`/api/pipeline/runs/${id}/resume`,
    { method: 'POST' }).then(toJson),
  cancelPipelineRun: (id, reason = '') => fetch(`/api/pipeline/runs/${id}/cancel`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  }).then(toJson),
  retryPipelineNode: (id) => fetch(`/api/pipeline/nodes/${id}/retry`,
    { method: 'POST' }).then(toJson),

  // 成片中心（阶段 D）：每次合成是一条普通 Job，版本只增不改
  compositions: (pid, params = {}) => fetch(
    `/api/projects/${pid}/compositions?` + new URLSearchParams(
      Object.fromEntries(Object.entries(params).filter(([, v]) => v)))).then(toJson),
  createComposition: (pid, body) => fetch(`/api/projects/${pid}/compositions`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  composition: (id) => fetch(`/api/compositions/${id}`).then(toJson),
  // 字幕是"喂给一次合成的输入文件"，不进库、不进 Git 工作区；
  // 列表同时带回这台机器的 FFmpeg 能不能烧字幕，界面据此决定控件是可选还是禁用
  compositionSubtitles: (pid) => fetch(
    `/api/projects/${pid}/composition-subtitles`).then(toJson),
  uploadCompositionSubtitle: (pid, file) => {
    const form = new FormData();
    form.append('file', file);
    return fetch(`/api/projects/${pid}/composition-subtitles`, {
      method: 'POST', body: form,
    }).then(toJson);
  },
  deleteCompositionSubtitle: (pid, path) => fetch(
    `/api/projects/${pid}/composition-subtitles?path=${encodeURIComponent(path)}`,
    { method: 'DELETE' }).then(toJson),

  // 整季生产（阶段 E）：一季只是若干条单集运行的排产记录
  seasons: (pid) => fetch(`/api/projects/${pid}/seasons`).then(toJson),
  season: (id) => fetch(`/api/seasons/${id}`).then(toJson),
  createSeason: (pid, body) => fetch(`/api/projects/${pid}/seasons`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  pauseSeason: (id) => fetch(`/api/seasons/${id}/pause`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: '{}',
  }).then(toJson),
  resumeSeason: (id) => fetch(`/api/seasons/${id}/resume`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: '{}',
  }).then(toJson),
  cancelSeason: (id, reason = '') => fetch(`/api/seasons/${id}/cancel`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  }).then(toJson),
  retrySeasonFailed: (id) => fetch(`/api/seasons/${id}/retry-failed`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: '{}',
  }).then(toJson),

  // 音频中心（W15）：素材仍送生成中心的同一条队列
  audioClips: (pid, params = {}) => fetch(
    `/api/projects/${pid}/audio-clips?` + new URLSearchParams(
      Object.fromEntries(Object.entries(params).filter(([, v]) => v)))).then(toJson),
  createAudioClip: (pid, body) => fetch(`/api/projects/${pid}/audio-clips`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  updateAudioClip: (id, body) => fetch(`/api/audio-clips/${id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  submitAudioClip: (id) => fetch(`/api/audio-clips/${id}/submit`,
    { method: 'POST' }).then(toJson),
  deleteAudioClip: (id) => fetch(`/api/audio-clips/${id}`,
    { method: 'DELETE' }).then(toJson),

  // 项目看板（W16）：只读聚合；导出是纯下载，不写状态
  dashboard: (pid) => fetch(`/api/projects/${pid}/dashboard`).then(toJson),

  // 首页工作台：跨模块待办聚合（审核待签、失败、待核对、进行中的运行）。
  // 只读；服务端按"我看得见的在产项目"先筛过，前端不再自己过滤一遍。
  workbench: () => fetch('/api/workbench').then(toJson),

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

  // 资产增强（0033）：人物档案、命名形态、生成与产物导入。
  // 视图里不散落裸 fetch——同一个接口的错误处理要只有一份。
  assetProfile: (id) => fetch(`/api/assets/${id}/character-profile`).then(toJson),
  saveAssetProfile: (id, body) => fetch(`/api/assets/${id}/character-profile`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  assetVariants: (id) => fetch(`/api/assets/${id}/variants`).then(toJson),
  createAssetVariant: (id, body) => fetch(`/api/assets/${id}/variants`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  updateAssetVariant: (vid, body) => fetch(`/api/asset-variants/${vid}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  // 204 没有响应体，toJson 已经认这个情况（回 null）
  deleteAssetVariant: (vid) => fetch(`/api/asset-variants/${vid}`, { method: 'DELETE' }).then(toJson),
  // 摘掉形态名下的**一张**图（v0.99）。以前只有"删整个形态"这一个出口，
  // 导错一张就得把其余的图一起扔了——基础参考图有这条删除路，形态也必须要有。
  deleteAssetVariantImage: (vid, path) => fetch(
    `/api/asset-variants/${vid}/images?path=${encodeURIComponent(path)}`,
    { method: 'DELETE' }).then(toJson),
  assetGenerationSubmit: (pid, body) => fetch(`/api/projects/${pid}/asset-generations`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
  assetGeneration: (pid, jobId) => fetch(
    `/api/projects/${pid}/asset-generations/${jobId}`).then(toJson),
  // 刷新后把"我刚生成的那批"读回来：状态在 job 表里，不在浏览器里攒副本
  assetGenerations: (pid, assetId) => fetch(`/api/projects/${pid}/asset-generations?`
    + new URLSearchParams({ ...(assetId ? { target_asset_id: assetId } : {}), limit: 12 }))
    .then(toJson),
  assetImportImage: (id, body) => fetch(`/api/assets/${id}/generated-images`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(toJson),
};
