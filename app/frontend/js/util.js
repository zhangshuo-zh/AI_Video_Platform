/** 小工具：转义、接口错误的统一出口、错误态卡片、危险确认弹窗、弹窗 Escape。 */
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
    // 401 不在这里弹 alert：会话过期是"要去重新登录"，不是"操作失败"。
    // 闸门由 api.js 的统一出口挂出来（app.js 接 app:unauthorized），
    // 这里只把"没做成"告诉调用方，别让人对着一个过期会话反复点确认。
    if (err && err.status === 401) return false;
    alert(`${label}失败：${err.message}`);
    return false;
  }
}

/**
 * 金额：后端一律给整数 micros（1e-6 货币单位），界面只负责显示。
 *
 * 不做前端汇总——成本账的真相在服务端；这里多留 4 位小数，
 * 是因为云端按秒计价时一笔可能就是 ¥0.0036，四舍五入到 2 位会显示成 ¥0.00。
 */
const CURRENCY_SIGNS = { CNY: '¥', USD: '$', EUR: '€' };

export function formatMoney(micros, currency) {
  if (micros == null) return '—';
  const sign = CURRENCY_SIGNS[currency] || (currency ? `${currency} ` : '');
  const amount = Math.abs(Number(micros)) / 1_000_000;
  const digits = amount > 0 && amount < 0.01 ? 4 : 2;
  return sign + amount.toLocaleString('zh-CN', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  });
}

/** 403/404 补一句"为什么"，不让人对着一个状态码猜。 */
function whyHint(err) {
  if (!err || !err.status) return '';
  if (err.status === 403) return ' —— 你的角色没有这项权限，找管理员加。';
  if (err.status === 404) return ' —— 这个项目或对象不在你的可见范围内。';
  return '';
}

/**
 * 错误态卡片。**空态和错误态必须是两种东西**：
 * 原来几处 `catch { list = [] }` 把 403/500 演成"还没有数据"，
 * 人第一反应是数据丢了，而不是"这一页没读到"。
 */
export function errorBox(what, err, retry = true) {
  const msg = err && err.message ? err.message : '未知错误';
  const code = err && err.status ? `（${err.status}）` : '';
  return `<div class="empty err" style="padding:26px 20px">
    <div class="empty-t">读不到${escapeHtml(what)}</div>
    <p>${escapeHtml(msg)}${escapeHtml(code)}${escapeHtml(whyHint(err))}</p>
    <p><button class="btn"${retry ? ' data-retry' : ' disabled'}>重试</button></p>
  </div>`;
}

/** 页面里"这一栏读不到"的一行版（列表内嵌用，不占一整屏） */
export function errorLine(what, err) {
  return `<p class="t-bad" style="margin:0 0 8px">${escapeHtml(what)}读不到：
    ${escapeHtml(err && err.message ? err.message : '未知错误')}${escapeHtml(whyHint(err))}
    <button class="btn xs" data-retry>重试</button></p>`;
}

let dangerHost = null;
let dangerResolve = null;

/**
 * 危险操作确认：全站一套自绘弹窗，替掉原生 `confirm()`。
 *
 * 原生 confirm 有两个问题：样式跟两套主题脱节，而且它把"确定/取消"
 * 说得一样轻——删一集和"知道了"用同一张脸。返回 Promise，
 * 调用方写成 `if (!(await confirmDanger({...}))) return;`，读起来和原来一样。
 * Escape 与点遮罩都等于取消：危险操作不该留"手滑确认"的路径。
 */
export function confirmDanger({ title = '确认操作', body = '', okText = '确定',
                                danger = true } = {}) {
  return new Promise((resolve) => {
    if (!dangerHost) {
      dangerHost = document.createElement('div');
      dangerHost.className = 'modal';
      dangerHost.innerHTML = `
        <div class="modal-box">
          <div class="modal-head" data-d-title></div>
          <div class="modal-body"><p data-d-body></p></div>
          <div class="modal-actions">
            <button class="btn" data-d-cancel>取消</button>
            <button class="btn" data-d-ok></button>
          </div>
        </div>`;
      document.body.appendChild(dangerHost);
      const done = (v) => { const r = dangerResolve; hideDanger(); if (r) r(v); };
      dangerHost.addEventListener('click', (e) => {
        if (e.target === dangerHost || e.target.closest('[data-d-cancel]')) done(false);
        else if (e.target.closest('[data-d-ok]')) done(true);
      });
      dangerHost.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') done(false);
      });
      // 也登记进全局 Escape 表：确认钮虽然抢了焦点，但人可能 Tab 到取消钮，
      // 那时 keydown 仍在这个弹窗里——两条路都指向同一个 done(false)
      bindModalDismiss(dangerHost, () => done(false));
    }
    dangerResolve = resolve;
    dangerHost.querySelector('[data-d-title]').textContent = title;
    dangerHost.querySelector('[data-d-body]').innerHTML = body;   // 调用方负责转义
    const okBtn = dangerHost.querySelector('[data-d-ok]');
    okBtn.textContent = okText;
    okBtn.classList.toggle('danger-solid', danger);
    okBtn.classList.toggle('primary', !danger);
    dangerHost.hidden = false;
    okBtn.focus();
  });
}

