/**
 * 应用入口：侧栏、命令面板、项目选择器、启动流程。
 * 页面内容全部由 router + views 负责，这里只管壳。
 */
import { MODULES } from './modules.js';
import { store } from './store.js';
import { api } from './api.js';
import { renderView, currentViewId } from './router.js';
import { setParam, setSub } from './navstate.js';
import { escapeHtml, announce } from './util.js';
import { applyPicks, restorePicks } from './pick.js';
import { mountNotifications, stopNotifications } from './notifications.js';

/* ---------------- worker 状态条 ---------------- */

/** 侧栏两行状态一起画：worker 点＋文字、队列点＋文字。
 *
 * 顶栏那颗 worker 胶囊 2026-11-07 撤了：左下角已经有同名同状态的一行，
 * 同一件事摆两处，人还得想「这俩是不是不一样」。侧栏这一份更全（还带点），
 * 所以留侧栏、撤顶栏。
 *
 * 读不到状态就保持「未接入」——不拿 0 冒充「队列空闲」。
 */
let workerSpoken = null;      // 上一次播报过的 worker/队列口径
let workerOnlineSpoken = null;

function paintWorker() {
  const w = store.worker;
  const dot = document.getElementById('worker-dot');
  const label = document.getElementById('worker-label');
  const queueDot = document.getElementById('queue-dot');
  const queue = document.getElementById('queue-label');
  if (!dot || !label) return;

  const online = !!w?.online;
  dot.className = 'dot ' + (online ? 'live' : 'idle');
  label.textContent = online ? '本地 worker 在线' : '本地 worker 未接入';

  if (queue) {
    const q = w?.queue || { queued: 0, running: 0 };
    const busy = (q.queued + q.running) > 0;
    queue.textContent = busy
      ? `队列 等待 ${q.queued} · 运行 ${q.running}`
      : '队列 空闲';
    // 队列那行的点：有活就亮，空闲就灰着。它与 worker 点说的不是一件事
    // （worker 在不在线 vs 队列有没有活），所以各用各的，不合并。
    if (queueDot) queueDot.className = 'dot ' + (busy ? 'live' : 'idle');

    // 一句话口径：worker 在不在 + 队列几个等、几个跑。变化与否比这一串就够了。
    const spoken = (online ? '在线' : '未接入') + '|' + q.queued + '|' + q.running;

    // 读屏那边只播"变了"的那一下：这函数每 20 秒被轮询叫一次，
    // 每轮都念一遍"队列 空闲"会把人烦到把读屏关掉——那比不播还糟。
    // 首帧（workerSpoken 还是 null）也不播：页面刚打开就念一句
    // "worker 在线"是噪音，这一行本来就写在侧栏上让人自己看。
    if (workerSpoken !== null) {
      if (online !== workerOnlineSpoken) {
        announce(online ? '本地 worker 已连接' : '本地 worker 已断开', true);
      } else if (workerSpoken !== spoken) {
        // 队列数变了通常正是"有活开跑了"或"跑完了"，是人想知道的那一下
        announce(busy ? `队列有活：等待 ${q.queued}，运行 ${q.running}` : '队列已空闲');
      }
    }
    workerSpoken = spoken;
    workerOnlineSpoken = online;
  }
}

/* ---------------- 侧栏 ---------------- */

function allowed(entry) {
  // perms 是"任一即可"；没有 perms 的模块人人可见
  return !entry?.perms || entry.perms.some((code) => store.can(code));
}

