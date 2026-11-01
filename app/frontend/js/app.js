/**
 * 应用入口：侧栏、命令面板、项目选择器、启动流程。
 * 页面内容全部由 router + views 负责，这里只管壳。
 */
import { MODULES } from './modules.js';
import { store } from './store.js';
import { api } from './api.js';
import { renderView, currentViewId } from './router.js';
import { setParam, setSub } from './navstate.js';
import { escapeHtml } from './util.js';

/* ---------------- 侧栏 ---------------- */

function allowed(entry) {
  // perms 是"任一即可"；没有 perms 的模块人人可见
  return !entry?.perms || entry.perms.some((code) => store.can(code));
}

function buildNav() {
  const nav = document.getElementById('nav');
  // 没权限的模块连入口都不给（真正的拦截在服务端，这里只是别让人白点）
  const visible = MODULES.filter(allowed);
  nav.innerHTML = visible.map((m) => {
    const item = `<div class="nav-item" data-v="${m.id}"><span class="nav-ico" aria-hidden="true"><svg><use href="#${m.icon}"/></svg></span>${m.label}</div>`;
    if (!m.subs.length) return item;
    const subs = m.subs
      .map((s, i) => `<span class="sub${i === 0 ? ' on' : ''}">${s}</span>`).join('');
    const cls = m.utility ? 'nav-group utility' : 'nav-group';
    return `<div class="${cls}">${item}<div class="nav-sub">${subs}</div></div>`;
  }).join('');
}

function bindNav() {
  document.getElementById('nav').addEventListener('click', (e) => {
    const sub = e.target.closest('.sub');
    if (sub) {
      const group = sub.parentElement;
      const index = [...group.querySelectorAll('.sub')].indexOf(sub);
      const viewId = sub.closest('.nav-group').querySelector('.nav-item').dataset.v;
      group.querySelectorAll('.sub').forEach((x) => x.classList.remove('on'));
      sub.classList.add('on');
      setSub(viewId, index);
      // 点侧栏（含二级标签）＝回列表：详情参数不清掉的话，
      // 从资产详情点"资产中心"会停在原来那条资产上，看着像侧栏没反应
      setParam(viewId, undefined);
      renderView(viewId);
      return;
    }
    const item = e.target.closest('.nav-item');
    if (item) { setParam(item.dataset.v, undefined); renderView(item.dataset.v); }
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
  document.getElementById('gate-note').textContent = note;
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
    shown = MODULES.filter((m) => allowed(m) && m.label.includes(key));
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
      renderView(shown[index].id);
      close();
    }
  });
  input.addEventListener('input', () => render(input.value));
  pal.addEventListener('click', (e) => { if (e.target === pal) close(); });
  list.addEventListener('click', (e) => {
    const it = e.target.closest('.pal-item');
    if (it) { renderView(it.dataset.v); close(); }
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
      // 手动选过一次，就别再跟着时钟走：他要的不是"按时间"
      try { localStorage.setItem(THEME_KEY, next); } catch { /* 禁用存储时切完这次仍然生效 */ }
      paintThemeButtons(next);
    });
  });
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
  buildNav();
  syncChrome();
  // 地址有三段：#projects（模块）、#assets/1（模块＋二级标签）、
  // #assets/1/a57（再带一个详情参数＝看第 57 号资产）。
  // 直接粘一个没权限的模块地址时退回首页，别渲染出一个满屏 403 的页面
  const [startView, startSub, startDeep] = (location.hash.replace('#', '') || 'overview').split('/');
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
  bindNav();
  setupProjPick();
  setupPalette();
  setupGate();
  bindUserMenu();

  window.addEventListener('app:go', (e) => renderView(e.detail));
  window.addEventListener('app:refresh', () => {
    syncChrome();
    renderView(currentViewId() || 'overview');
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
}

boot();
