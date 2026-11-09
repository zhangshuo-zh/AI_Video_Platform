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
let timer = null;
let items = [];

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

function render(menu, dot) {
  const unread = items.filter((x) => !x.read).length;
  if (dot) dot.hidden = unread === 0;
  paintUnreadTone(unread);

  if (!items.length) {
    menu.innerHTML = '<div class="bell-empty">没有通知</div>';
    return;
  }
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
    <div class="bell-foot">
      <span class="hint">${unread} 条未读</span>
      <button class="btn xs" id="bell-read-all" ${unread ? '' : 'disabled'}>全部已读</button>
    </div>`;
}

async function refresh(dot, menu) {
  try {
    const payload = await api.notifications();
    items = payload?.items || [];
    if (menu) render(menu, dot);
    else {
      const unread = payload?.unread || 0;
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
  if (!btn || !menu || !dot) return;

  refresh(dot, null);

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
        await refresh(dot, null);
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
    refresh(dot, null);
  });

  // 点外面收起（与顶栏其它菜单同一套行为）
  document.addEventListener('click', (e) => {
    if (!menu.hidden && !menu.contains(e.target) && !btn.contains(e.target)) {
      closeMenu();
    }
  });

  // **Esc 关闭**（2026-11-10 自查补的）：顶栏的项目菜单、集菜单、用户菜单
  // 三个都支持 Esc（见 app.js 的 closeProjMenu/closeEpMenu 与 bindUserMenu），
  // 只有这一处漏了——键盘用户点开之后没有退路。同一套外壳就该有同一套行为。
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !menu.hidden) {
      closeMenu();
      btn.focus();          // 焦点还给铃铛，否则会掉到 body 上
    }
  });

  // 轮询：卸载时由调用方 stopNotifications 清掉，避免切页后还在跑
  stopNotifications();
  timer = setInterval(() => refresh(dot, menu.hidden ? null : menu), POLL_MS);
}

export function stopNotifications() {
  if (timer) { clearInterval(timer); timer = null; }
}