function buildNav() {
  const nav = document.getElementById('nav');
  // 没权限的模块连入口都不给（真正的拦截在服务端，这里只是别让人白点）
  const visible = MODULES.filter((m) => !m.hidden && allowed(m));
  // 0037 分组：任务书 §11 要的是"按我在做什么"找入口，不是按模块名列表。
  // 分组只影响侧栏的排布与一行小标题——**模块 id、顺序、二级页一个都没动**，
  // 深链 #assets/1/a57 这类地址照旧有效（为导航调整而删页面是任务书明确禁止的）。
  const groups = [];
  visible.forEach((m) => {
    const key = m.group || (m.utility ? '__utility' : '__plain');
    let bucket = groups.find((g) => g.key === key);
    if (!bucket) { bucket = { key, label: m.group_label || '', items: [] }; groups.push(bucket); }
    bucket.items.push(m);
  });
  nav.innerHTML = groups.map((g) => {
    const body = g.items.map((m) => {
      const item = `<div class="nav-item" data-v="${m.id}"><span class="nav-ico" aria-hidden="true"><svg><use href="#${m.icon}"/></svg></span>${m.label}</div>`;
      if (!m.subs.length) return item;
      const subs = m.subs
        .map((s, i) => `<span class="sub${i === 0 ? ' on' : ''}">${s}</span>`).join('');
      // 内层那圈 `.nav-sub-in` 是动效的必需品：`grid-template-rows:0fr` 要压的是
      // 一个可以缩到 0 的行，padding/gap 必须住在内层，否则那条内边距撑着一个
      // "0 高度"的容器，缩回时收不干净（app.css 的 .nav-sub 注释写了为什么不用 display 切换）
      return `<div class="nav-group${m.utility ? ' utility' : ''}">${item}`
        + `<div class="nav-sub"><div class="nav-sub-in">${subs}</div></div></div>`;
    }).join('');
    if (!g.label) return body;
    return `<div class="nav-section"><div class="nav-section-t">${g.label}</div>${body}</div>`;
  }).join('');
}

// 渲染完再同步顶栏：项目中心的 render 会重拉列表，当前项目被删或归档时会换人，
// 只同步一次的话顶部还挂着旧项目名。所有"换视图"的入口都走这里。
async function goView(id) {
  await renderView(id);
  syncChrome();
}

function bindNav() {
  document.getElementById('nav').addEventListener('click', (e) => {
    const sub = e.target.closest('.sub');
    if (sub) {
      // 认容器而不是认 parentElement：动效给二级项加了一层 .nav-sub-in 外壳，
      // 用 closest 就与"这层壳今天有几层"解耦，加壳、去壳都不用回来改这里
      const group = sub.closest('.nav-sub');
      const index = [...group.querySelectorAll('.sub')].indexOf(sub);
      const viewId = sub.closest('.nav-group').querySelector('.nav-item').dataset.v;
      group.querySelectorAll('.sub').forEach((x) => x.classList.remove('on'));
      sub.classList.add('on');
      setSub(viewId, index);
      // 点侧栏（含二级标签）＝回列表：详情参数不清掉的话，
      // 从资产详情点"资产中心"会停在原来那条资产上，看着像侧栏没反应
      setParam(viewId, undefined);
      goView(viewId);
      return;
    }
    const item = e.target.closest('.nav-item');
    if (item) { setParam(item.dataset.v, undefined); goView(item.dataset.v); }
  });
}

/* ---------------- 顶栏项目切换（自绘菜单） ----------------
 * 不用原生 <select>：Safari 没有以代码打开下拉列表的 API，只能 focus，
 * 于是"点文字→出下拉→再点一次才展开"要两下，比原来还慢。自绘菜单一下即开。
 * 菜单每次打开现渲染，所以新建/删除项目后不会拿到陈旧列表。
 */

const TICK = '<svg viewBox="0 0 12 12" width="11" height="11" fill="none" stroke="currentColor"'
  + ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2.4 6.4 4.9 8.9 9.6 3.4"/></svg>';

let pmIndex = -1;   // 菜单里当前高亮的那一行，和"已选中的项目"是两件事

function pmItems() {
  return [...document.getElementById('proj-menu').querySelectorAll('.pm-item')];
}

function openProjMenu() {
  // 只列在产的：归档了还挂在"当前项目"下拉里，归档就白做了
  const list = store.activeProjects;
  if (!list.length) return;
  const menu = document.getElementById('proj-menu');
  menu.innerHTML = list.map((p) => `
    <div class="pm-item" role="option" data-id="${p.id}"
         aria-selected="${p.id === store.currentId}">
      <span class="pm-tick">${p.id === store.currentId ? TICK : ''}</span>
      <span class="pm-name">${escapeHtml(p.name)}</span>
      <span class="pm-meta">${p.episode_count} 集 · ${p.shot_count} 镜 · ${p.asset_count} 资产</span>
    </div>`).join('');
  const items = pmItems();
  pmIndex = Math.max(0, items.findIndex((el) => el.dataset.id === String(store.currentId)));
  items[pmIndex]?.classList.add('on');
  menu.hidden = false;
  document.getElementById('proj-pick').setAttribute('aria-expanded', 'true');
  document.getElementById('proj-pick').classList.add('open');
}

