/* 顶栏通知铃铛（0049）：
 *
 * 需求原话是「worker 停止、掉线影响任务运行，被影响的项目所属的用户和协作者
 * 可以收到消息提醒，点击跳转至相关页面进行处理」。这里只做三件事：
 * 拉未读、画列表、点了就跳——**判定谁该收在服务端**（`notifications.inbox`），
 * 前端不认识"项目成员"这件事，也就不可能算错。
 *
 * 两条纪律：
 *
 * 1. **未读数以接口为准，不在前端自己数**。列表可能被 limit 截断，
 *    前端 filter 一遍就漏数；而且"已读"是按人记的，前端没有那份状态。
 *
 * 2. **点开不自动全标记已读**。人点开看一眼发现与自己无关，就该保留未读
 *    （否则下次真出事先被自己划掉了）。要清得显式点「全部已读」。
 */
import { api } from './api.js';
import { escapeHtml } from './util.js';
import { setSub } from './navstate.js';
import { MODULES } from './modules.js';

const POLL_MS = 20000;      // 20 秒一轮，与 worker 心跳同量级；不追实时
// 面板一次取多少条。后端 Query(30, ge=1, le=100) 的上限就是 100，取满它——
// 原来不传 limit 只拿 30 条，第 31 条起在界面上无法到达（无翻页、无加载更多）。
const LIMIT = 100;
let timer = null;
let items = [];
// 挂在 document 上的三个监听。它们不随任何节点回收，必须记住引用成对解绑：
// visibilitychange 只在 document 上派发（不冒泡到元素），点外面与 Esc 也
// 只能挂 document。会话过期后不再 mount，靠 stopNotifications 一起收。
let visHandler = null;
let awayHandler = null;
let escHandler = null;
// 回前台补一拍要用的那两个节点：用的就是 mount 那一次的，不重新查 DOM。
let pollDot = null;
let pollMenu = null;

/** 通知落到哪一页。
 *
 *  三个约定（2026-11-10 自查时改的，原来那条跳转**丢了子页**）：
 *
 *  1. `link_page` 与 `modules.js` 的 id 一致（服务端只存页面 id，不存 URL——
 *     端口与域名会变，路径不会）。
 *  2. `link_params.sub` 是**子页名**（人读的），而路由里的子页是**下标**：
 *     `#models/3` 才是运行监控。名字→下标要在这里换一次，因为 `MODULES`
 *     是前端才知道的东西，服务端不该存数字（它一改顺序就全错）。
 *  3. 只设 hash 不够：视图靠 `navstate.setSub()` 决定画哪一屏。
 *     不调它，页面会停在第一个子页——这正是第一版的表现（点"worker 掉线"
 *     落到了「模型仓库」而不是「运行监控」）。
 */
function navigate(item) {
  const page = item.link_page || 'overview';
  const subName = (item.link_params || {}).sub || '';
  if (subName) {
    const mod = MODULES.find((m) => m.id === page);
    const index = (mod?.subs || []).indexOf(subName);
    if (index >= 0) setSub(page, index);
  }
  // **必须走 `app:go` 事件，不能只改 `location.hash`**（2026-11-10 实测踩到）：
  // 直接写 hash 只会改地址栏，`renderView()` 不会跑——页面停在原处。
  // 实测那次的表现是：地址变成 #models 了，而侧栏高亮还在「资产库」、
  // 主标题还是「生成中心」，即"地址跳了、页面没动"。
  // `app:go` 是项目里"人主动换页"的唯一入口（app.js:753 接到 goView），
  // 与 asset-detail / writing 那几处跳转用的是同一条路。
  window.dispatchEvent(new CustomEvent('app:go', { detail: page }));
}

/** 画面板。
 *
 *  unread 由调用方从服务端字段传进来，不在这里数（文件头第 1 条纪律）。
 *  2026-11-11 审计实测：原来这里写的是 items.filter(x => !x.read).length，
 *  而 items 被 limit 截断——造 45 条未读，服务端 unread=45 / items=30，
 *  页脚就显示"30 条未读"，「全部已读」的启用判据也跟着少算。注释写对了、
 *  代码没跟上，正是这条纪律要防的情形。
 */
