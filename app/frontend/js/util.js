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
export async function run(label, fn, opts) {
  // opts.btn：把按钮交给 run 一起管忙碌态（见 withBusy）。这样调用点只要
  // 把 `btn.disabled = true` 换成传参，文案与 aria-busy 都跟着走，
  // 不用在每个视图里手写一遍「变灰 + 改文案 + 还原」。
  const busy = opts && opts.btn;
  const doing = (opts && opts.doing) || `${label}中…`;
  if (busy) return withBusy(busy, doing, () => runInner(label, fn));
  return runInner(label, fn);
}

async function runInner(label, fn) {
  try {
    await fn();
    return true;
  } catch (err) {
    // 401 不在这里弹 alert：会话过期是"要去重新登录"，不是"操作失败"。
    // 闸门由 api.js 的统一出口挂出来（app.js 接 app:unauthorized），
    // 这里只把"没做成"告诉调用方，别让人对着一个过期会话反复点确认。
    if (err && err.status === 401) return false;
    // 用全站那条轻提示，不用原生 `alert`：
    // `alert` 是**阻塞**的模态，会打断当前操作、也抢走焦点（键盘用户尤其难受），
    // 而且在真浏览器验收里是唯一会卡住脚本的调用——一条"写回失败"的提示
    // 不值得让人先点一下确定才能继续。
    // `app:toast` 是既有约定（`store.js:129`、`shot-workbench.js:336` 都在派），
    // 由 `app.js:641` 接住渲染成右下角那条。`util.js` 不 import `app.js`——
    // 那是壳，反向依赖会把工具模块和应用壳绑死。
    // whyHint 补那句「为什么」：403 是权限、404 是看不见，光一个状态码
    // 让人对着「失败：（403）」猜。errorBox／errorLine 早就用了它，
    // 只有这条 toast 路一直只说 err.message —— 而全站绝大多数操作的失败
    // 提示正是从这条路出去的，等于最常用的那条路一直缺这半句。
    window.dispatchEvent(new CustomEvent('app:toast', {
      detail: { text: `${label}失败：${err.message}${whyHint(err)}`, kind: 'err' } }));
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

/**
 * 「点开原图/产物」唯一的安全出口。
 *
 * 只放行同源、且路径落在 /files/ 或 /api/ 下的地址，其余返回 null。
 * 为什么不是「转义一下就行」：转义防的是拼进 HTML 属性的引号被截断，
 * 而这里的值是从 dataset 读回来后**直接交给 window.open** 的跳转目标，
 * 转义对 `javascript:` 这种协议一点作用都没有。
 *
 * 放在 util 里而不是各视图各写一份：白名单这种东西一旦有两份，
 * 迟早只改一处，另一处就成了后门。
 */
export function safeOpenUrl(raw) {
  let u;
  try {
    u = new URL(String(raw || ''), location.origin);
  } catch {
    return null;
  }
  if (u.origin !== location.origin) return null;
  if (!(u.pathname.startsWith('/files/') || u.pathname.startsWith('/api/'))) return null;
  return u.href;
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
  // **弹窗已经开着**：这一次直接取消，绝不覆盖 dangerResolve。
  //
  // 原来下面是无条件 `dangerResolve = resolve`，而弹窗只在第一次调用时才
  // createElement 并 append —— 第二次调用改的是**同一个**弹窗，把第一个
  // Promise 的 resolve 顶掉、且此后没有任何代码还会去调它。那一条 `await`
  // 永不 settle，发起它的按钮就永远停在 disabled。
  // 真实触发路径：连点两个删除；或一个确认框还开着时，某处轮询回来的异步
  // 分支也要确认一次。两者都不是理论构造。
  // 这里选「这一次直接返回 false」而不是排队：危险操作被吞掉是安全的方向；
  // 而且排队会让两个框的内容与顺序对不上，人看到的是「点了没反应、然后又
  // 跳出来一个框」，比干脆没响应更难受。
  if (dangerHost && !dangerHost.hidden) return Promise.resolve(false);
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

/**
 * 把"有弹窗开着"这件事同步到 DOM：开着就把背景锁上，关完就摘掉。
 *
 * 顺序上以**最后打开的那个**为准（全站逻辑都是"最上面那层接键盘"）。
 * 用 `hasAttribute` 而不是布尔缓存：`hidden` 可能被任何调用点直接改，
 * 观察器是唯一真相，缓存迟早和 DOM 走散。
 */
function syncModalInert() {
  const open = [...document.querySelectorAll('.modal')].filter((m) => !m.hidden);
  const app = document.querySelector('.app');
  if (!app) return;
  // 不能简单锁 `.app`：有些弹窗就写在视图模板里（#mc-form 在 .app 内部），
  // 锁 .app 会把它自己一起锁死——焦点进不去、按钮点不动，看着像弹窗坏了。
  // 反过来说也不能把祖先链全解锁，那等于没锁。
  //
  // 正确的做法是逐层走：从 body 往下，凡是**不在弹窗祖先链上**的兄弟都锁上。
  // inert 作用于整棵子树，所以锁住兄弟就足够了，弹窗那条链一个不动。
  const keep = new Set();
  for (const m of open) {
    for (let el = m; el && el !== document.body; el = el.parentElement) keep.add(el);
  }
  // 这几样必须留在 inert 外面：toast 与读屏播报区是说给人听的通道，
  // 弹窗开着的时候它们照样要有声音——把播报一起锁掉，等于让读屏用户
  // 在弹窗期间彻底失去反馈。
  const KEEP_ALIVE = '.toast-host, #live-polite, .sr-only';
  const lockThese = (el) => {
    for (const child of el.children) {
      // 走到弹窗本身：**到底为止**。它自己不能锁（那会把弹窗内容一起锁死，
      // 焦点进不去、按钮点不动），也不能再往它子层走——里面全是弹窗自己的
      // 东西。之前这里只判了 keep.has 就继续递归，结果 .modal-box 被当成了
      // 兄弟锁住，弹窗看着开着却整个失灵。
      if (child.classList.contains('modal')) continue;
      if (keep.has(child)) { lockThese(child); continue; }
      if (child.matches(KEEP_ALIVE)) continue;
      if (!child.hasAttribute('inert')) child.setAttribute('inert', '');
      child.dataset.inertByModal = '1';
    }
  };
  if (open.length) {
    lockThese(document.body);
  } else {
    // 全关了就只撤我们加的那些，别人（视图自己）加的 inert 不碰
    for (const el of document.querySelectorAll('[data-inert-by-modal]')) {
      el.removeAttribute('inert');
      delete el.dataset.inertByModal;
    }
  }
  // 顺带给开着的弹窗补上语义：模板里写了一半，剩下的一半不该靠人记。
  // role/aria-modal 是"这是对话框"的唯一机器可读信号，缺了读屏会把弹窗
  // 当成页面上又一段普通内容念过去。
  for (const m of open) {
    if (m.getAttribute('role') !== 'dialog') m.setAttribute('role', 'dialog');
    if (m.getAttribute('aria-modal') !== 'true') m.setAttribute('aria-modal', 'true');
    if (!m.hasAttribute('aria-labelledby') && !m.hasAttribute('aria-label')) {
      const head = m.querySelector('.modal-head');
      if (head) {
        if (!head.id) head.id = 'modal-head-' + (headSeq += 1);
        m.setAttribute('aria-labelledby', head.id);
      }
    }
  }
}

let headSeq = 0;

/**
 * Tab 循环。**只在环绕的那两步接管**，中间一律交给浏览器。
 *
 * 原来这里是「聚焦在首元素就送去末元素、在末元素就送去首元素」，不看方向——
 * 弹窗里有五个按钮时，人在第一个上按 Tab 会直接跳到第五个，第二三四永远
 * 摸不到；按 Shift+Tab 又跳回第五个。看着像「陷阱生效了」，其实键盘已经坏了。
 * 现在只处理两个越界：Tab 在末元素 → 回首个；Shift+Tab 在首元素 → 去末个。
 * 其余情况直接返回、让浏览器按自己的顺序走，那才是对的顺序。
 */
function trapTab(modal, back) {
  const list = focusables(modal);
  if (!list.length) return;
  const first = list[0];
  const last = list[list.length - 1];
  const active = document.activeElement;
  // 焦点还在弹窗外（刚打开、或人在外面点过一下）：先收进来
  if (!modal.contains(active)) { (back ? last : first).focus(); return; }
  if (back && active === first) last.focus();
  else if (!back && active === last) first.focus();
  // 中间的元素：什么都不做，浏览器自己会走到下一个
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
  // 弹窗一打开，底下的侧栏和正文仍然可点、可 Tab 进去——键盘用户会从弹窗
  // 直接掉到背后的页面里，鼠标用户则会误点到被遮住的东西。`inert` 是浏览器
  // 原生那一层：既挪出 Tab 序列，也吃掉指针事件，还让读屏只看见弹窗。
  //
  // 锁的范围见 syncModalInert：不是整块 `.app`，而是弹窗祖先链之外的兄弟——
  // 因为有些弹窗（#mc-form、#job-modal）本来就写在 `.app` 里。
  //
  // 用观察器跟着 `hidden` 走，而不是要求每个调用点补一句——全库 126 处直接写
  // `modal.hidden = true`，逐个改不现实，漏一个就是一个能 Tab 出去的弹窗。
  new MutationObserver(syncModalInert).observe(document.body, {
    childList: true, subtree: true, attributes: true, attributeFilter: ['hidden'],
  });
  syncModalInert();
  document.addEventListener('keydown', (e) => {
    const open = [...document.querySelectorAll('.modal:not([hidden])')];
    if (!open.length) return;
    const top = open[open.length - 1];
    if (e.key === 'Escape') {
      const fn = closers.get(top);
      if (fn) { e.preventDefault(); fn(); }
      return;
    }
    if (e.key === 'Tab') trapTab(top, e.shiftKey);
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
    // 只给"刚变可见"的行上透明再淡入；变隐藏的方向不需要动画——
    // 行已经不可见了，给它做淡出只是白占一次重绘。
    // （这里原来写成 `visible ? '0' : '0'`，两个分支同一个值，是笔误的形状。）
    if (visible) tr.style.opacity = '0';
    if (visible) {
      requestAnimationFrame(() => { tr.style.opacity = ''; });
    }
    setTimeout(() => { tr.style.transition = ''; tr.style.opacity = ''; }, 130);
  });
}


/* ---------------- 读屏播报（aria-live） ----------------
 * 2026-11-09 之前全站 `aria-live` 出现 0 次。视觉上一切正常，但读屏用户听到的
 * 是一个**不会说话**的界面：点了保存、写回失败、切了一页、worker 掉线——
 * 屏幕上这些都有变化，声轨上什么都没有。这不是"锦上添花"，是那部分人用不了。
 *
 * 放在 util.js 而不是 app.js：`announce` 是通用工具，router（切页面）与
 * app（轻提示、worker 状态）都要用；放进 app.js 这个壳里，router 就得反向
 * 依赖壳，那是不该有的方向。
 *
 * 只开一档 polite：不打断读屏正在念的句子。`role="alert"` 隐式 assertive，
 * 全站没有一处"必须立刻打断人"的信息，先不滥用。
 *
 * 一个坑：同一节点上连写两次一模一样的文字，读屏往往一声不吭（文本没变化
 * ＝没有变更事件）。所以先清空再写，并带一个零宽空格制造真实变更。
 */
let livePolite = null;
let liveSeq = 0;

function ensureLiveRegion() {
  if (livePolite && document.body.contains(livePolite)) return livePolite;
  livePolite = document.createElement('div');
  livePolite.className = 'sr-only';
  livePolite.id = 'live-polite';
  livePolite.setAttribute('role', 'status');
  livePolite.setAttribute('aria-live', 'polite');
  livePolite.setAttribute('aria-atomic', 'true');
  document.body.appendChild(livePolite);
  return livePolite;
}

/** 播报一句给读屏听。`force` 用于"内容相同也要再说一遍"的场合。 */
export function announce(text, force = false) {
  const body = String(text || '').trim();
  if (!body) return;
  const host = ensureLiveRegion();
  if (!force && host.dataset.text === body) return;   // 不重复念同一句
  host.dataset.text = body;
  liveSeq += 1;
  host.textContent = '';            // 先清空：同文重复时读屏才会重新出声
  requestAnimationFrame(() => {
    // 零宽空格参与"内容指纹"但不被念出来，用来制造一次真实变更
    host.textContent = body + (liveSeq % 2 ? '\u200b' : '\u200b\u200b');
  });
}

/**
 * 骨架占位：数据还没到手时，先把**将要长出来的形状**摆出来。
 *
 * 原来这九处都写一句「正在读……」。问题不在措辞，在几何：一行 13px 的字
 * 与一张三行卡片的高度差着好几倍，数据一到，页面就「腾」地撑开或瘪下去——
 * 人的眼睛正盯着那块，视线被甩一次。骨架屏用同一套行高与间距占住位置，
 * 数据到了只换内容、不动版。
 *
 * 为什么不放转圈：转圈只说明「在等」，不说明「等的是什么、会出来几行」。
 * 骨架的条数就是预期的条数，人一眼能看出这一屏大概会有多少东西。
 *
 * `rows` 是预期行数，`kind` 挑形状：
 *   card （默认）三行卡片：标题条 + 两行正文
 *   row   表格行：一条细长条，配 `rows` 铺满
 *   text  单段文字：两行，用于抽屉、详情这类
 *   grid  卡片网格：每格封面 + 一行标题
 *
 * 无障碍：外层 `aria-busy="true"` + `role="status"`，并塞一句给读屏的
 * 「正在读取」——骨架条本身是装饰（`aria-hidden`），读屏不该去念一堆空 div。
 * 数据到了把整块换掉，`aria-busy` 随之消失，读屏那边就是「忙完了」。
 */
export function skeleton(rows = 3, kind = 'card') {
  const n = Math.max(1, Math.min(Number(rows) || 3, 12));
  const bar = (w, h = 12) => `<div class="sk-bar" style="width:${w}%;height:${h}px"></div>`;
  let body;
  if (kind === 'row') {
    body = Array.from({ length: n }, () => `<div class="sk-line">${bar(100, 14)}</div>`).join('');
  } else if (kind === 'text') {
    body = bar(92) + bar(64) + bar(78, 10);
  } else if (kind === 'grid') {
    body = `<div class="sk-grid">` + Array.from({ length: n }, () =>
      `<div class="sk-cell"><div class="sk-cover"></div>${bar(70, 10)}</div>`).join('') + '</div>';
  } else {
    body = Array.from({ length: n }, () =>
      `<div class="sk-card">${bar(38)}${bar(100, 10)}${bar(84, 10)}</div>`).join('');
  }
  return `<div class="sk" aria-busy="true" role="status">
    <span class="sr-only">正在读取</span>
    <div aria-hidden="true">${body}</div>
  </div>`;
}

/**
 * 按钮「进行中」态：禁用它、把文案换成进行时、结束再原样还回去。
 *
 * 为什么要有这个（2026-11-12 前端体验优化）：全站 68 处 `btn.disabled = true`，
 * 其中改过文案的只有 2 处（models/engines.js 的启动服务）。其余 66 处点下去
 * 只是变灰——用户看不出「正在处理」还是「点坏了」，于是会再点一次；
 * 而按钮已经 disabled，第二次点击没有任何反应，体验上等于卡死。
 *
 * 用法（与 run 配套，两者职责不混）：
 *
 *     const ok = await run('保存', () => api.save(x), { btn: saveBtn, doing: '保存中…' });
 *
 * 也可以单独用 `withBusy(btn, '保存中…', () => ...)` 包任意异步动作。
 *
 * 三个细节：
 *  1. 用 `dataset.busy` 记原文案，不用变量——同一个按钮可能被两条路径先后点，
 *     记在 DOM 上才不会互相覆盖；
 *  2. `finally` 里无条件还原：抛错时按钮必须回到可点状态，否则一次网络抖动
 *     就把这个按钮永久废掉了；
 *  3. 只改 textContent、不改 innerHTML：按钮里可能有图标（`.i-sun` 之类），
 *     整块重写会把它们弄丢。所以先量一下：**有子元素的按钮不动文案**，
 *     只做 disabled + aria-busy，免得把图标吃了。
 */
export async function withBusy(btn, doing, fn) {
  if (!btn) return fn();
  // 有子元素（图标、计数器）的按钮不改文案，只标记忙碌态
  const textOnly = btn.children.length === 0;
  if (textOnly) {
    btn.dataset.busyText = btn.textContent;
    btn.textContent = doing;
  }
  btn.disabled = true;
  // aria-busy 让读屏知道这个控件正忙——光变灰对屏幕阅读器没有区别
  btn.setAttribute('aria-busy', 'true');
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
    if (textOnly && btn.dataset.busyText !== undefined) {
      btn.textContent = btn.dataset.busyText;
      delete btn.dataset.busyText;
    }
  }
}