function closeProjMenu(returnFocus) {
  const menu = document.getElementById('proj-menu');
  if (menu.hidden) return;
  menu.hidden = true;
  pmItems().forEach((el) => el.classList.remove('on'));
  const btn = document.getElementById('proj-pick');
  btn.setAttribute('aria-expanded', 'false');
  btn.classList.remove('open');
  if (returnFocus) btn.focus();
}

function moveProjMenu(step) {
  const items = pmItems();
  if (!items.length) return;
  items[pmIndex]?.classList.remove('on');
  pmIndex = (pmIndex + step + items.length) % items.length;
  items[pmIndex].classList.add('on');
  items[pmIndex].scrollIntoView({ block: 'nearest' });
}

function chooseProject(el) {
  closeProjMenu(true);
  store.select(Number(el.dataset.id));
}

function setupProjPick() {
  const btn = document.getElementById('proj-pick');
  const menu = document.getElementById('proj-menu');

  btn.addEventListener('click', () => {
    menu.hidden ? openProjMenu() : closeProjMenu(true);
  });
  menu.addEventListener('click', (e) => {
    const it = e.target.closest('.pm-item');
    if (it) chooseProject(it);
  });
  // 鼠标划过要跟着挪高亮，否则键盘停在的那行和眼睛看到的不是同一行
  menu.addEventListener('mousemove', (e) => {
    const it = e.target.closest('.pm-item');
    if (!it) return;
    const items = pmItems();
    items[pmIndex]?.classList.remove('on');
    pmIndex = items.indexOf(it);
    it.classList.add('on');
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#proj-pick') && !e.target.closest('#proj-menu')) closeProjMenu(false);
  });
  document.addEventListener('keydown', (e) => {
    if (menu.hidden) return;
    if (e.key === 'Escape') { closeProjMenu(true); return; }
    if (e.key === 'ArrowDown') { e.preventDefault(); moveProjMenu(1); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); moveProjMenu(-1); return; }
    if (e.key === 'Enter') {
      // 必须吃掉默认行为：焦点还在触发按钮上时，Enter 的默认动作是再点一次，
      // 会把刚关掉的菜单又开回来
      e.preventDefault();
      const it = menu.querySelector('.pm-item.on');
      if (it) chooseProject(it);
    }
  });
}

function syncChrome() {
  const cur = store.current;
  const btn = document.getElementById('proj-pick');
  document.getElementById('cur-name').textContent = cur ? cur.name : '未选择项目';
  // 判空看在产的：只剩归档项目时，下拉点开是空的，这里就该提示去项目中心
  const pickable = store.activeProjects.length;
  btn.disabled = pickable === 0;
  btn.title = pickable ? '切换项目' : '没有在产项目，到项目中心新建或恢复';
  document.getElementById('cur-path').textContent = '';
  paintEpisode();
}

/* ---------------- 顶栏当前集（0037） ----------------
 * 与项目切换器同一套做法（自绘菜单、键盘可达、点外面收起），
 * 因为"当前集"和"当前项目"是同一类东西：全站上下文，不是某个页面的局部筛选。
 */

function paintEpisode() {
  const btn = document.getElementById('ep-pick');
  const label = document.getElementById('cur-episode');
  if (!btn || !label) return;
  const ep = store.currentEpisode;
  label.textContent = ep ? `第 ${ep.seq} 集` : '未选集';
  const has = (store.episodes || []).length;
  btn.disabled = !store.currentId || has === 0;
  btn.title = !store.currentId ? '先选一个项目'
    : has === 0 ? '这个项目还没有集，去创作中心建一集'
      : '切换当前集';
}

function closeEpMenu(returnFocus) {
  const menu = document.getElementById('ep-menu');
  const btn = document.getElementById('ep-pick');
  if (!menu || menu.hidden) return;
  menu.hidden = true;
  btn.setAttribute('aria-expanded', 'false');
  if (returnFocus) btn.focus();
}

function openEpMenu() {
  const menu = document.getElementById('ep-menu');
  const btn = document.getElementById('ep-pick');
  const list = store.episodes || [];
  menu.innerHTML = list.map((e) => `
    <div class="pm-item${e.id === store.currentEpisodeId ? ' on' : ''}" role="option"
         data-id="${e.id}" tabindex="-1">第 ${e.seq} 集${e.title ? ' · ' + escapeHtml(e.title) : ''}</div>`).join('')
    || '<div class="pm-empty">这个项目还没有集</div>';
  menu.hidden = false;
  btn.setAttribute('aria-expanded', 'true');
}

