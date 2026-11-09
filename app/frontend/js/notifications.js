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

const POLL_MS = 20000;      // 20 秒一轮，与 worker 心跳同量级；不追实时
let timer = null;
let items = [];

/** 通知落到哪一页。`link_page` 与 `modules.js` 的 id 一致，`link_params`
 *  里的 `sub` 是该页的二级子页名（与 `views/<page>.js` 的 SUBS 对得上）。 */
function navigate(item) {
  const page = item.link_page || 'overview';
  const sub = (item.link_params || {}).sub || '';
  // 用 hash 路由：`#<page>`，子页由视图自己按 `SUBS` 下标钳位。
  // 这里**不拼完整 URL**——端口/域名会变，路径不会（服务端也只存页面 id）。
  window.location.hash = sub ? `${page}` : page;
  // 子页切换：模块页监听这个事件（见 `models.js` 的 SUBS 处理）。
  if (sub) {
    window.dispatchEvent(new CustomEvent('page:sub', {
      detail: { page, sub },
    }));
  }
}

function render(menu, dot) {
  const unread = items.filter((x) => !x.read).length;
  if (dot) dot.hidden = unread === 0;

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
    else if (dot) dot.hidden = (payload?.unread || 0) === 0;
  } catch {
    // 拉不到就不显示角标：宁可静默，也不要用一个假的未读数骗人去点。
    if (dot) dot.hidden = true;
  }
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
        menu.hidden = true;
        btn.setAttribute('aria-expanded', 'false');
        await refresh(dot, null);
      } catch { /* 失败就保持原样，下次刷新重试 */ }
      return;
    }
    const row = e.target.closest('.bell-item');
    if (!row) return;
    const id = Number(row.dataset.id);
    const item = items.find((x) => x.id === id);
    menu.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
    try {
      if (item && !item.read) await api.markNotificationsRead([id]);
    } catch { /* 跳转优先于标记 */ }
    if (item) navigate(item);
    refresh(dot, null);
  });

  // 点外面收起（与顶栏其它菜单同一套行为）
  document.addEventListener('click', (e) => {
    if (!menu.hidden && !menu.contains(e.target) && !btn.contains(e.target)) {
      menu.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
    }
  });

  // 轮询：卸载时由调用方 stopNotifications 清掉，避免切页后还在跑
  stopNotifications();
  timer = setInterval(() => refresh(dot, menu.hidden ? null : menu), POLL_MS);
}

export function stopNotifications() {
  if (timer) { clearInterval(timer); timer = null; }
}