function render(menu, dot, unread) {
  if (dot) dot.hidden = unread === 0;
  paintUnreadTone(unread);

  if (!items.length) {
    menu.innerHTML = '<div class="bell-empty">没有通知</div>';
    return;
  }
  // 截断提示的判据是「未读有没有全在列表里」，不是 unread > items.length：
  // 列表按 id 倒序取最近 N 条，已读的那些也占名额。最新 100 条里若 90 条已读、
  // 10 条未读，而真实未读 45，那时 45 > 100 为假、却仍有 35 条未读看不见。
  // 这里数 items 是为了判断要不要提示；角标与页脚的数字仍然只用服务端的
  // unread——两者不是一回事，别把这一行当成"又在前端自己数未读"。
  const shownUnread = items.filter((x) => !x.read).length;
  const truncated = shownUnread < unread;
  const rows = items.map((item) => `
    <button class="bell-item ${item.read ? '' : 'unread'}" data-id="${item.id}">
      <div class="bt">
        <span class="chip ${item.level === 'bad' ? 'red'
                          : (item.level === 'warn' ? 'amber' : 'plain')}"
              style="font-size:10px;padding:1px 6px">${escapeHtml(item.kind_label || item.kind)}</span>
        <b>${escapeHtml(item.title)}</b>
      </div>
      ${item.body ? `<div class="bd">${escapeHtml(item.body)}</div>` : ''}
      <div class="bm">${escapeHtml(String(item.created_at || '').slice(0, 16))}
        ${item.read ? '' : ' · 未读'}</div>
    </button>`).join('');

  menu.innerHTML = `${rows}
    ${truncated ? `<div class="bell-trunc">未读共 ${unread} 条，这里只列出最近 ${items.length} 条。
      「全部已读」仍会清掉全部未读（含没列出来的）。</div>` : ''}
    <div class="bell-foot">
      <span class="hint">${unread} 条未读</span>
      <button class="btn xs" id="bell-read-all" ${unread ? '' : 'disabled'}>全部已读</button>
    </div>`;
}

async function refresh(dot, menu) {
  try {
    // 菜单关着时**只要未读数**，不必把整页通知正文拉回来（2026-11-10 审计）。
    //
    // 角标用的是 `payload.unread`——服务端独立那条 COUNT 查询给的
    // （`notifications.py:190-196`），跟 `items` 的长度无关。也就是说
    // 菜单关着的每一轮轮询，`items` 那 30 条正文是**纯废流量**：拉回来
    // 赋值给 `items`，然后没人看，下一次又被覆盖。
    // `unread_only=true` 时 `items` 只剩未读（通常 0—几条），`unread`
    // 仍是全量未读数，角标口径不变。
    //
    // 只在这个方向省：菜单**开着**时必须拉全量（人要看已读的那些），
    // 不能因为"反正角标只要计数"就把打开的列表缩成未读——那样已读通知
    // 会从眼前消失。
    // 面板打开时取满上限（后端 le=100）；关着时只要未读数，不传 limit。
    const payload = await api.notifications(!menu, menu ? LIMIT : undefined);
    items = payload?.items || [];
    // 两条分支共用同一个口径：服务端独立那条 COUNT，与 items 长度无关。
    const unread = payload?.unread ?? 0;
    if (menu) render(menu, dot, unread);
    else {
      if (dot) dot.hidden = unread === 0;
      paintUnreadTone(unread);
    }
  } catch {
    // 拉不到就不显示角标：宁可静默，也不要用一个假的未读数骗人去点。
    if (dot) dot.hidden = true;
    paintUnreadTone(0);
  }
}

/** 有未读时给铃铛换个色（`.has-unread`）。只改颜色不改尺寸——
 *  那一栏左右都有间距，宽度一变会跳。 */
function paintUnreadTone(unread) {
  const btn = document.getElementById('bell-btn');
  if (btn) btn.classList.toggle('has-unread', unread > 0);
}


/** 收起菜单并同步 aria。三处调用（点外面、Esc、点条目）共用一份，
 *  免得漏掉 `aria-expanded` 让读屏软件以为它还开着。 */
function closeMenu() {
  const btn = document.getElementById('bell-btn');
  const menu = document.getElementById('bell-menu');
  if (menu) menu.hidden = true;
  if (btn) btn.setAttribute('aria-expanded', 'false');
}