function setupEpPick() {
  const btn = document.getElementById('ep-pick');
  const menu = document.getElementById('ep-menu');
  if (!btn || !menu) return;
  btn.addEventListener('click', () => { menu.hidden ? openEpMenu() : closeEpMenu(true); });
  menu.addEventListener('click', (e) => {
    const it = e.target.closest('.pm-item');
    if (!it) return;
    closeEpMenu(true);
    store.selectEpisode(Number(it.dataset.id));
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#ep-pick') && !e.target.closest('#ep-menu')) closeEpMenu(false);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !menu.hidden) closeEpMenu(true);
  });
}

/* ---------------- 轻提示 ----------------
 * 全站共用一条：写回失败、预检阻塞这类"只有一句话"的反馈，
 * 不值得弹一层模态，但也不能什么都不说。
 *
 * 2026-11-09 从"一条自己 fixed 定位的浮层"改成"一个堆栈"，两条硬理由：
 *   ① 原来每条 toast 各自 `position:fixed;right:24px;bottom:24px`，坐标完全
 *      相同。批量操作连失败三次，第 2、3 条直接盖在第 1 条身上——人只看见
 *      最后一句，还以为只错了一次。现在一个容器纵向排，谁也别压谁。
 *   ② 原来恒为 `chip amber` 且 4200ms 一律自动消失。于是"写回失败"和
 *      "记住这一集了"共用一张脸，失败提示还会在人读完之前自己走掉。
 *      现在按 kind 分色；失败类不自动消失，必须点掉。
 *
 * 调用约定：`toast(文案, kind)`，kind ∈ err|warn|ok|info（缺省 info）。
 * 视图照旧只派 `app:toast` 事件，把 kind 放进 detail——util.js 不 import
 * 这个壳（反向依赖会把工具模块和应用壳绑死）。
 */

const TOAST_TONE = { err: 'tone-err', warn: 'tone-warn', ok: 'tone-ok', info: 'tone-info' };
const TOAST_MAX = 4;      // 最多同时挂 4 条，再多先挤掉最老的非失败条目
const TOAST_TTL = 4200;   // 非失败类的自动消失时长
let toastHost = null;

function ensureToastHost() {
  if (toastHost && document.body.contains(toastHost)) return toastHost;
  toastHost = document.createElement('div');
  toastHost.className = 'toast-host';
  toastHost.id = 'toast-host';
  document.body.appendChild(toastHost);
  return toastHost;
}

function toastDismiss(el) {
  if (!el || el.dataset.leaving || !el.isConnected) return;
  el.dataset.leaving = '1';
  if (el.dataset.timer) clearTimeout(Number(el.dataset.timer));
  el.classList.add('toast-out');
  setTimeout(() => el.remove(), 160);
}

function toastArmTimer(el) {
  el.dataset.timer = String(setTimeout(() => toastDismiss(el), TOAST_TTL));
}

function toastTrim(host) {
  const items = [...host.querySelectorAll('.toast-item')];
  while (items.length > TOAST_MAX) {
    // 先挤最老的非失败条目；清一色失败条目时再挤最老那条
    const idx = items.findIndex((el) => el.dataset.kind !== 'err');
    const victim = items.splice(idx >= 0 ? idx : 0, 1)[0];
    if (victim) victim.remove();
  }
}

/** 读屏念轻提示时的语气前缀。
 * 只念正文不行："上下文写回失败"和"已保存"在耳朵里必须是两种语气；
 * 前缀就是屏幕上那条 3px 左边条的听觉对应物。 */
function prefixOf(kind) {
  return { err: '出错了：', warn: '提示：', ok: '完成：', info: '' }[kind] || '';
}

