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

/* 焦点陷阱（无障碍）：弹窗开着时 Tab 不许跑到背后那一屏去。
 *
 * 原来只有 Escape 一条键盘路径——鼠标用户察觉不到，键盘与读屏用户会直接
 * 撞到"Tab 到底下表格里去了，回不来"。这里补的是最小可用的一套：
 *   ① 打开时把焦点移进弹窗（第一个可聚焦元素，没有就聚焦容器本身）；
 *   ② Tab／Shift+Tab 在弹窗内的可聚焦元素之间循环；
 *   ③ 关闭时把焦点还给打开它的那个元素（`document.activeElement` 快照）。
 *
 * 不引库、不改 DOM 结构：`.modal` 加 `role="dialog" aria-modal="true"` 由各
 * 视图自己写在模板里（已经有一半这么写了），这里只管键盘与焦点。
 */
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]),'
  + ' select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
const lastFocus = new WeakMap();   // modal → 打开前持有焦点的元素

function focusables(modal) {
  return [...modal.querySelectorAll(FOCUSABLE)]
    .filter((el) => el.offsetParent !== null || el === document.activeElement);
}

function trapTab(modal) {
  const list = focusables(modal);
  if (!list.length) return;
  const first = list[0];
  const last = list[list.length - 1];
  const active = document.activeElement;
  if (!modal.contains(active)) { first.focus(); return; }
  if (active === last) { last.blur(); first.focus(); }
  else if (active === first) { first.blur(); last.focus(); }
}


/**
 * 关弹窗：先播退场动画，150ms 后才真正 hidden。
 *
 * 为什么要有这一个：全库 126 处 `modal.hidden = true`，逐处改成动画不现实，
 * 而关闭瞬间消失与打开那两层动画不对称。这里把"退场"收成一个函数，
 * 新代码用 closeModal(m)，旧代码保持写 hidden = true 也照常有动画——
 * 见下面 bindModalDismiss 里那个 MutationObserver：它监听 hidden 变 true，
 * 如果元素还没加 .closing，就先撤回 hidden、补播退场再提交。
 *
 * 两个边界：
 *   - 已经 hidden 的不重复播（重复点"取消"不该闪两下）。
 *   - 退场期间 pointer-events 关掉（CSS 里给 .closing 设了 none），
 *     免得人在 150ms 里又点开一次，落进"正在关"的弹窗。
 */
const closingTimers = new WeakMap();

/* 退场进行中的弹窗。观察器看到它在里面就放行最终那次 hidden 提交——
   否则会把"退场完成"误判成"又一次直接置 hidden"，撤回再补播，永远关不掉。
   用 WeakSet 而不是 dataset：dataset 会残留在节点上，第二次关闭时被误读。 */
const settling = new WeakSet();
/* 每个弹窗一个递增代号：关闭时记下当代号，定时器到点只认这一代。
   打开（hidden=false）会 bump 代号，于是迟到的关闭提交自动作废。
   用代号而不是「取消定时器」：打开那条路径可能根本不经观察器
   （我们的验收脚本就在同一同步块里连着 hidden=true、hidden=false，
   观察器回调要等微任务，来不及取消）。 */

export function closeModal(modal) {
  if (!modal || modal.hidden || modal.classList.contains('closing')) return;
  modal.classList.add('closing');
  clearClosingTimer(modal);
  closingTimers.set(modal, setTimeout(() => {
    closingTimers.delete(modal);
    // 只保留一条判据：还在退场状态里才提交。
    // 原来还有 epoch 与 modal.hidden 两条，实测会把正常的关闭也拦掉
    // （hidden 在退场期间被别处改过就永远关不上），去掉它们更稳。
    if (!modal.classList.contains('closing')) return;
    modal.classList.remove('closing');
    // **不能在这里立刻 delete**：MutationObserver 是异步的，回调跑到时
    // WeakSet 已经空了，于是这次最终提交又被当成"直接关"、撤回再补播，
    // 弹窗永远关不掉（实测：Vc 挂上了但 280ms 后仍是 V）。
    // 改成加进去、由观察器在消费时删除。
    settling.add(modal);
    modal.hidden = true;
  }, 150));
}

