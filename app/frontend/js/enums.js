/**
 * 后端枚举在界面上的**唯一一份**中文名。
 *
 * 为什么又开一个文件：`validation.js` 管的是"同一个徽章只有一种说法"，
 * 这一份管的是"同一类取值只有一种叫法"——能力、运行位置、计费单位。
 * 它们的权威在 `app/backend/models.py`（`CAPABILITIES` / `BACKENDS` / `UNITS`），
 * 那边已经写明理由：**单位不做自由文本，因为它是给人对账的**。
 *
 * 原来这三张表在界面上被抄了四遍，而且抄出了真事故：
 * - `writing.js` 把 `character` 写成「千字」，后端 `UNITS` 是「字符」；
 * - `models.js` 把 `million_token` 写成「百万 token」，后端是「百万 Token」。
 * 同一个价格卡在两屏上是两个单位说法，这正是"秒／sec／seconds 混着存"
 * 那一类问题在界面侧的翻版。
 *
 * 对齐由 `tests/test_infra.py::test_前端枚举表和后端枚举对齐` 钉住：
 * 有人在这里少一条或多一条，或者改了一个字，那条测试当场红。
 */

/** 五类能力。顺序就是界面渲染顺序，别在这里重排。 */
export const CAPS = { text: '文本', image: '图像', video: '视频', tts: '配音', music: '音乐' };

/** 选型层（provider_config 那一行）的运行位置。登记层多一档"远程自建"，见下。 */
export const MODES = { local: '本地', cloud: '云端' };

/**
 * 登记层（model_registration.source／backend）的取值。
 * 优先用接口下发的 `catalog.reg_sources`，这份只在"清单还没回来"时兜底。
 */
export const REG_SOURCES = { local: '本地', cloud: '云端', remote: '远程自建' };

/** 计费单位。这一张表直接影响对账，改字之前先改后端 models.UNITS。 */
export const UNITS = {
  second: '秒',
  minute: '分钟',
  request: '次',
  token: 'Token',
  million_token: '百万 Token',
  image: '张',
  character: '字符',
};

/** 取单位中文名：认不出来就原样念 code，绝不猜一个近义单位。 */
export function unitLabel(unit) {
  return UNITS[unit] || String(unit || '单位');
}