function toast(text, kind = 'info') {
  const body = String(text || '').trim();
  if (!body) return;
  const k = TOAST_TONE[kind] ? kind : 'info';
  const host = ensureToastHost();

  // 同文案合并计数：批量失败的 5 条原因常常一字不差——那不是 5 个问题，
  // 是同一个问题发生了 5 次。合并成一条 ×5 比并排挂 5 张一样的脸好读。
  for (const el of host.querySelectorAll('.toast-item')) {
    if (el.dataset.kind !== k || el.dataset.text !== body) continue;
    const n = Number(el.dataset.count || 1) + 1;
    el.dataset.count = String(n);
    const badge = el.querySelector('.toast-count');
    if (badge) { badge.textContent = `x${n}`; badge.hidden = n < 2; }
    if (el.dataset.timer) clearTimeout(Number(el.dataset.timer));
    delete el.dataset.timer;
    if (k !== 'err') toastArmTimer(el);
    // 合并了也要出声：屏幕上数字从 x1 变 x2，读屏若不说话，
    // 用耳朵的人会以为只发生了一次。这里**强制**重播，
    // 因为同文同义、普通去重会把它当重复吞掉。
    announce(`${prefixOf(k)}${body}（第 ${n} 次）`, true);
    return;
  }

  const el = document.createElement('div');
  el.className = `toast-item ${TOAST_TONE[k]}`;
  el.dataset.kind = k;
  el.dataset.text = body;
  el.dataset.count = '1';
  el.innerHTML = '<span class="toast-text"></span>'
    + '<span class="toast-count" hidden></span>'
    + '<button class="toast-x" type="button" aria-label="关闭">&times;</button>';
  el.querySelector('.toast-text').textContent = body;   // 调用方给什么显示什么
  el.addEventListener('click', (e) => {
    if (e.target.closest('.toast-x')) toastDismiss(el);
  });
  host.appendChild(el);
  toastTrim(host);
  if (k !== 'err') toastArmTimer(el);   // 失败不自动消失：人得有机会读完

  // 读屏：轻提示是**新增信息**，得念出来。
  announce(prefixOf(k) + body, k === 'err');   // 失败要压过正在念的内容，force 重播
}

/* ---------------- 登录闸门 ----------------
 * /api/auth/me 返回 401 就把整屏盖住，登录成功后同一份 DOM 继续 boot，
 * 不跳转页面（省一次整页重载，也不会丢掉已渲染的壳）。
 * appStarted 用来区分"还没进应用"和"已经在用、从菜单里改密"：
 * 后者改完只需刷新当前视图。
 */

let lastLoginPassword = '';   // 强制改密时拿它当"原密码"，省得再输一遍
let appStarted = false;

function showGate(mode, note = '') {
  const gate = document.getElementById('gate');
  const login = document.getElementById('gate-login');
  const pwd = document.getElementById('gate-password');
  // 强制改密只有在"刚登录过"时才能省掉原密码框；刷新页面后重进就没这条线索了
  const needOld = mode === 'voluntary' || (mode === 'force' && !lastLoginPassword);

  document.getElementById('gate-err').hidden = true;
  document.getElementById('gate-perr').hidden = true;
  login.hidden = mode !== 'login';
  pwd.hidden = mode === 'login';
  document.getElementById('gate-old-wrap').hidden = !needOld;
  document.getElementById('gate-pcancel').hidden = mode !== 'voluntary';
  // 说明放在哪一块取决于当前是哪一屏：登录那一屏原来没有可放话的地方
  const loginNote = document.getElementById('gate-login-note');
  loginNote.hidden = !(mode === 'login' && note);
  if (loginNote.hidden) loginNote.textContent = ''; else loginNote.textContent = note;
  document.getElementById('gate-note').textContent = mode === 'login' ? '' : note;
  gate.hidden = false;
  (mode === 'login' ? document.getElementById('gate-user') : document.getElementById('gate-new')).focus();
}

function hideGate() {
  document.getElementById('gate').hidden = true;
  ['gate-pass', 'gate-old', 'gate-new', 'gate-new2']
    .forEach((id) => { document.getElementById(id).value = ''; });
}

/** 让登录卡抖一下。先移除再强制重排，否则连续输错只有第一次会动 */
function shakeGate() {
  const card = document.querySelector('.gate-card');
  card.classList.remove('shake');
  void card.offsetWidth;
  card.classList.add('shake');
}

function renderUserChip() {
  const u = store.user;
  if (!u) return;
  const btn = document.getElementById('user-btn');
  btn.textContent = (u.display_name || u.username).slice(0, 1);
  btn.title = `${u.display_name}（${u.username}）`;
}

