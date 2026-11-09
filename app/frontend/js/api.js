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
    // 登录接口自己的 401 是「口令错了」，不能拿它去挂过期闸门。
    //
    // 原来这里靠 `/\/api\/auth\/(login|password)$/.test(res.url)` 认端点——
    // 而 `res.url` 是**解析后的绝对 URL**，带上查询串（`?next=/models`）或以
    // 重定向形式回来（末尾多个 `/`）时正则都不匹配，于是「口令错」被当成
    // 「会话过期」，人看到的是重新登录的闸门，而不是「口令错了」。
    // 现在由调用方在 init 上打个显式标记，不再从 URL 反推意图。
    if (res.status === 401 && !(res.__authEndpoint)) {
      window.dispatchEvent(new CustomEvent('app:unauthorized'));
    }
    throw err;
  }
  // 204 没有响应体，直接 JSON.parse 会炸
  return text ? JSON.parse(text) : null;
}

/* ---------------------------------------------------------------- 请求收敛
 *
 * 这个文件里原来有 67 处逐字重复的
 *     method: 'POST', headers: { 'Content-Type': 'application/json' },
 *     body: JSON.stringify({...}),
 * 以及 187 处 .then(toJson)。重复的代价不是「看着啰嗦」，而是**横切改动要改 67 遍**：
 * 加一个请求头、加超时、加 trace id、改重试策略，每一处都得记得。
 *
 * 收成 send／get 两个内部函数：
 *   - 有 body 才加 Content-Type 与 JSON.stringify（DELETE 带不带 body 都支持）；
 *   - 传 undefined 表示「这次没有 body」，传 {} 才是「发一个空对象」——
 *     原来有 3 处 body 是字符串 '{}'（语义就是空对象），统一成 {}；
 *   - toJson 仍是唯一出口，401 闸门、detail 提取、204 空响应那一套一行没动。
 *
 * **对外签名一个字都没变**：api.xxx(...) 的调用方（十几个视图）不用改。
 */
/* ------------------------------------------------------------ 超时与取消
 *
 * 这个文件原来一次 `fetch` 都没有超时。后端一旦 hang（进程卡死、SQLite 被
 * 长事务锁住、反向代理没回），Promise 就永不 settle：按钮的 `disabled` 不复位、
 * 三处轮询僵在「在飞」状态、`run()` 的 catch 也永远等不到。人看到的是
 * 「点了没反应」，刷新页面才好——而刷新并不解决后端 hang，只是重来一次。
 *
 * 统一挂一个 15 秒上限。选 15 秒：本机这台机器上的接口正常都在 1 秒内，
 * 最慢的整季排产、产物清单也在几秒；超过 15 秒基本等于「这次不会有结果了」，
 * 再等只是延长人以为它在干活的时间。
 *
 * 超时抛出的 Error 带 `status = 0`：0 不是一个真实 HTTP 状态码，用它表示
 * 「根本没拿到响应」。这样 `run()` 照旧走同一条出口弹提示，视图里那些
 * `if (err.status === 403)` 也不会把超时误当成权限问题。
 */
const TIMEOUT_MS = 15_000;

/** 给一次请求挂上超时；调用方自己传了 signal 就以调用方为准（它可能还要取消）。 */
function withTimeout(init = {}) {
  if (init.signal) return init;
  if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) {
    return { ...init, signal: AbortSignal.timeout(TIMEOUT_MS) };
  }
  // 老内核没有 AbortSignal.timeout：降级成手搓的 AbortController。
  // 不求优雅，只求「一定会 settle」这一条性质在所有浏览器上都成立。
  const ctl = new AbortController();
  setTimeout(() => ctl.abort(), TIMEOUT_MS);
  return { ...init, signal: ctl.signal };
}

/**
 * fetch + 超时 + 把中断翻译成一句人话。所有请求（含裸 fetch 的那几处）都过它。
 *
 * `opts.authEndpoint`：这个端点返回的 401 表示「口令错了」而不是「会话过期」。
 * 由调用方显式声明，不从 URL 反推——见 toJson 里那段注释。
 */
