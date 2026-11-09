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

/** 验证出处的短名。**权威译名在后端 `VALIDATION_KINDS`**（models.py），
 * 且随每条记录下发 `kind_label`——逐行渲染必须优先读那个字段。
 * 这一份只给"没有行可读"的场合兜底（表头、按 kind 聚合的小结）。
 * mock 在这里与后端同念「假适配器」：模型中心曾把同一档念成两个名字
 * （这边「模拟」、那边「假适配器」），后端没有行的时候界面上也不许分叉。 */
export const VAL_KIND_LABEL = {
  env_check: '环境检查', mock: '假适配器', real_call: '真实调用',
  target_machine: '目标机基准', artifact_review: '产物验收',
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
  // 「未登记」这个词原来是健康与模型状态共用的：健康说"清单没这个 provider"，
  // 模型状态说"本机没登记这一档"——一个往回改配置、一个往前点登记，
  // 同一个词指向相反的动作。健康这一档补上主语：缺的是清单里的这一家。
  unregistered: ['red', '清单无此 provider'],
};
export const HEALTH_LABEL = Object.fromEntries(
  Object.entries(HEALTH).map(([k, [, label]]) => [k, label]));

/** 作业状态的对外五档（任务书 §12.3）。
 *
 * 为什么要多这一层：制作人员真正要回答的是"这件事现在要我管吗"，
 * 而 `queued`／`starting`／`running` 三个词在界面上是三种颜色、一句话。
 * 深层详情仍然显示 `jobLabel` 的原词——**数据库与后端状态语义一个字都没合并**，
 * 这层只用于展示分组与筛选。
 */
export const PUBLIC_STATUS = {
  待处理: ['queued'],
  进行中: ['starting', 'running', 'cancel_requested'],
  需要处理: ['needs_attention'],
  // 「完成」而不是「已完成」：同列的「失败」是两个字，三个字的 chip 会比它宽
  // 一档，扫一列时参差不齐。五档里另外四个都是 2–4 字，这一档取最短的准确说法。
  完成: ['succeeded'],
  失败: ['failed', 'cancelled'],
};

const PUBLIC_OF = {};
Object.entries(PUBLIC_STATUS).forEach(([word, list]) => list.forEach((s) => { PUBLIC_OF[s] = word; }));

/** 后端状态 → 对外五档；认不出来回「需要处理」，不假装它没事。 */
export function publicStatus(status) {
  return PUBLIC_OF[status] || '需要处理';
}

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
  // 0047：并发上限与本机内存的关系。bad＝估算会撑破，warn＝余量偏薄。
  concurrency_over_memory: '并发超内存',
  concurrency_tight_memory: '并发余量薄',
};

/** 告警那句中文；认不出来的 code 原样回（不会变成空白）。 */
export function alertLabel(code) {
  return ALERT_LABEL[code] || String(code || '');
}

/** 签字结论的中文名与色调：审核中心与工作台统计共用这一份。
 *
 *  这三行原来住在 `views/review.js` 里。抽出来的理由与 `publicStatus` 一样——
 *  第二处想要「已通过」这三个字的时候只能自己再写一遍，而从那一刻起
 *  两处就可能一处说「通过」、另一处说「已通过」。
 */
export const DECISION_LABEL = { approved: '已通过', returned: '退回', rejected: '驳回' };
export const DECISION_TONE = { approved: 'green', returned: 'amber', rejected: 'red' };