/** 取消一次待执行的关闭。
 *
 * 为什么必须有：退场是"150ms 后才提交 hidden"，而人在这个窗口里完全可能
 * 又把弹窗打开（我们自己的验收脚本就连续设 hidden=true 再 =false）。
 * 不取消的话，那次迟到的提交会把刚打开的弹窗关掉——
 * 实测就是这么坏的：设 hidden=false 之后 500ms 再读，hidden 仍是 true。
 */
export function cancelModalClose(modal) {
  if (!modal) return;
  clearClosingTimer(modal);
  modal.classList.remove('closing');
}

function clearClosingTimer(modal) {
  const t = closingTimers.get(modal);
  if (t) { clearTimeout(t); closingTimers.delete(modal); }
}

export function bindModalDismiss(modal, close) {
  closers.set(modal, close);
  // 打开时记下"焦点原来在哪"，关的时候还回去；并主动把焦点移进弹窗。
  // 用 hidden 属性判可见性：所有弹窗都是 `hidden` 切换，没有其它显隐机制。
  if (!modal.dataset.focusBound) {
    modal.dataset.focusBound = '1';
    // 观察器里自己也会改 hidden（撤回 -> 补播退场 -> 150ms 后提交），
    // 那些被动变化不该再走一遍拦截，否则会自触发成死循环。
    // 用一个"正在自改"的标记把这段时间圈出去，比靠 dataset 置位可靠：
    // dataset 那版实测坏过——第二次回调看到 closedByAnim 已置 1，
    // 就跳过拦截走正常分支，结果弹窗反而留在了打开状态。
    const observer = new MutationObserver(() => {
      if (modal.hidden) {
        settling.delete(modal);
        const back = lastFocus.get(modal);
        lastFocus.delete(modal);
        if (back && back.isConnected) back.focus();
      } else if (!lastFocus.has(modal)) {
        lastFocus.set(modal, document.activeElement);
        const target = focusables(modal)[0] || modal;
        // 容器本身要可聚焦才接得住焦点：补 tabindex，不然 focus() 是空操作
        if (target === modal && !modal.hasAttribute('tabindex')) {
          modal.setAttribute('tabindex', '-1');
        }
        target.focus();
      }
    });
    observer.observe(modal, { attributes: true, attributeFilter: ['hidden'] });
  }
  if (escBound) return;
  escBound = true;
  document.addEventListener('keydown', (e) => {
    const open = [...document.querySelectorAll('.modal:not([hidden])')];
    if (!open.length) return;
    const top = open[open.length - 1];
    if (e.key === 'Escape') {
      const fn = closers.get(top);
      if (fn) { e.preventDefault(); fn(); }
      return;
    }
    if (e.key === 'Tab') trapTab(top);
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
  // 关闭统一走 closeModal：它会先播 150ms 退场再提交 hidden。
  // （观察器那条"拦截所有 hidden=true"的路子试过，会在
  //  打开/关闭交替时自触发，最终把刚打开的弹窗关掉；退回成
  //  "谁想有动画就调 closeModal"，旧调用点保持原样、没有动画也不出错。）
  const closeAnimated = () => closeModal(modal);
  modal.addEventListener('click', (e) => { if (e.target === modal) closeAnimated(); });
  bindModalDismiss(modal, closeAnimated);
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

/**
 * 列表行的进入／离开过渡。
 *
 * 用在哪：一屏列表被整块重绘（生成中心刷作业、审核队列增减、运行监控翻页），
 * 新行直接出现、旧行直接消失，人眼很难察觉"刚才什么变了"。
 *
 * 做法：重绘前后各记一次"有哪些行"（用 data 键），重绘后给新增的行挂
 * `.row-enter`、给消失的行先留一个占位再挂 `.row-leave`。**整批同时播**，
 * 不做逐条错峰——CSS 那边写了理由：错峰在慢机器上像多米诺。
 *
 * 两个刻意的边界：
 *   - 只认**同一个容器内**的变化。换页、换项目时整张表都算"新"的，
 *     那属于整页过渡，不该在这里逐行播。
 *   - 离开的行只停留一个动画时长就移除。留久了下一次 diff 会把它们
 *     当成"还在"，过渡就对不上了。
 *
 * @param {Element} tbody   表格体
 * @param {() => void} redraw  执行重绘（内部会改写 tbody 的 innerHTML）
 * @param {string} keyAttr  用来认行的 data 属性名，默认 data-key
 */
export function animateRows(tbody, redraw, keyAttr = 'data-key') {
  if (!tbody) { redraw(); return; }
  const keyOf = (tr) => tr.getAttribute(keyAttr) || tr.getAttribute('data-job')
    || tr.getAttribute('data-id') || tr.textContent.slice(0, 40);
  const before = new Map([...tbody.querySelectorAll('tr')].map((tr) => [keyOf(tr), tr]));

  redraw();

  const after = [...tbody.querySelectorAll('tr')];
  const afterKeys = new Set(after.map(keyOf));
  after.forEach((tr) => {
    if (!before.has(keyOf(tr))) tr.classList.add('row-enter');
  });

  // 消失的行：插回原位置播一次淡出。找不到原位置就跳过——
  // 宁可没有动画，也不要把行插错地方。
  const gone = [...before.entries()].filter(([k]) => !afterKeys.has(k));
  gone.forEach(([k, tr]) => {
    const clone = tr.cloneNode(true);
    clone.classList.add('row-leave');
    clone.removeAttribute('id');
    const nextIdx = [...before.keys()].indexOf(k);
    const anchorKey = [...before.keys()].slice(nextIdx + 1).find((x) => afterKeys.has(x));
    const anchor = anchorKey
      ? after.find((t) => keyOf(t) === anchorKey) : null;
    tbody.insertBefore(clone, anchor || null);
    setTimeout(() => clone.remove(), 200);
  });

  // 动画类只活一轮：不清掉的话下一次重绘会把它当成"本来就有"
  setTimeout(() => {
    tbody.querySelectorAll('.row-enter').forEach((tr) => tr.classList.remove('row-enter'));
  }, 200);
}

/**
 * 保存成功的回执脉冲：给按钮播一次光晕，200ms 后自动摘类。
 *
 * 为什么不用 toast：这一屏的保存是高频动作，每次都弹一条浮层会烦。
 * 按钮自己亮一下、说的正是"这次点它成了"。
 */
export function pulseSaved(btn) {
  if (!btn) return;
  btn.classList.remove('save-ok');
  // 强制一次样式计算，否则连续两次保存时动画不会重播
  void btn.offsetWidth;
  btn.classList.add('save-ok');
  setTimeout(() => btn.classList.remove('save-ok'), 260);
}

/**
 * 排序位移：让"行换位置"这件事看得见。
 *
 * 背景：模型仓库、项目中心都能换排序。点「最近打开 → 项目名称」时，
 * 行会瞬间跳到新位置，人只知道"变了"，说不出"哪几行去哪了"。
 *
 * 用 FLIP 的简化版：排序前后各量一次每一行的 top，重排后对**位置变了**的行
 * 先反向位移回去、再在下一帧归位，200ms 内插值完成。没变的行一个都不动——
 * 只动该动的，是这类过渡最省也最准的做法。
 *
 * 没用 View Transitions API：它要求每个 <tr> 有唯一的 view-transition-name，
 * 一张几十行的表就是几十个命名快照，切换时反而更慢；而且行内还有 :hover
 * 与选中态，快照会把这些状态也拍进去。FLIP 只动 transform，代价可控。
 *
 * @param {(rows: HTMLElement[]) => void} applySort  真正执行排序的函数
 * @param {HTMLElement} tbody  行所在的表格体
 */
export function flipSort(applySort, tbody) {
  if (!tbody) { applySort(); return; }
  const rows = [...tbody.querySelectorAll('tr')];
  // 排序前的位置：只对有 data 键的行记录（表里可能有分组行、空态行）
  const before = new Map();
  rows.forEach((tr) => {
    const k = tr.getAttribute('data-key') || tr.getAttribute('data-id') || tr.getAttribute('data-job');
    if (k) before.set(k, tr.getBoundingClientRect().top);
  });
  if (!before.size) { applySort(); return; }

  applySort();

  const moved = [];
  [...tbody.querySelectorAll('tr')].forEach((tr) => {
    const k = tr.getAttribute('data-key') || tr.getAttribute('data-id') || tr.getAttribute('data-job');
    if (!k || !before.has(k)) return;
    const delta = before.get(k) - tr.getBoundingClientRect().top;
    if (!delta) return;                       // 没挪动就别动它
    tr.style.transition = 'none';
    tr.style.transform = 'translateY(' + delta + 'px)';
    moved.push(tr);
  });
  if (!moved.length) return;

  // 下一帧再归位，浏览器才会把"反向位移"当成起始态做插值
  requestAnimationFrame(() => {
    moved.forEach((tr) => {
      tr.style.transition = 'transform 200ms cubic-bezier(.2,0,0,1)';
      tr.style.transform = '';
    });
    // 动画结束后把内联样式摘干净，别留着影响 :hover 之类的换状态
    setTimeout(() => {
      moved.forEach((tr) => { tr.style.transition = ''; tr.style.transform = ''; });
    }, 240);
  });
}

/**
 * 筛选/搜索时的行显隐过渡。
 *
 * 用在：项目中心搜索、模型仓库筛选——输入后行是**瞬间**显示/隐藏的，
 * 没有任何过渡，列表像"啪"地换了一张。
 *
 * 关键约束：这些输入框**没有 debounce**（输入即筛）。如果每次按键都播动画，
 * 连续打字会变成一串闪烁，比没有动画更糟。所以这里按**输入节奏**判断：
 * 距上次变化不足 300ms 就当"还在连续输入"，跳过动画直接切。
 * 这样不用改任何输入逻辑，也不用给每个入口加 debounce。
 *
 * @param {HTMLElement} root  用来找输入框的容器
 * @param {() => void} apply  真正执行筛选的函数（内部会设 tr.hidden 或 display）
 * @param {object} [opts]
 * @param {string} [opts.inputSel]  输入框选择器，用于判断连续输入
 * @param {HTMLElement} [opts.tbody] 行容器，用于拿 rows
 */
let lastFilterAt = 0;
export function softFilter(root, apply, opts = {}) {
  const tbody = opts.tbody || root.querySelector('table tbody');
  const rows = tbody ? [...tbody.querySelectorAll('tr')] : [];
  const now = Date.now();
  const typing = now - lastFilterAt < 300;
  lastFilterAt = now;

  const before = rows.map((tr) => !tr.hidden && tr.style.display !== 'none');

  apply();

  // 连续打字：直接切，不播动画
  if (typing || !rows.length) return;

  // **只用 opacity，不碰 transform**：flipSort 也在用 transform 做位移插值，
  // 两边都写同一个内联属性会互相覆盖——实测切换排序时 flipSort 刚设上的
  // 反向位移会被这里的收尾一次性清掉，排序动画看起来完全没有。
  // 分工：softFilter 管"显隐"（opacity），flipSort 管"位移"（transform）。
  rows.forEach((tr, i) => {
    const visible = !tr.hidden && tr.style.display !== 'none';
    if (visible === before[i]) return;
    tr.style.transition = 'opacity 120ms cubic-bezier(.2,0,0,1)';
    tr.style.opacity = visible ? '0' : '0';
    if (visible) {
      requestAnimationFrame(() => { tr.style.opacity = ''; });
    }
    setTimeout(() => { tr.style.transition = ''; tr.style.opacity = ''; }, 130);
  });
}