async function request(url, init, opts) {
  // 信号是调用方带进来的 ⇒ 中断是它自己的决定（换项目要作废旧请求之类），
  // 不该被包成「请求超时」；只有我们自己加的那个信号才代表超时。
  const callerSignal = !!(init && init.signal);
  let res;
  try {
    res = await fetch(url, withTimeout(init));
  } catch (err) {
    if (!callerSignal && err
        && (err.name === 'TimeoutError' || err.name === 'AbortError')) {
      const out = new Error('请求超时（15 秒没有响应），后端可能卡住了');
      out.status = 0;
      throw out;
    }
    throw err;
  }
  // 标记挂在这次 Response 实例上，只给 toJson 认；不进响应体、不污染全局。
  if (opts && opts.authEndpoint) res.__authEndpoint = true;
  return res;
}

const send = (method, url, body, opts) => {
  const init = { method };
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(body);
  }
  return request(url, init, opts).then(toJson);
};
const get = (url) => request(url).then(toJson);
// 登录类端点：它们的 401 是「口令错了」，要显式告诉 toJson 别挂过期闸门
const AUTH_ENDPOINT = { authEndpoint: true };

export const api = {
  me: () => get('/api/auth/me'),
  login: (username, password) => send('POST', '/api/auth/login',
    { username, password }, AUTH_ENDPOINT),
  logout: () => send('POST', '/api/auth/logout'),
  changePassword: (oldPassword, newPassword) => send('POST', '/api/auth/password',
    { old_password: oldPassword, new_password: newPassword }, AUTH_ENDPOINT),
  // status：'active' 在产 / 'archived' 已归档 / 'all' 全部（默认）。
  // 项目中心要在那三者之间切，所以一次捞全再由页面筛；顶栏下拉只认在产。
  projects: (status = 'all') => get(`/api/projects?status=${encodeURIComponent(status)}`),
  openProject: (id) => send('POST', `/api/projects/${id}/open`),
  setProjectStatus: (id, status) => send('PATCH', `/api/projects/${id}/status`, { status }),
  // 批量：归档 archive / 恢复 restore / 加标签 tag
  batchProjects: (body) => send('POST', '/api/projects/batch', body),
  batchDeleteProjects: (ids, confirm) => send('POST', '/api/projects/batch-delete', { ids, confirm }),

  // 用户与角色（系统设置 · 用户与角色页）
  users: () => get('/api/users'),
  roles: () => get('/api/roles'),
  permissions: () => get('/api/permissions'),
  createUser: (body) => send('POST', '/api/users', body),
  updateUser: (id, body) => send('PATCH', `/api/users/${id}`, body),
  setUserRoles: (id, roles) => send('PUT', `/api/users/${id}/roles`, { roles }),
  resetUserPassword: (id, newPassword) => send('POST', `/api/users/${id}/password`, { new_password: newPassword }),
  setRolePermissions: (id, permissions) => send('PUT', `/api/roles/${id}/permissions`, { permissions }),
  deleteUser: (id, body) => send('DELETE', `/api/users/${id}`, body),

  // 项目协作人（只有所有人与管理员能读写这张名单）
  memberCandidates: () => get('/api/member-candidates'),
  projectMembers: (id) => get(`/api/projects/${id}/members`),
  addProjectMember: (id, userId) => send('POST', `/api/projects/${id}/members`, { user_id: userId }),
  removeProjectMember: (id, userId) => send('DELETE', `/api/projects/${id}/members/${userId}`),
  // 转移所有权。规则在后端：接手人要启用且层级不高于发起人（v0.81 起不必先是协作人），交完自己降为协作人
  transferProject: (id, userId) => send('POST', `/api/projects/${id}/transfer`, { user_id: userId }),
  health: () => get('/api/health'),
  updateProject: (id, body) => send('PUT', `/api/projects/${id}`, body),
  projectSummary: (id) => get(`/api/projects/${id}/summary`),
  // 原来这里自己写了一段 `if (!res.ok) throw new Error(status + ' ' + statusText)`，
  // 把后端那句给人看的 detail 换成了 "409 Conflict"——同一个页面里其他 40 个函数
  // 都走 toJson，只有它不听话。改回 toJson：错误照样抛，但说的是人话。
  deleteProject: (id, confirmName) => send('DELETE', `/api/projects/${id}?confirm=${encodeURIComponent(confirmName)}`),
  createProject: (body) => send('POST', '/api/projects', body),

  // worker 在线状态与队列长度（顶栏/侧栏/首页共用）
  workerStatus: () => get('/api/worker/status'),

  // 并发上限（0047）：读任何登录者都行，改要 system:config。
  // 危险档（估算撑破本机内存）必须在 body 里带 acknowledge_risk: true，
  // 否则后端 400 —— 界面把这句话显示出来，不自己替人决定。
  workerConcurrency: () => get('/api/worker/concurrency'),
  setWorkerConcurrency: (body) => send('PUT', '/api/worker/concurrency', body),

  // worker 进程启停（0048）。这是**手动启停不是守护**——崩了不会自动拉起，
  // 界面文案必须说出来（详见 worker_supervisor 模块头）。
  workerProcess: () => get('/api/worker/process'),
  startWorker: (body) => send('POST', '/api/worker/process/start', body || {}),
  stopWorker: () => send('POST', '/api/worker/process/stop', {}),

  // 生成中心：队列、成本账、参数模板。
  // 状态可以逗号分隔（"进行中"＝三个状态一起看），其余筛选空值就不带。
  // 队列要翻页，所以这个函数比别的多两件事：
  //   * before_id 是游标（上一页最后一行的 id），只取比它更早的行；
  //   * 返回值从数组变成 { items, total }，total 来自响应头 X-Total-Count。
  // 为什么要带总数：原来队列写死 limit 100，超出的部分压根没取，而界面上
  // 那格数字会停在 100，看着像总共就这些。有真分母才分得开「就这些」和「还有」。
  // 这一条不能走 get()：get 已经把 body 解析成对象返回了，响应头拿不到。
  jobs: async (params = {}) => {
    const qs = new URLSearchParams(
      Object.fromEntries(Object.entries(params).filter(([, v]) => v !== '' && v != null)));
    const res = await request(`/api/jobs?${qs}`);
    const items = await toJson(res);
    return { items, total: Number(res.headers.get('X-Total-Count') || 0) };
  },
  job: (id) => get(`/api/jobs/${id}`),
  submitJob: (body) => send('POST', '/api/jobs', body),
  cancelJob: (id) => send('POST', `/api/jobs/${id}/cancel`),
  retryJob: (id) => send('POST', `/api/jobs/${id}/retry`),
  // 重试前那一屏：只读，不创建任何东西。云端重试会多一笔预计计费，
  // 所以界面先拿这份预览弹确认，人看清"会继承什么、可能再花多少"再点。
  retryPreview: (id) => get(`/api/jobs/${id}/retry-preview`),
  // 人工核对一条待核对的云端作业。decision：succeeded／failed 必须带实际金额，
  // pending 只留备注。不重新提交、不改提示词与参数快照。
  reconcileJob: (id, body) => send('POST', `/api/jobs/${id}/reconcile`, body),
  // 把一条云端作业显式送进"待核对"。供应商说成功、产物却打不开时用——
  // 那笔钱可能已经花了，先挂起来核对，比让系统替人决定重试更安全。
  markAttention: (id, reason) => send('POST', `/api/jobs/${id}/attention`, { reason }),
  jobLogs: (id) => get(`/api/jobs/${id}/logs`),
  jobArtifacts: (id) => get(`/api/jobs/${id}/artifacts`),
  // §14 异常目录在这一条作业上的诊断。单独一个请求而不是塞进详情：
  // 队列页每次刷几十行，七项解释只在点开这一条时要用。
  jobDiagnosis: (id) => get(`/api/jobs/${id}/diagnosis`),
  // 产物走浏览器直连（图片/视频能直接预览）；它不是 JSON，不能过 toJson
  jobArtifactUrl: (id, index) => `/api/jobs/${id}/artifacts/${index}`,
  jobCosts: (projectId) => get('/api/jobs/costs'
    + (projectId ? `?project_id=${projectId}` : '')),
  providerOptions: () => get('/api/providers/available'),
  generationProfiles: (projectId, capability) => get('/api/generation-profiles?'
    + new URLSearchParams({ ...(projectId ? { project_id: projectId } : {}),
      ...(capability ? { capability } : {}) })),
  createGenerationProfile: (body) => send('POST', '/api/generation-profiles', body),
  updateGenerationProfile: (id, body) => send('PUT', `/api/generation-profiles/${id}`, body),
  deleteGenerationProfile: (id) => send('DELETE', `/api/generation-profiles/${id}`),

  // 模型双轨配置：读全部、整批写回
  providers: () => get('/api/providers'),
  providerManifest: () => get('/api/providers/manifest'),
  currentProviders: () => get('/api/providers/current'),
  saveProviders: (rows) => send('PUT', '/api/providers', rows),
  // 模型验证（P0）：四种出处（环境检查／模拟／真实调用／目标机）各记各的，
  // 谁也不能替谁升级。历史单独一张表，只追加不改。
  // 验证历史：一页 + 总量。`before_id` 拿上一页最后一条的 id 往前翻——
  // 表只追加不改，所以"比某条更早"是稳定边界（用 offset 会因为新写入而错位）。
  // 总量在响应头 `X-Total-Count` 里，调用方据此说"共 N 条"而不是拿"少于 limit"猜。
  providerValidations: async (limit = 50, beforeId = null) => {
    const qs = new URLSearchParams({ limit: String(limit) });
    if (beforeId) qs.set('before_id', String(beforeId));
    // 这一条**不能走 get()**：它要读完响应头再走 toJson，
    // 而 get() 已经把 body 解析成对象返回了（headers 拿不到）。
    const res = await request(`/api/providers/validations?${qs}`);
    const items = await toJson(res);
    return { items, total: Number(res.headers.get('X-Total-Count') || 0) };
  },
  // 环境检查：不发网络、不加载权重，所以永远不能替代真实调用验证。
  validateProvider: (body) => send('POST', '/api/providers/validate', body),
  // 从一条已成功的作业登记真实调用凭据。只有管理员能点——这一格升级是有后果的判断。
  validateJob: (jobId, body) => send('POST', `/api/providers/validate-job/${jobId}`, body),

  // 模型中心（0035）：登记层与策略层。
  // 目录是"清单 × 登记"的联表，所以只有这一个读接口就够——前端不再自己拼第二套口径。
  models: () => get('/api/models'),
  modelStats: () => get('/api/models/stats'),
  // 现查服务型引擎：`GET /api/models` 不出网（打开页面不该被一台卡住的服务拖住），
  // 所以"这一档到底连不连得上"有一个显式入口。fresh=1 绕过 15 秒缓存。
  probeModels: (fresh) => send('POST', `/api/models/probe?fresh=${fresh ? 1 : 0}`),
  createModel: (body) => send('POST', '/api/models', body),
  patchModel: (id, body) => send('PATCH', `/api/models/${id}`, body),
  // 撤登记必须带 confirm=1：这一步只删登记行，权重文件与历史作业都不动。
  // 别再往 URL 里塞中文口令——那个字面量会变成协议的一部分，改文案就改接口。
  removeModel: (id) => send('DELETE', `/api/models/${id}?confirm=1`),
  checkModel: (id) => send('POST', `/api/models/${id}/check`),
  modelPolicies: () => get('/api/model-policies'),
  saveModelPolicies: (rows) => send('PUT', '/api/model-policies', rows),
  // 场景候选：只读、登录即可。返回里带 auto_switch=false——备用轨不会自动顶上。
  resolveScene: (key) => get(`/api/model-policies/resolve/${encodeURIComponent(key)}`),
  // 场景骨架（九个任务场景的只读清单）。801b2c9 把 api.js 收敛成 get/send 出口时
  // 漏了这个方法名，generate.js 调用时同步抛 TypeError，提交弹窗的场景/路线/槽位
  // 三个下拉一起空掉——test_infra 的"方法名一致性"守卫就是为钉住这类漏口子而加。
  modelScenes: () => get('/api/model-policies/scenes'),

  // 引擎实例（0040）：这一族管的是"这一档引擎在哪个端点"，不是"跑了什么模型"。
  // 后端从 W-B 起就有这五个口子，但界面一直没接——于是 `engines.probe()` 只能对着
  // "还没有登记启用中的引擎实例"这句提示干瞪眼，而人没有办法在界面里加一条端点。
  engines: () => get('/api/engines'),
  createEngine: (body) => send('POST', '/api/engines', body),
  patchEngine: (id, body) => send('PATCH', `/api/engines/${id}`, body),
  // 撤实例同样带 confirm=1：只删这一行端点登记，应用与权重都不动
  removeEngine: (id) => send('DELETE', `/api/engines/${id}?confirm=1`),
  probeEngine: (id) => send('POST', `/api/engines/${id}/probe`),
  // 探测历史（0042）：只追加不改，最新在前。卡面只摆最近一条，这里是给它后面
  // 那个「看历史」用的——「这台是不是三天两头掉线」得看几条才答得上来。
  engineProbes: (id, limit = 20) =>
    get(`/api/engines/${id}/probes?limit=${limit}`),

  // 智能体中心（0041）
  // 这一族的读接口分两档：配置／工具表要 system:config，运行与待审批按项目可见范围。
  // 签字**不在这里**——批准与驳回走 `signAgentStep`，它落在审核中心（不变量 3）。
  agentOverview: () => get('/api/agent/overview'),
  agentConfigs: () => get('/api/agent/configs'),
  agentTools: () => get('/api/agent/tools'),
  agentPlanOptions: (qs) => get(`/api/agent/plan-options?${qs}`),
  agentRuns: (pid) => get(`/api/agent/runs?project_id=${pid}`),
  agentRun: (id) => get(`/api/agent/runs/${id}`),
  createAgentRun: (body) => send('POST', '/api/agent/runs', body),
  // 暂停／恢复用 POST＋空 body：它们改的是"外生意愿"那两位，不是内容，
  // 用 PATCH 反而会让读代码的人以为还有别的字段可改
  pauseAgentRun: (id) => send('POST', `/api/agent/runs/${id}/pause`),
  resumeAgentRun: (id) => send('POST', `/api/agent/runs/${id}/resume`),
  cancelAgentRun: (id, reason) => send('POST', `/api/agent/runs/${id}/cancel?reason=${encodeURIComponent(reason || '')}`),
  agentPending: () => get('/api/agent/pending'),
  agentStepReview: (id) => get(`/api/agent-steps/${id}/review`),
  // 审核中心那一栏（动作审批）读它：签字写口在审核中心，智能体中心只显示状态
  agentActionReviews: (pid) =>
    get(`/api/projects/${pid}/agent-action-reviews`),
  signAgentStep: (id, body) => send('POST', `/api/agent-steps/${id}/review-sign`, body),

  // 创作单元 / 分镜 / 资产
  episodes: (pid) => get(`/api/projects/${pid}/episodes`),
  createEpisode: (pid, title) => send('POST', `/api/projects/${pid}/episodes`, { title }),
  // 这三个 DELETE 原来漏了 .then(toJson)，返回的是原始 Response：
  // 后端 403/409/500 也照样"成功"，页面刷新一下就像真删掉了。
  // 204 没响应体，toJson 会回 null，调用方本来也不看返回值。
  deleteEpisode: (id) => send('DELETE', `/api/episodes/${id}`),
  setEpisodeStatus: (id, status) => send('PATCH', `/api/episodes/${id}/status`, { status }),

  shots: (eid) => get(`/api/episodes/${eid}/shots`),
  createShot: (eid, body) => send('POST', `/api/episodes/${eid}/shots`, body),
  // 整体改一镜：内容／提示词／时长／引用资产；镜号与所属集不在可改范围
  updateShot: (id, body) => send('PUT', `/api/shots/${id}`, body),
  deleteShot: (id) => send('DELETE', `/api/shots/${id}`),

  // 文档（W05）：元数据在库里、正文在工作区里、版本在 Git 里。
  // 前端只传文档 id，一个接口都不接受路径 —— 路径由服务端生成并钉在库里，
  // "非法路径不可读写"这件事才有落脚的地方。
  documents: (pid, kind) => get(`/api/projects/${pid}/documents${kind ? `?kind=${encodeURIComponent(kind)}` : ''}`),
  createDocument: (pid, body) => send('POST', `/api/projects/${pid}/documents`, body),
  document: (id) => get(`/api/documents/${id}`),
  // 改标题与挂靠的分集。**不含路径**：改名不挪文件，历史是按路径记的
  updateDocument: (id, body) => send('PUT', `/api/documents/${id}`, body),
  setDocumentStatus: (id, status) => send('PATCH', `/api/documents/${id}/status`, { status }),
  deleteDocument: (id) => send('DELETE', `/api/documents/${id}`),
  docContent: (id) => get(`/api/documents/${id}/content`),
  // expected_version 必填：不带版本后端直接 422，前端也不给"最后一次保存覆盖前一次"的便道
  saveDocContent: (id, content, expectedVersion) => send('PUT', `/api/documents/${id}/content`, { content, expected_version: expectedVersion }),
  // 拿锁不报错也拿不到就是 held：那是界面要显示的状态，不是要弹的失败
  lockDocument: (id) => send('POST', `/api/documents/${id}/lock`),
  unlockDocument: (id) => send('DELETE', `/api/documents/${id}/lock`),
  docVersions: (id) => get(`/api/documents/${id}/versions`),
  docDiff: (id, commit) => get(`/api/documents/${id}/versions/${encodeURIComponent(commit)}/diff`),
  // 文档 AI（W11）：计价 → 提交 Job → 轮询预览 → 应用。
  // 选择文字一起带上，服务端拿它做偏移的第二重校验（emoji 会让码点与 UTF-16 差一截）。
  docAiOptions: (id, body) => get(`/api/documents/${id}/ai-options?` + new URLSearchParams({
      operation: body.operation, scope: body.scope,
      start: String(body.start ?? 0), end: String(body.end ?? 0),
      selection_text: body.selection_text || '',
    })),
  docAiSubmit: (id, body) => send('POST', `/api/documents/${id}/ai-jobs`, body),
  docAiJob: (id, jobId) => get(`/api/documents/${id}/ai-jobs/${jobId}`),
  docAiApply: (id, jobId) => send('POST', `/api/documents/${id}/ai-jobs/${jobId}/apply`),

  // 文本抽取（W13）：计价是同一条文本默认轨；候选要人确认才入资产。
  extractOptions: (pid, docId) => get(`/api/projects/${pid}/extract/options?doc_id=${docId}`),
  extractRuns: (pid) => get(`/api/projects/${pid}/extract/runs`),
  extractSubmit: (pid, body) => send('POST', `/api/projects/${pid}/extract/runs`, body),
  extractRun: (runId) => get(`/api/extract/runs/${runId}`),
  extractItems: (runId, params = {}) => get(`/api/extract/runs/${runId}/items?` + new URLSearchParams(
      Object.fromEntries(Object.entries(params).filter(([, v]) => v)))),
  extractAccept: (itemId, body = {}) => send('POST', `/api/extract/items/${itemId}/accept`, body),
  extractMerge: (itemId, assetId) => send('POST', `/api/extract/items/${itemId}/merge`, { asset_id: assetId }),
  extractReject: (itemId, reason = '') => send('POST', `/api/extract/items/${itemId}/reject`, { reason }),

  // 审核中心（W14）：校验报告按正文版本冻结，签字按锚点落
  reviewQueue: (pid) => get(`/api/projects/${pid}/reviews`),
  reviewGate: (docId) => get(`/api/documents/${docId}/review-gate`),
  runReviewGate: (docId) => send('POST', `/api/documents/${docId}/review-gate`),
  reviewSign: (docId, body) => send('POST', `/api/documents/${docId}/review-sign`, body),
  // 产物审核（0025／D1）：审的是一条已成功作业的产物，签字仍只在审核中心落
  jobReviews: (pid) => get(`/api/projects/${pid}/job-reviews`),
  jobReview: (jobId) => get(`/api/jobs/${jobId}/review`),
  jobReviewSign: (jobId, body) => send('POST', `/api/jobs/${jobId}/review-sign`, body),
  // §8.2 审核质量清单：自动项每次现算（含 ffprobe），所以只在点开某一条时读
  jobChecklist: (jobId) => get(`/api/jobs/${jobId}/review-checklist`),
  // 音频四类能力的真实状态（能不能提交、缺哪一件）：页面上的按钮按它来画，不按写死的文案
  audioCapabilities: () => get('/api/audio/capabilities'),
  saveJobChecklist: (jobId, body) => send('POST', `/api/jobs/${jobId}/review-checklist`, body),
  // 批量签字：整批同一个结论，服务端逐条落地、逐条报原因
  jobReviewBatch: (body) => send('POST', '/api/jobs/review-batch', body),
  /** 批量重投：把这一集卡在失败／已取消的镜头一次重投（服务端逐条判，界面不自己筛）。*/
  jobRetryBatch: (episodeId, capability = 'video') => send('POST', '/api/jobs/retry-batch', { episode_id: Number(episodeId), capability }),

  // 产线中心（0026／阶段 C）：只做编排。不建第二条队列、不记第二本账——
  // 这里拿到的 job_id／review_id 都是引用，成本与产物仍去生成中心读。
  // 发起运行之前的生产前检查：只读，blocking／warnings／info 三档
  preflight: (pid, episodeId, templateId) => get(`/api/projects/${pid}/preflight?` + new URLSearchParams({
      episode_id: episodeId, ...(templateId ? { template_id: templateId } : {}),
    })),
  pipelineTemplates: () => get('/api/pipeline/templates'),
  pipelineTemplate: (id) => get(`/api/pipeline/templates/${id}`),
  pipelineRuns: (pid) => get(`/api/projects/${pid}/pipeline/runs`),
  pipelineRun: (id) => get(`/api/pipeline/runs/${id}`),
  createPipelineRun: (pid, body) => send('POST', `/api/projects/${pid}/pipeline/runs`, body),
  advancePipelineRun: (id) => send('POST', `/api/pipeline/runs/${id}/advance`),
  pausePipelineRun: (id) => send('POST', `/api/pipeline/runs/${id}/pause`),
  resumePipelineRun: (id) => send('POST', `/api/pipeline/runs/${id}/resume`),
  cancelPipelineRun: (id, reason = '') => send('POST', `/api/pipeline/runs/${id}/cancel`, { reason }),
  retryPipelineNode: (id) => send('POST', `/api/pipeline/nodes/${id}/retry`),

  // 成片中心（阶段 D）：每次合成是一条普通 Job，版本只增不改
  compositions: (pid, params = {}) => get(`/api/projects/${pid}/compositions?` + new URLSearchParams(
      Object.fromEntries(Object.entries(params).filter(([, v]) => v)))),
  createComposition: (pid, body) => send('POST', `/api/projects/${pid}/compositions`, body),
  composition: (id) => get(`/api/compositions/${id}`),
  // 字幕是"喂给一次合成的输入文件"，不进库、不进 Git 工作区；
  // 列表同时带回这台机器的 FFmpeg 能不能烧字幕，界面据此决定控件是可选还是禁用
  compositionSubtitles: (pid) => get(`/api/projects/${pid}/composition-subtitles`),
  uploadCompositionSubtitle: (pid, file) => {
    const form = new FormData();
    form.append('file', file);
    // 走裸 fetch：FormData 不能被 JSON.stringify（send 是 JSON 那条路的）。
    return request(`/api/projects/${pid}/composition-subtitles`, { method: 'POST', body: form })
      .then(toJson);
  },
  deleteCompositionSubtitle: (pid, path) => send('DELETE', `/api/projects/${pid}/composition-subtitles?path=${encodeURIComponent(path)}`),

  // 整季生产（阶段 E）：一季只是若干条单集运行的排产记录
  seasons: (pid) => get(`/api/projects/${pid}/seasons`),
  season: (id) => get(`/api/seasons/${id}`),
  createSeason: (pid, body) => send('POST', `/api/projects/${pid}/seasons`, body),
  pauseSeason: (id) => send('POST', `/api/seasons/${id}/pause`, {}),
  resumeSeason: (id) => send('POST', `/api/seasons/${id}/resume`, {}),
  cancelSeason: (id, reason = '') => send('POST', `/api/seasons/${id}/cancel`, { reason }),
  retrySeasonFailed: (id) => send('POST', `/api/seasons/${id}/retry-failed`, {}),

  // 音频中心（W15）：素材仍送生成中心的同一条队列
  audioClips: (pid, params = {}) => get(`/api/projects/${pid}/audio-clips?` + new URLSearchParams(
      Object.fromEntries(Object.entries(params).filter(([, v]) => v)))),
  createAudioClip: (pid, body) => send('POST', `/api/projects/${pid}/audio-clips`, body),
  updateAudioClip: (id, body) => send('PATCH', `/api/audio-clips/${id}`, body),
  submitAudioClip: (id) => send('POST', `/api/audio-clips/${id}/submit`),
  deleteAudioClip: (id) => send('DELETE', `/api/audio-clips/${id}`),

  // 项目看板（W16）：只读聚合；导出是纯下载，不写状态
  dashboard: (pid) => get(`/api/projects/${pid}/dashboard`),

  // 首页工作台：跨模块待办聚合（审核待签、失败、待核对、进行中的运行）。
  // 只读；服务端按"我看得见的在产项目"先筛过，前端不再自己过滤一遍。
  workbench: () => get('/api/workbench'),

  // 资产：scope 决定看自有／全局池／两者都要。默认 all——镜头要引用全局资产时
  // 得先搜得到它，否则"跨项目复用"这句话在界面上无处可点
  assets: (pid, kind, scope) => get(`/api/projects/${pid}/assets?` + new URLSearchParams(
      { ...(kind ? { kind } : {}), ...(scope && scope !== 'all' ? { scope } : {}) })),
  createAsset: (pid, body) => send('POST', `/api/projects/${pid}/assets`, body),
  asset: (id) => get(`/api/assets/${id}`),
  // 整体提交：名称／类型／简介／提示词／别名／标签／范围一次改完。
  // 同一个字段不留第二个编辑入口，所以没有"只改标签"这种半截接口
  updateAsset: (id, body) => send('PUT', `/api/assets/${id}`, body),
  setAssetCover: (id, path) => send('PUT', `/api/assets/${id}/cover`, { path }),
  assetShots: (id) => get(`/api/assets/${id}/shots`),
  deleteAsset: (id) => send('DELETE', `/api/assets/${id}`),
  uploadAssetImage: (id, file) => {
    const form = new FormData();
    form.append('file', file);
    // 同上：multipart 不走 send。
    return request(`/api/assets/${id}/images`, { method: 'POST', body: form }).then(toJson);
  },
  deleteAssetImage: (id, path) => send('DELETE', `/api/assets/${id}/images?path=${encodeURIComponent(path)}`),

  // 资产增强（0033）：人物档案、命名形态、生成与产物导入。
  // 视图里不散落裸 fetch——同一个接口的错误处理要只有一份。
  assetProfile: (id) => get(`/api/assets/${id}/character-profile`),
  saveAssetProfile: (id, body) => send('PUT', `/api/assets/${id}/character-profile`, body),
  assetVariants: (id) => get(`/api/assets/${id}/variants`),
  createAssetVariant: (id, body) => send('POST', `/api/assets/${id}/variants`, body),
  updateAssetVariant: (vid, body) => send('PUT', `/api/asset-variants/${vid}`, body),
  // 204 没有响应体，toJson 已经认这个情况（回 null）
  deleteAssetVariant: (vid) => send('DELETE', `/api/asset-variants/${vid}`),
  // 摘掉形态名下的**一张**图（v0.99）。以前只有"删整个形态"这一个出口，
  // 导错一张就得把其余的图一起扔了——基础参考图有这条删除路，形态也必须要有。
  deleteAssetVariantImage: (vid, path) => send('DELETE', `/api/asset-variants/${vid}/images?path=${encodeURIComponent(path)}`),
  assetGenerationSubmit: (pid, body) => send('POST', `/api/projects/${pid}/asset-generations`, body),
  assetGeneration: (pid, jobId) => get(`/api/projects/${pid}/asset-generations/${jobId}`),
  // 刷新后把"我刚生成的那批"读回来：状态在 job 表里，不在浏览器里攒副本
  //
  // 这里**不能再挂 .then(toJson)**（`:389-391` 原来就是这么写的）：get() 已经是
  // `fetch().then(toJson)`，解析好的数组会被当成 Response 再喂一次 toJson，
  // 它第一句就是 `res.text()`——对数组调是 TypeError，于是这两处「最近的生成记录」
  // 恒为空，而两侧都是 `catch { gens = [] }`，错误被演成"还没有记录"。
  // 需要读响应头（X-Total-Count 之类）的接口不走 get/send，自己裸 fetch；
  // 现在只有 jobs（列表面板）和 providerValidations（验证历史）这两处。
  assetGenerations: (pid, assetId) => get(`/api/projects/${pid}/asset-generations?`
    + new URLSearchParams({ ...(assetId ? { target_asset_id: assetId } : {}), limit: 12 })),
  assetImportImage: (id, body) => send('POST', `/api/assets/${id}/generated-images`, body),

  /* ---------- 项目生产工作台（0037） ---------- */
  // 上下文在服务端，不在 localStorage：顶栏、工作台、预检、成片四入口必须认同一个集
  context: () => get('/api/context'),
  setContext: (projectId, episodeId) => send('PUT', '/api/context', { project_id: projectId ?? null, episode_id: episodeId ?? null }),
  production: (pid, episodeId) => get(`/api/projects/${pid}/production?`
    + new URLSearchParams({ ...(episodeId ? { episode_id: episodeId } : {}) })),
  shotWorkbench: (sid) => get(`/api/shots/${sid}/workbench`),
  assetImpact: (id) => get(`/api/assets/${id}/impact`),
  precheckShot: (sid, params = {}) => get(`/api/precheck/shot/${sid}?` + new URLSearchParams(params)),
  precheckEpisode: (pid, episodeId, templateId) => get(`/api/projects/${pid}/precheck?` + new URLSearchParams({
      episode_id: episodeId, ...(templateId ? { template_id: templateId } : {}),
    })),
  compositionPrecheck: (pid, episodeId, subtitlePath) => get(`/api/projects/${pid}/composition-precheck?` + new URLSearchParams({
      episode_id: episodeId, ...(subtitlePath ? { subtitle_path: subtitlePath } : {}),
    })),
};
