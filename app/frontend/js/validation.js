/**
 * 「这一槽位凭什么算已验证」的唯一出口。
 *
 * 为什么要单独一个文件：系统设置页（配置的那位）与资产创作页（用槽位干活的这位）
 * 都要给同一枚 `validation_state` 起名字。两边各写一份字典的结果是——
 * 配置页说"真实调用已验证"、创作页说"跑过一次"，而人看到的**是同一个徽章**；
 * 口径事故就出在这种"同一个东西两个说法"上（P0 那轮外部评审点名的正是它）。
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
 * 这一槽位**能不能提交**：只有健康 `ok` 才算能。
 * 验证状态不参与这道判断——"环境齐但从没真跑过"是可用的，只是不能吹成
 * "生产已验证"。两件事混成一条，人就会以为平台在替他担保。
 */
export function submittable(option) {
  return !!option && option.health_state === 'ok';
}