async function submitLogin() {
  const userEl = document.getElementById('gate-user');
  const passEl = document.getElementById('gate-pass');
  const errEl = document.getElementById('gate-err');
  const btn = document.getElementById('gate-submit');
  errEl.hidden = true;
  btn.disabled = true;
  try {
    const me = await api.login(userEl.value.trim(), passEl.value);
    lastLoginPassword = passEl.value;
    store.setSession(me);
  } catch (err) {
    errEl.textContent = err.message || '登录失败';
    errEl.hidden = false;
    btn.disabled = false;
    shakeGate();
    return;
  }
  btn.disabled = false;
  renderUserChip();

  if (store.user.must_change_password) {
    showGate('force', `${store.user.display_name}，这是第一次登录，先设一个新密码。`);
    return;
  }
  hideGate();
  if (appStarted) window.dispatchEvent(new CustomEvent('app:refresh'));
  else await startApp();
}

async function submitChangePassword() {
  const oldEl = document.getElementById('gate-old');
  const newEl = document.getElementById('gate-new');
  const new2El = document.getElementById('gate-new2');
  const errEl = document.getElementById('gate-perr');
  const btn = document.getElementById('gate-psave');
  const needsOld = !document.getElementById('gate-old-wrap').hidden;

  errEl.hidden = true;
  if (newEl.value.length < 6) {
    errEl.textContent = '新密码至少 6 位';
    errEl.hidden = false;
    shakeGate();
    return;
  }
  if (newEl.value !== new2El.value) {
    errEl.textContent = '两次输入的新密码不一样';
    errEl.hidden = false;
    shakeGate();
    return;
  }

  btn.disabled = true;
  try {
    await api.changePassword(needsOld ? oldEl.value : lastLoginPassword, newEl.value);
    // 服务端改密会把该用户的所有会话作废（含这一次），所以拿新口令重登一次
    const me = await api.login(store.user.username, newEl.value);
    lastLoginPassword = newEl.value;
    store.setSession(me);
  } catch (err) {
    errEl.textContent = err.message || '改密失败';
    errEl.hidden = false;
    btn.disabled = false;
    shakeGate();
    return;
  }
  btn.disabled = false;
  hideGate();
  if (appStarted) window.dispatchEvent(new CustomEvent('app:refresh'));
  else await startApp();
}

function setupGate() {
  document.getElementById('gate-form').addEventListener('submit', (e) => {
    e.preventDefault();
    if (!document.getElementById('gate-login').hidden) submitLogin();
    else submitChangePassword();
  });
  // 只有"主动改密"才给取消——强制改密没有退路，取消就得回到登录
  document.getElementById('gate-pcancel').addEventListener('click', hideGate);
}

function bindUserMenu() {
  const btn = document.getElementById('user-btn');
  const menu = document.getElementById('user-menu');
  const close = () => {
    menu.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
  };

  const renderMenu = () => {
    const u = store.user;
    if (!u) return;
    menu.innerHTML = `
      <div class="pm-item" role="menuitem" data-act="pwd">
        <span class="pm-tick"></span>
        <span class="pm-name">修改密码</span>
        <span class="pm-meta">${escapeHtml(u.username)}</span>
      </div>
      <div class="pm-item" role="menuitem" data-act="out">
        <span class="pm-tick"></span>
        <span class="pm-name">退出登录</span>
        <span class="pm-meta"></span>
      </div>`;
  };

  btn.addEventListener('click', () => {
    if (menu.hidden) {
      renderMenu();
      menu.hidden = false;
      btn.setAttribute('aria-expanded', 'true');
    } else close();
  });

  menu.addEventListener('click', async (e) => {
    const item = e.target.closest('.pm-item');
    if (!item) return;
    close();
    if (item.dataset.act === 'pwd') {
      showGate('voluntary', '改完密码会退出所有端，需要重新登录。');
      return;
    }
    // 退出：服务端删会话，本地也清干净，然后整页重来一遍最省心
    try { await api.logout(); } catch { /* 会话本来就过期了也无所谓 */ }
    store.clearSession();
    location.reload();
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('#user-btn') && !e.target.closest('#user-menu')) close();
  });
  menu.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
}

/* ---------------- 命令面板 ---------------- */