export function mountNotifications() {
  const btn = document.getElementById('bell-btn');
  const menu = document.getElementById('bell-menu');
  const dot = document.getElementById('bell-dot');
  if (!btn || !menu || !dot) { stopNotifications(); return; }

  refresh(dot, null);

  // 节点级委托只挂一次。会话过期后重新登录会再调一次 mountNotifications，
  // 而铃铛这两个节点一直在 body 里、不随视图回收：不判一次就会叠两份 click，
  // 一次点击开→关，看上去像铃铛坏了。要重挂的只有轮询与 document 监听。
  if (!btn.__bellBound) {
    btn.__bellBound = true;
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const willOpen = menu.hidden;
      menu.hidden = !willOpen;
      btn.setAttribute('aria-expanded', String(willOpen));
      if (willOpen) {
        await refresh(dot, menu);
      }
    });

    // 点条目：跳过去 + 标记已读（**只有被点的那一条**，见文件头第 2 条）
    menu.addEventListener('click', async (e) => {
      const readAll = e.target.closest('#bell-read-all');
      if (readAll) {
        try {
          await api.markNotificationsRead([]);     // 空 ids＝全部已读
          closeMenu();
          // **要把 menu 一起传**：原来传 `null` 等于"只更新角标、不重绘列表"，
          // 于是菜单 DOM 里还留着"3 条未读"和 3 个 .unread 条目。
          // 菜单此刻已关、用户看不见，但只要在 refresh 的 await 落地前重新打开，
          // 就会看到那个过期状态（实测能稳定复现）。重绘一次就没有这个窗口了。
          await refresh(dot, menu);
        } catch { /* 失败就保持原样，下次刷新重试 */ }
        return;
      }
      const row = e.target.closest('.bell-item');
      if (!row) return;
      const id = Number(row.dataset.id);
      const item = items.find((x) => x.id === id);
      closeMenu();
      try {
        if (item && !item.read) await api.markNotificationsRead([id]);
      } catch { /* 跳转优先于标记 */ }
      if (item) navigate(item);
      // 同上：传 menu，让列表里那一条立刻变成已读态。
      // 这里不 await——跳转已经发生，不该让新页面的首屏等这次刷新。
      refresh(dot, menu);
    });
  }

  // 点外面收起（与顶栏其它菜单同一套行为）
  awayHandler = (e) => {
    if (!menu.hidden && !menu.contains(e.target) && !btn.contains(e.target)) {
      closeMenu();
    }
  };
  document.addEventListener('click', awayHandler);

  // **Esc 关闭**（2026-11-10 自查补的）：顶栏的项目菜单、集菜单、用户菜单
  // 三个都支持 Esc（见 app.js 的 closeProjMenu/closeEpMenu 与 bindUserMenu），
  // 只有这一处漏了——键盘用户点开之后没有退路。同一套外壳就该有同一套行为。
  escHandler = (e) => {
    if (e.key === 'Escape' && !menu.hidden) {
      closeMenu();
      btn.focus();          // 焦点还给铃铛，否则会掉到 body 上
    }
  };
  document.addEventListener('keydown', escHandler);

  // 轮询：卸载时由调用方 stopNotifications 清掉，避免切页后还在跑
  stopNotifications();
  pollDot = dot;
  pollMenu = menu;
  timer = setInterval(() => {
    // 后台标签页不发（2026-11-10 审计，与 monitor.js／app.js 同一纪律）。
    // 原来这条轮询不看 document.hidden：人在别的标签页干活时它照跑，
    // 而屏幕上没有"让人知道"的对象。
    if (document.hidden) return;
    refresh(dot, menu.hidden ? null : menu);
  }, POLL_MS);

  // 回前台补一拍（2026-11-11 审计 F-04）：只跳过不够——人在别的标签页待了
  // 十分钟，切回来看到的还是十分钟前那个角标，而这十分钟里 worker 可能
  // 已经掉线了。补这一下才让"回来看到的就是现在"，不额外加轮询。
  visHandler = () => {
    if (document.hidden) return;
    refresh(pollDot, pollMenu && !pollMenu.hidden ? pollMenu : null);
  };
  document.addEventListener('visibilitychange', visHandler);
}

/** 收掉这一套：计时器 + 三个 document 监听。
 *
 *  为什么要收监听而不只是计时器：会话过期走的是 app:unauthorized →
 *  登录闸门，**不重新加载页面**（app.js:769）。只停计时器的话，过期之后
 *  每一次点外面、每一次按 Esc 都还在跑旧闭包；而重新登录会再 mount 一次，
 *  于是同一件事叠两份回调。 */
export function stopNotifications() {
  if (timer) { clearInterval(timer); timer = null; }
  if (visHandler) { document.removeEventListener('visibilitychange', visHandler); visHandler = null; }
  if (awayHandler) { document.removeEventListener('click', awayHandler); awayHandler = null; }
  if (escHandler) { document.removeEventListener('keydown', escHandler); escHandler = null; }
  pollDot = null;
  pollMenu = null;
}
