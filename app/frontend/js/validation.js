/**
 * 「同一个东西在界面上只有一种说法」的唯一出口：验证出处、健康、作业状态。
 *
 * 为什么要单独一个文件：系统设置页（配置的那位）与资产创作页（用槽位干活的这位）
 * 都要给同一枚 `validation_state` 起名字。两边各写一份字典的结果是——
 * 配置页说"真实调用已验证"、创作页说"跑过一次"，而人看到的**是同一个徽章**；
 * 口径事故就出在这种"同一个东西两个说法"上（P0 那轮外部评审点名的正是它）。
 * 同一批道理后来也适用于作业状态（见下面的 JOB_STATUS）：它原来在五个视图文件里
 * 各写了一遍，所以这个文件的名字按"说法的出口"理解，不按字面的"验证"理解。
 *
 * 可信度从高到低：目标机 > 产物验收 > 真实调用 > 模拟 > 环境检查 > 从未。
 * 环境检查通过永远不能冒充真实调用。
 */
export const VAL_BADGE = {
  target_machine: ['green', '目标机已验证'],
  artifact_review: ['green', '产物验收通过'],
  real_call: ['green', '真实调用已验证'],
  mock: ['amber', '仅模拟验证'],
  env_check: ['blue', '仅环境检查'],
  never: ['gray', '未验证'],
};

/**
 * 健康状态的中文译名，同样只留一份。
 *
 * 原来生成中心与模型中心各写了一份 `HEALTH`：配置页把 ok 叫"就绪"、
 * 队列页哪天改叫"可用"，同一个枚举就在两屏上成了两个意思。
 * `HEALTH` 带配色（列表页的徽章用），`HEALTH_LABEL` 是纯文案（行内小字用）。
 *
 * unknown 用琥珀而不是红色——"平台查不了"不等于"坏了"。
 */
export const HEALTH = {
  ok: ['green', '就绪'],
  missing: ['red', '缺东西'],
  unknown: ['amber', '未检查'],
  unconfigured: ['gray', '未配置'],
  unregistered: ['red', '未登记'],
};
export const HEALTH_LABEL = Object.fromEntries(
  Object.entries(HEALTH).map(([k, [, label]]) => [k, label]));

/** 一句括号里的短语，给"选模型"那种地方用；取不到的状态原样回状态码。 */
export function validationText(state) {
  const badge = VAL_BADGE[state || 'never'];
  return badge ? badge[1] : `验证状态 ${state}`;
}

/**
 * 作业（job）状态的配色与兜底文案，同样只留一份。
 *
 * 原来这一份在五个文件里各写了一遍（生成中心、创作中心的镜头列、资产创作页、
 * 资产详情、产线页），其中前两份**逐字相同**。重复的代价不是行数，是"改一处漏四处"：
 * 后端加一个终态，四屏里有三屏还把它染成蓝色当"还在跑"。
 *
 * 文案优先用后端随每行下发的 `status_label`（那才是权威说法），这张表里的 label
 * 只在响应没带 label 时兜底；**配色必须在这儿定一次**——色板是界面的事，后端不管。
 * 取值集合由 `tests/test_infra.py::test_前端作业状态表和后端枚举对齐` 钉住。
 */
export const JOB_STATUS = {
  queued: ['gray', '排队中'],
  starting: ['blue', '启动中'],
  running: ['blue', '运行中'],
  succeeded: ['green', '成功'],
  failed: ['red', '失败'],
  cancel_requested: ['amber', '取消中'],
  cancelled: ['gray', '已取消'],
  needs_attention: ['amber', '待核对'],
};

/** 还在飞的作业：能取消、该继续轮询、按钮要按住。＝ queued + 后端的 JOB_RUNNING。 */
export const JOB_ACTIVE = ['queued', 'starting', 'running', 'cancel_requested'];

/** 已经停下来的作业：不再轮询。取值照抄后端的 JOB_FINISHED。 */
export const JOB_TERMINAL = ['succeeded', 'failed', 'cancelled', 'needs_attention'];

/** 停下来且结果不干净的——列表里用来算"这一批有没有要处理的"。 */
export const JOB_FINISHED_BAD = ['failed', 'cancelled', 'needs_attention'];

/** 配色：认不出来的状态一律 gray，绝不猜成 blue（猜成蓝就是在说"它还在跑"）。 */
export function jobTone(status) {
  const hit = JOB_STATUS[status];
  return hit ? hit[0] : 'gray';
}

/** 文案：后端给的 label 优先，没有就用这张表，再没有就原样念状态码。 */
export function jobLabel(status, label) {
  return label || (JOB_STATUS[status] || [null, ''])[1] || String(status || '');
}

/**
 * 队列告警的中文短标签。**英文 code 只用于样式分档，不往界面上端。**
 *
 * 这一份原来在三处各写一遍：生成中心叫 `ALERT_CODE`、首页叫 `ALERT_LABEL`
 * （还自带一句"与生成中心同一套口径"的注释）、模型中心的运行监控则干脆没写，
 * 于是同一句告警在生成中心是「待核对」、在模型中心是 `needs_attention`。
 *
 * 取值本身就是后端 `services.queue_health()` 里 `{"code": ...}` 那一串，
 * 由 `tests/test_infra.py::test_告警标签和后端告警码对齐` 钉住。
 */
export const ALERT_LABEL = {
  worker_offline: 'worker 离线',
  queue_stalled: '队列停滞',
  lease_expired: '租约过期',
  run_timeout: '运行超时',
  log_silent: '日志静默',
  needs_attention: '待核对',
};

/** 告警那句中文；认不出来的 code 原样回（不会变成空白）。 */
export function alertLabel(code) {
  return ALERT_LABEL[code] || String(code || '');
}