function setupPalette() {
  const pal = document.getElementById('palette');
  const input = document.getElementById('palette-input');
  const list = document.getElementById('palette-list');
  let shown = [];
  let index = 0;

  const render = (q = '') => {
    const key = q.trim();
    shown = MODULES.filter((m) => !m.hidden && allowed(m) && m.label.includes(key));
    index = 0;
    list.innerHTML = shown.length
      ? shown.map((m, i) =>
          `<div class="pal-item${i === 0 ? ' cur' : ''}" data-v="${m.id}">${m.label}</div>`).join('')
      : '<div class="pal-empty">没有匹配的模块</div>';
  };
  const move = (step) => {
    if (!shown.length) return;
    const items = list.querySelectorAll('.pal-item');
    items[index]?.classList.remove('cur');
    index = (index + step + shown.length) % shown.length;
    items[index]?.classList.add('cur');
    items[index]?.scrollIntoView({ block: 'nearest' });
  };
  const open = () => { pal.hidden = false; input.value = ''; render(); input.focus(); };
  const close = () => { pal.hidden = true; };

  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      pal.hidden ? open() : close();
      return;
    }
    if (pal.hidden) return;
    if (e.key === 'Escape') close();
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
    if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    if (e.key === 'Enter' && shown[index]) {
      goView(shown[index].id);
      close();
    }
  });
  input.addEventListener('input', () => render(input.value));
  pal.addEventListener('click', (e) => { if (e.target === pal) close(); });
  list.addEventListener('click', (e) => {
    const it = e.target.closest('.pal-item');
    if (it) { goView(it.dataset.v); close(); }
  });
}

/* ---------------- 白天 / 夜间 ----------------
 * 首屏用哪套由 index.html 里那段前置脚本决定（模块脚本要等整页解析完才跑，
 * 等它再设就会先按默认主题画一帧），这里只接手动态切换，所以不再自己判时间。
 * localStorage 的键名 'fv.theme' 和那段前置脚本是同一个，改一处要改两处。
 */

const THEME_KEY = 'fv.theme';

function paintThemeButtons(theme) {
  const now = theme === 'night' ? '夜间（暗金片场）' : '白天（极光玻璃）';
  const to = theme === 'night' ? '白天' : '夜间';
  document.querySelectorAll('[data-theme-toggle]').forEach((btn) => {
    const text = `当前：${now} · 点击切到${to}`;
    btn.title = text;
    btn.setAttribute('aria-label', text);
  });
}

function setupTheme() {
  const root = document.documentElement;
  paintThemeButtons(root.dataset.theme || 'day');
  document.querySelectorAll('[data-theme-toggle]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const next = root.dataset.theme === 'night' ? 'day' : 'night';
      root.dataset.theme = next;
      // 夜间的原生 select 弹层是系统菜单，深色主题管不到它；切过去就把下拉换成自绘的。
      if (next === 'night') applyPicks(); else restorePicks();
      // 手动选过一次，就别再跟着时钟走：他要的不是"按时间"
      try { localStorage.setItem(THEME_KEY, next); } catch { /* 禁用存储时切完这次仍然生效 */ }
      paintThemeButtons(next);
    });
  });
}

/**
 * 视图每次重绘、弹窗每次填充都会换一批 select；按 DOM 变化补增强。
 * 80ms 去抖，且 `applyPicks` 只处理没标记过的节点，所以不会自己触发自己。
 */
let pickTimer = 0;
function watchPicks() {
  new MutationObserver(() => {
    clearTimeout(pickTimer);
    pickTimer = setTimeout(() => applyPicks(), 80);
  }).observe(document.body, { childList: true, subtree: true });
}

/* ---------------- 启动 ---------------- */

function renderBackendDown(err) {
  // 后端没起来时给一句人话，别让页面白屏
  document.getElementById('view-root').innerHTML =
    `<section class="view on"><div class="card"><div class="empty">
       <div class="empty-t">连不上后端</div>
       <p>${escapeHtml(err.message)}</p>
       <p class="hint">先在 backend 目录启动：uvicorn main:app --reload</p>
     </div></div></section>`;
}