function hideDanger() {
  if (!dangerHost) return;
  dangerHost.hidden = true;
  dangerResolve = null;
}

/**
 * 二级页的页内切换器。窄屏（≤860px）侧栏的二级标签会收起，
 * 那时页内没有第二个入口就等于把功能藏了（指令 §3.2-1「不得隐藏关键入口」）。
 * 所以凡是有 subs 的模块都该在抬头摆一排：侧栏和这里读的是同一个 navstate 下标。
 */
export function subChipRow(labels, current) {
  // sub-chip 这个类只管宽窄屏的显隐（见 app.css 里的 .sub-chip）：
  // 861px 起侧栏二级导航是展开的，页内再摆一排就是同一份状态的两个控件。
  return labels.map((l, i) => `
    <button type="button" class="chip sub-chip ${i === current ? 'blue' : 'plain'}"
            data-sub="${i}">${escapeHtml(l)}</button>`).join('');
}

export function bindSubChips(root, onPick) {
  root.querySelectorAll('[data-sub]').forEach((el) => {
    el.addEventListener('click', () => onPick(Number(el.dataset.sub)));
  });
}

/**
 * 给弹窗补 Escape 关闭。九个弹窗原来只有五个能键盘关，
 * 差的那四个恰好都是"打开后想撤回来"最典型的场景（成员、改角色、改密、删号）。
 *
 * 监听挂在 document 上而不是弹窗里：绑在元素上的话，**焦点必须在弹窗内**才收得到
 * keydown——成员弹窗打开后没有聚焦任何控件，Escape 就永远不生效（第一版就是这么写的，
 * 真浏览器里当场暴露）。这里按"当前 DOM 里可见的最后一个 .modal"来关，
 * 一次只关最上面那层，也不依赖谁持有焦点。
 *
 * 用 WeakMap 存关闭函数：视图每次重渲染都会换新元素，按元素键自然回收，
 * 不会攒出一堆指向已销毁节点的监听。
 */
const closers = new WeakMap();
let escBound = false;

export function bindModalDismiss(modal, close) {
  closers.set(modal, close);
  if (escBound) return;
  escBound = true;
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const open = [...document.querySelectorAll('.modal:not([hidden])')];
    if (!open.length) return;
    const fn = closers.get(open[open.length - 1]);
    if (fn) { e.preventDefault(); fn(); }
  }, true);
}

/**
 * 遮罩点击关闭 ＋ Escape 关闭，一次绑好。
 *
 * 过去每个弹窗各写一句 `addEventListener('click', (e) => { if (e.target === m) close(); })`，
 * 21 处逐字重复，且只有一半顺手绑了 Escape——同一个弹窗用鼠标点外面能关、
 * 按 Esc 却关不掉。新代码一律用这一个；旧的逐处替换可以慢慢来，
 * 因为少绑一次不会改变既有行为，只是少一条重复。
 */
export function bindOverlayDismiss(modal, close) {
  if (!modal) return;
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  bindModalDismiss(modal, close);
}

/**
 * 空态卡片：标题 + 一段说明。`body` 是 HTML（调用方负责转义与按钮）。
 *
 * 过去几十处各写一遍 `<div class="empty"><div class="empty-t">…</div><p>…</p></div>`，
 * 改一次结构要动十几个文件。新代码一律用它；老调用点可以逐处替换——
 * 输出与原结构逐字一致，替换不改变 DOM，只是少抄一层 div。
 */
export function emptyBox(title, body = '') {
  return `<div class="empty"><div class="empty-t">${title}</div>${body}</div>`;
}

/**
 * 「多久前」的唯一写法：按 天／小时／分钟／秒 给一级读得出来的量级。
 *
 * 原来有两份：`views/models.js` 直接拼 `${age_seconds}s 前`，
 * `views/pipeline.js` 自己写了个只到小时的 agoText。
 * 于是同一张 worker 表在两个页面一个念「46045s 前」一个念「12 小时前」，
 * 而 46045 这种数还得人自己心算——心跳新旧是判断"要不要重启 worker"的依据，
 * 不该让人做除法。
 */
export function agoText(seconds) {
  const n = Number(seconds);
  if (seconds == null || Number.isNaN(n) || n < 0) return '—';
  if (n < 60) return `${n} 秒前`;
  if (n < 3600) return `${Math.floor(n / 60)} 分钟前`;
  if (n < 86400) return `${Math.floor(n / 3600)} 小时前`;
  return `${Math.floor(n / 86400)} 天前`;
}
