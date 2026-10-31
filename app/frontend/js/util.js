/** 小工具：拼接进 innerHTML 的用户输入一律先转义。 */
export function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

/**
 * 调接口的统一出口：失败弹一句人话并返回 false，成功返回 true。
 *
 * 后端给的 detail 本来就是给人看的，所以直接拼进去，不另编一套文案。
 * 刷新不包进来——有的流程要先 store.load() 再刷新，硬塞进来反而别扭。
 * 需要把错误显示在页面里的地方（例如弹窗内联提示）不要用这个，继续自己 catch。
 */
export async function run(label, fn) {
  try {
    await fn();
    return true;
  } catch (err) {
    alert(`${label}失败：${err.message}`);
    return false;
  }
}