async function startApp() {
  try {
    await store.load();
  } catch (err) {
    renderBackendDown(err);
    return;
  }

  appStarted = true;
  // 铃铛在 boot 里挂过一次（未登录也要在），这里再挂一次是**幂等**的：
  // 节点委托有 __bellBound 判重，轮询与 document 监听每次先收再挂。
  // 需要这一次是因为会话过期那条路把它收掉了，而重登只走 startApp，
  // 不再经过 boot——不补这一下，过期重登后角标就永远不动了。
  mountNotifications();
  await store.loadWorker();
  paintWorker();
  // 0037：当前集是服务端上下文，不是浏览器缓存——启动时读一次，
  // 之后顶栏、工作台、成片都按它算
  await store.loadContext();
  buildNav();
  syncChrome();
  // 地址有三段：#projects（模块）、#assets/1（模块＋二级标签）、
  // #assets/1/a57（再带一个详情参数＝看第 57 号资产）。
  // 直接粘一个没权限的模块地址时退回首页，别渲染出一个满屏 403 的页面
  const [startView, startSub, startDeep] = (location.hash.replace('#', '') || (store.can('project:read') ? 'production' : 'overview')).split('/');
  const entry = MODULES.find((m) => m.id === startView);
  const view = (entry && !allowed(entry)) ? 'overview' : startView;
  if (startSub !== undefined && startSub !== '') setSub(view, Number(startSub));
  // a57 这种写法：只有数字部分认，别的都当没给（粘错地址不该渲染出一个空详情壳）
  if (view === startView && /^a\d+$/.test(startDeep || '')) {
    setParam(view, Number(startDeep.slice(1)));
  }
  await renderView(view);
}

async function boot() {
  setupTheme();       // 要在问身份之前：未登录时能看见的只有闸门里那枚切换钮
  // 通知铃铛（0049）：DOM 就绪就能挂；未登录时接口 401，refresh 内部 catch
  // 掉、不显示角标——所以不必等身份回来。
  mountNotifications();
  watchPicks();
  bindNav();
  setupProjPick();
  setupEpPick();
  setupPalette();
  setupGate();
  bindUserMenu();

  // 视图自己的 render 可能改当前项目（项目中心进入时会重拉列表，当前项被删或归档
  // 就退回在产第一个）。所以渲染完要再同步一次顶栏——先同步的那次拿的是旧状态，
  // 会出现"列表里已经没有这个项目、顶栏还写着它"。
  window.addEventListener('app:go', (e) => goView(e.detail));
  // 轻提示的统一出口：视图只派事件，不各自攒一个浮层
  window.addEventListener('app:toast', (e) => toast(e.detail?.text || '', e.detail?.kind || 'info'));
  let refreshTimer = 0;
  window.addEventListener('app:refresh', () => {
    // 一次操作常连发两三条（保存 + 刷新列表 + 切集）。合并成一次重绘，
    // 既少闪一下，也不让刚恢复的焦点被下一次重绘再冲掉。
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(async () => {
      syncChrome();
      await goView(currentViewId() || 'overview');
    }, 80);
  });
  // 会话过期的出口（W02-a）：api.js 认到 401 就派这条。原来只有启动那一次
  // 会去挂登录闸门，之后过期只会弹一句"失败：未登录"，人没地方重登。
  // 闸门已经开着就别重复 focus——会把人正在输入的口令框清空焦点。
  window.addEventListener('app:unauthorized', () => {
    if (!document.getElementById('gate').hidden) return;
    // 会话过期与登出不同：登出是 location.reload()，进程里的一切自然没了；
    // 这条路只是把闸门盖上，页面还活着。不在这儿收掉通知轮询，它就会
    // 每 20 秒再吃一个 401、再派一次 app:unauthorized，一直转到人重新登录
    // 或关掉标签页（2026-11-11 审计 F-07 实测：过期后 22 秒仍有 1 次请求）。
    appStarted = false;      // worker 心跳那两条回调也据此停下
    stopNotifications();
    showGate('login', '登录会话已过期（默认 12 小时）。重新登录后回到原来这一页。');
  });

  // 先问身份：401 就去登录，其余错误当后端没起来处理
  try {
    await store.loadMe();
  } catch (err) {
    if (err.status === 401) {
      showGate('login');
      return;
    }
    renderBackendDown(err);
    return;
  }

  renderUserChip();
  if (store.user.must_change_password) {
    showGate('force', `${store.user.display_name}，先设一个新密码再进。`);
    return;
  }
  await startApp();
  // 20 秒刷一次就够了：这不是实时看板，只是让人知道队列还有没有人在推
  // 页面不可见时停掉：后台标签页没有"让人知道"的对象，白发的请求只会
  // 让 worker_status 的查询（队列聚合 + worker 心跳镜像）整夜空转。
  // visibilitychange 回来时立即补一次，不让人回来先看 20 秒前的旧数。
  setInterval(async () => {
    if (!appStarted || document.hidden) return;
    await store.loadWorker();
    paintWorker();
  }, 20000);
  document.addEventListener('visibilitychange', async () => {
    if (!appStarted || document.hidden) return;
    await store.loadWorker();
    paintWorker();
  });
}

boot();
