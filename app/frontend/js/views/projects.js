import { store } from '../store.js';
import { api } from '../api.js';
import { bindModalDismiss, confirmDanger, escapeHtml, formatMoney, run } from '../util.js';

/**
 * 项目中心只管 Project 这一层：一个页面同时承担检索与增删改。
 * 集与分镜的东家是创作中心，成本账的东家是生成中心，这里一律只读（设计稿 §3.2）。
 * 新建收进弹窗（含指定协作人），主页面只留"筛选 + 一张表"，不再挂一整块表单。
 * 列表的筛选是纯前端做的：项目就几十个，不值为它再跑一趟接口。
 */
let activeTag = '';
// 状态筛选默认"在产"——归档的意思就是不挡在眼前。三档：active / archived / all
let activeStatus = 'active';
// 搜索词放模块级：批量操作完、切项目后视图都会重画，词丢了人只会以为"筛坏了"
let keyword = '';
// 勾选的项目 id。批量操作完要留着，所以放模块级；每次 render 按现存项目剪掉一次
const picked = new Set();
// 排序方式。默认"最近打开"就是服务端给的顺序；另两种在本地重排行，不动筛选状态
let sortMode = 'opened';

// 库里存的是 datetime('now','localtime') 出来的 'YYYY-MM-DD HH:MM:SS'。
// 创建和最后修改两列必须走同一个截法：一个截 10 位、一个截 16 位，
// 同一行摆出两种精度，看着就像两个不同的时间类型。改口径只改这一行。
const stamp = (t) => t.slice(0, 16);

// 一次性提示（新建成功、归档、批量结果）。渲染会重画整页，所以它必须存在模块里
// 让下一次 render 带出来——直接写进当前 DOM 的话，紧接着的 refresh 就把它冲掉了。
let flash = '';

// ⋯ 菜单是 fixed 浮层：页面一滚、表格一横滚、窗口一缩放，它就不再指着原来那一行。
// 与其让它跟着跳（横滚时一路抖），不如收起并把焦点还给触发它的 ⋯。
// 监听挂在 window 上，而视图每次 render 都会重建闭包，所以这里只留"当前该叫谁"
// 一个槽位：触发即摘监听，切页留下的旧闭包不会攒成一堆孤儿监听。
let onViewportMove = null;

function dismissRowMenu() {
  const close = onViewportMove;
  disarmRowMenu();
  if (close) close();
}

function armRowMenu(close) {
  onViewportMove = close;
  window.addEventListener('scroll', dismissRowMenu, true);   // 捕获：表格内部横滚也算
  window.addEventListener('resize', dismissRowMenu);
}

function disarmRowMenu() {
  onViewportMove = null;
  window.removeEventListener('scroll', dismissRowMenu, true);
  window.removeEventListener('resize', dismissRowMenu);
}

// 菜单与视口之间留的缝：贴边不留零，不然看着像被裁了一半
const MENU_EDGE = 8;
const MENU_GAP = 4;

const STATUS_LABELS = { active: '在产', archived: '已归档', all: '全部' };
const BATCH_DELETE_WORD = '批量删除';
const MAX_TAGS = 10;
const MAX_TAG_LEN = 20;

// 标签的归一化与 services.clean_tags 同一条口径：去空白、去重、每个 20 字、最多 10 个。
// 前端先做一遍不是为了替服务端把关，而是让弹窗里写的东西和存下去的东西一致——
// 服务端超出会静默截掉，界面若原样显示"12 个标签"，就是在骗人。
function normTags(raw) {
  const seen = [];
  let cut = false;
  raw.split(/[,，]/).map((t) => t.trim()).filter(Boolean).forEach((t) => {
    const tag = t.slice(0, MAX_TAG_LEN);
    if (tag !== t) cut = true;
    if (!seen.includes(tag)) seen.push(tag);
  });
  if (seen.length > MAX_TAGS) cut = true;
  return { tags: seen.slice(0, MAX_TAGS), cut };
}

// 标签一格最多摆两个，其余折叠成 +N。项目中心那列本来就窄，
// 七八个标签横着铺会把"内容"挤成两行，整张表的高度就不齐了。
function tagCell(tags) {
  if (!tags.length) return '<span class="t-dim">—</span>';
  const shown = tags.slice(0, 2)
    .map((t) => `<span class="chip plain">${escapeHtml(t)}</span>`).join(' ');
  const rest = tags.length - 2;
  const more = rest > 0
    ? `<span class="chip gray" title="${escapeHtml(tags.slice(2).join('、'))}">+${rest}</span>` : '';
  return shown + (more ? ' ' + more : '');
}


// 筛选 chip 用真 button：span 挂个手型光标 Tab 停不住、Enter 也没反应，
// 行框都能键盘操作了，chip 不能说不通。外观还是 .chip 那套胶囊（见 CSS）
function tagChips(tags, active) {
  return tags.map((t) => `
    <button type="button" class="chip ${t === active ? 'blue' : 'plain'}"
            data-tag="${escapeHtml(t)}">${escapeHtml(t)}</button>`).join('');
}

function statusChips(current) {
  return Object.entries(STATUS_LABELS).map(([key, label]) => `
    <button type="button" class="chip ${key === current ? 'blue' : 'plain'}"
            data-status="${key}">${label}</button>`).join('');
}

/** 筛选条整段。点 chip 后只重画这一段——不重建表格，搜索框里的词也就不会被洗掉 */
function filtersHtml(allTags) {
  return `
    <span class="note" style="margin-right:4px">状态</span>${statusChips(activeStatus)}
    <span class="note" style="margin-left:14px;margin-right:4px">标签</span>
    ${allTags.length ? tagChips(allTags, activeTag)
      : '<span class="t-dim">还没有标签 —— 用「修改」给项目加，加完这里就能点着筛</span>'}
    ${activeTag ? '<button type="button" class="chip gray" data-tag="">清除</button>' : ''}`;
}

function rowsFor(projects) {
  return projects.map((p) => {
    // 权限只决定按钮显不显示；点下去的拦截在服务端
    const canWrite = store.can('project:write');
    const canDelete = store.can('project:delete');
    // 删项目与管协作人只归所有人（与管理员），协作人只能改和看
    const canOwn = (row) => store.isManager || row.owner_id === store.user?.id;
    const archived = p.status === 'archived';
    // 成本账的明细在生成中心，这里只摆按币种的只读累计。不在这里做加总，
    // 也不给编辑入口——两处各算一遍，迟早对不上（设计稿 §3.2）。
    const costHtml = (p.costs || []).length
      ? `<span class="t-desc cost-inline">成本 ${p.costs.map((c) =>
        `${formatMoney(c.total_micros, c.currency)}${c.pending ? `（${c.pending} 笔待核对）` : ''}`
      ).join(' · ')}</span>`
      : '';
    // 进度由权威数据现算：episode_done / episode_count 都来自 episode.status，
    // 这里只画，不存。没有集的项目显示"还没有集"，不摆一条 0% 的空条。
    const progress = p.episode_count
      ? (() => {
        const pct = Math.round((p.episode_done / p.episode_count) * 100);
        return `<span class="prog" title="已定稿 ${p.episode_done}/${p.episode_count} 集">`
          + `<i style="width:${pct}%"></i></span>`
          + `<span class="prog-t">定稿 ${p.episode_done}/${p.episode_count} 集</span>`;
      })()
      : '<span class="t-dim">还没有集</span>';
    // 只能看的人不给复选框：勾出来也没有可做的动作
    const pickCell = canWrite || canDelete
      ? `<td class="c"><input type="checkbox" class="row-pick" data-pick="${p.id}"
              aria-label="选择 ${escapeHtml(p.name)}"${picked.has(p.id) ? ' checked' : ''}></td>`
      : '';
    // 行内只留两个动作：打开（主）＋ ⋯（修改／成员／归档／删除）。
    // 三个管理按钮常驻一列，日常最常用的"继续做这个项目"反而没有位置。
    const menuItems = [
      canWrite ? '<button type="button" role="menuitem" data-act="edit">修改项目</button>' : '',
      canOwn(p) ? '<button type="button" role="menuitem" data-act="members">成员与所有权</button>' : '',
      canWrite ? `<button type="button" role="menuitem" data-act="${archived ? 'restore' : 'archive'}">
        ${archived ? '恢复到在产' : '归档（数据保留）'}</button>` : '',
      canDelete && canOwn(p) ? '<button type="button" role="menuitem" class="danger" data-act="delete">永久删除…</button>' : '',
    ].filter(Boolean).join('');
    return `
      <tr data-id="${p.id}" data-name="${escapeHtml(p.name)}" data-status="${p.status}"
          data-tags="${escapeHtml(JSON.stringify(p.tags))}"
          data-opened="${escapeHtml(p.last_opened_at || '')}"
          data-updated="${escapeHtml(p.updated_at)}"
          data-created="${escapeHtml(p.created_at)}"
          class="${p.id === store.currentId ? 'sel ' : ''}${archived ? 'arch' : ''}">
        ${pickCell}
        <td class="proj-main">
          <button type="button" class="proj-open" data-open="${p.id}"
                  title="设为当前项目并进入创作中心">${escapeHtml(p.name)}</button>
          ${p.id === store.currentId ? '<span class="chip blue now">当前</span>' : ''}
          ${archived ? '<span class="chip gray">已归档</span>' : ''}
          <span class="t-desc">所有人 ${escapeHtml(p.owner_name || '—')}</span>
          ${p.description
            ? `<span class="t-desc proj-desc" title="${escapeHtml(p.description)}">${escapeHtml(p.description)}</span>`
            : ''}
        </td>
        <td class="tagcell">${tagCell(p.tags)}</td>
        <td class="proj-content">
          <span class="t-dim">${p.episode_count} 集 · ${p.shot_count} 镜 · ${p.asset_count} 资产</span>
          <span class="proj-prog">${progress}</span>
          ${costHtml}
        </td>
        <!-- 最近活动：只有时间是真的（列表按最后打开排，最后修改是内容真动过）。
             没有可靠的活动对象数据，就不编"最近改了哪份文档" -->
        <td class="proj-activity" title="创建于 ${stamp(p.created_at)}">
          <span>${p.last_opened_at ? '最后打开 ' + stamp(p.last_opened_at) : '还没打开过'}</span>
          <span class="t-dim">最后修改 ${stamp(p.updated_at)}</span>
        </td>
        <td class="r proj-actions">
          <button type="button" class="btn sm" data-open="${p.id}">打开</button>
          ${menuItems ? `<span class="more-wrap">
            <button type="button" class="btn sm more" data-more="${p.id}"
                    aria-haspopup="menu" aria-expanded="false"
                    aria-label="更多操作：${escapeHtml(p.name)}">⋯</button>
            <!-- 菜单内容跟行一起渲染、藏在这里，点开时整块搬进浮层。
                 直接放在单元格里会被 .table-wrap 的 overflow 裁掉 -->
            <span class="row-menu-src" hidden>${menuItems}</span>
          </span>` : ''}
        </td>
      </tr>`;
  }).join('');
}

export default {
  async render() {
    // 每次进项目中心都重拉一次：集/镜/资产的计数和成本累计都会变，
    // 只靠启动那一份快照，生成中心跑完几条作业再回到这里，看到的还是旧账。
    try {
      await store.load();
    } catch { /* 读不到就先用上一次的列表，别把整页打空 */ }
    const projects = store.projects;
    const cur = store.current;
    const flashText = flash;   // 只带出一次：下一轮渲染就清掉
    flash = '';
    const canWrite = store.can('project:write');
    const canDelete = store.can('project:delete');

    const allTags = [...new Set(projects.flatMap((p) => p.tags))].sort();
    // 选中的标签可能随着项目删光而消失；不清掉就会筛成一片空白，还看不出是为什么
    if (activeTag && !allTags.includes(activeTag)) activeTag = '';
    // 勾过的项目可能已经被删掉了，剪掉再算，免得批量条上显示一个数、实际能操作的另一个数
    [...picked].forEach((id) => { if (!projects.some((p) => p.id === id)) picked.delete(id); });
    const pickable = canWrite || canDelete;
    // 三个数都从现有接口字段现算，不新增统计表：
    // 在产/已归档看 project.status；"待核对账单"看 cost_summary 给的 pending 笔数。
    const activeCount = projects.filter((p) => p.status !== 'archived').length;
    const archivedCount = projects.filter((p) => p.status === 'archived').length;
    const pendingProjects = projects
      .filter((p) => (p.costs || []).some((c) => (c.pending || 0) > 0)).length;

    return `
    <section class="view on">
      <div class="page-head">
        <h1>项目中心</h1>
        <p>管理项目、查看进展、继续制作。集与分镜在创作中心，花费在生成中心</p>
        <div class="right">
          ${canWrite ? '<button class="btn primary" id="btn-new-project">新建项目</button>' : ''}
          <button class="btn" id="btn-goto-writing" ${cur ? '' : 'hidden'}>去创作中心</button>
        </div>
      </div>

      <div class="proj-stats">
        <div class="stat"><span class="stat-n">${activeCount}</span><span class="stat-l">在产项目</span></div>
        <div class="stat"><span class="stat-n">${archivedCount}</span><span class="stat-l">已归档</span></div>
        <div class="stat ${pendingProjects ? 'warn' : ''}"><span class="stat-n">${pendingProjects}</span><span class="stat-l">有待核对账单的项目</span></div>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>项目</h2><span class="sub" id="proj-count">共 ${projects.length} 个</span>
          <div class="right">
            <select id="proj-sort" class="ctl sm proj-sort" aria-label="排序方式">
              <option value="opened">最近打开</option>
              <option value="updated">最近修改</option>
              <option value="name">项目名称</option>
            </select>
            <input id="proj-search" class="ctl sm" placeholder="搜项目名或标签"
                   style="width:200px" value="${escapeHtml(keyword)}">
          </div>
        </div>
        <div class="card-body" style="padding:10px 14px;border-bottom:1px solid var(--line)">
          <div class="row" id="proj-filters" style="gap:6px;flex-wrap:wrap">
            ${filtersHtml(allTags)}
          </div>
        </div>
        <p class="proj-msg" id="proj-msg" ${flashText ? '' : 'hidden'}>${escapeHtml(flashText)}</p>
        ${pickable ? '<div class="batch-bar" id="batch-bar" hidden></div>' : ''}
        ${projects.length ? `
        <div class="table-wrap">
          <table>
          <thead><tr>${pickable ? `<th class="c"><input type="checkbox" class="row-pick" id="pick-all"
          aria-label="全选当前筛选结果"></th>` : ''}<th>项目</th><th>标签</th><th>内容</th><th>最近活动</th><th class="r">操作</th></tr></thead>
          <tbody id="proj-rows">${rowsFor(projects)}</tbody>
          </table>
        </div>
        <div class="empty" id="no-match" hidden style="padding:26px 20px">
          <div class="empty-t">没有匹配的项目</div>
          <p id="no-match-hint">换个关键词或清掉标签筛选。</p>
        </div>` : `
        <div class="empty">
          <div class="empty-t">还没有项目</div>
          <p>${canWrite ? '右上角「新建项目」建第一个。' : '等项目所有人把你加为协作人。'}</p>
        </div>`}
      </div>

      <!-- 行内"更多"菜单：放 body 级浮层，不放进表格。
           .table-wrap 有 overflow-x:auto，绝对定位的子元素会被裁在表格里 -->
      <div class="row-menu" id="row-menu" role="menu" hidden></div>

      <div class="modal" id="new-modal" hidden>
        <div class="modal-box">
          <div class="modal-head neutral">新建项目</div>
          <div class="modal-body">
            <div class="form-grid">
              <label for="np-name" class="top">项目名 <span class="req">必填</span></label>
              <div class="pf-ctl">
                <input id="np-name" placeholder="例如 梦华录" autocomplete="off" maxlength="60">
                <span class="pf-hint-inline">1—60 字，前后空格会自动去掉</span>
              </div>

              <label for="np-tags" class="top">标签</label>
              <div class="pf-ctl">
                <input id="np-tags" placeholder="逗号分隔，例如 神话,短剧">
                <span class="pf-hint-inline">用来检索；最多 10 个、每个 20 字，超出的会截掉</span>
              </div>

              <label for="np-desc" class="top">项目介绍</label>
              <div class="pf-ctl">
                <textarea id="np-desc" maxlength="500" rows="3"
                          placeholder="一两句话说清做什么、给谁看（可留空）"></textarea>
                <span class="pf-hint-inline">可留空，最多 500 字</span>
              </div>

              <!-- 协作人那行不给 .top：下面摆的是一行勾选框（或一句空态），不是文本域，
                   贴顶的 9px 内边距会把字段名压得比自己的内容还低 -->
              <span class="pf-label">协作人</span>
              <div id="np-members" class="role-pick" style="gap:12px 18px"></div>

              <!-- 提示挪进控件列：原来它挂在表单外面，左边界跑到字段名那一栏，
                   跟上面「协作人」的内容对不齐，看着像两列没对齐 -->
              <p class="modal-meta pf-hint">只能邀请与你同级或更低的账号；建完也能在「成员」里加减。</p>
              <p class="modal-meta pf-hint">创建后会设为当前项目，可以直接去创作中心。</p>
            </div>
            <p class="form-err" id="np-err" role="alert" hidden></p>
          </div>
          <div class="modal-actions">
            <button class="btn" id="np-cancel">取消</button>
            <button class="btn primary" id="np-create">新建</button>
          </div>
        </div>
      </div>

      <div class="modal" id="del-modal" hidden>
        <div class="modal-box">
          <div class="modal-head">删除项目</div>
          <div class="modal-body">
            <p class="modal-warn">此操作不可撤销，删掉的内容恢复不了。</p>
            <p id="del-summary">正在清点……</p>
            <label class="modal-label">输入项目名以确认</label>
            <input id="del-confirm" class="ctl" autocomplete="off" placeholder="">
          </div>
          <div class="modal-actions">
            <button class="btn" id="del-cancel">取消</button>
            <button class="btn danger-solid" id="del-ok" disabled>永久删除</button>
          </div>
        </div>
      </div>
      ${pickable ? `
      <div class="modal" id="tag-modal" hidden>
        <div class="modal-box">
          <div class="modal-head neutral">批量加标签</div>
          <div class="modal-body">
            <p id="tag-count">已选 0 个项目</p>
            <label class="modal-label" for="tag-input">要加的标签（逗号分隔）</label>
            <input id="tag-input" class="ctl" autocomplete="off" placeholder="例如 第二季">
            <p class="modal-meta">只加不减：每个项目原有的标签保留；同一个标签不会重复加；
              单个项目最多 10 个标签，满了就不再追加。</p>
          </div>
          <div class="modal-actions">
            <button class="btn" id="tag-cancel">取消</button>
            <button class="btn primary" id="tag-ok">加标签</button>
          </div>
        </div>
      </div>
      <div class="modal" id="batch-del-modal" hidden>
        <div class="modal-box">
          <div class="modal-head">批量删除项目</div>
          <div class="modal-body">
            <p class="modal-warn">此操作不可撤销，删掉的内容恢复不了。</p>
            <p id="bd-list">正在清点……</p>
            <label class="modal-label">输入「${BATCH_DELETE_WORD}」以确认</label>
            <input id="bd-confirm" class="ctl" autocomplete="off" placeholder="${BATCH_DELETE_WORD}">
            <p class="modal-meta">单个删除要输项目名；一次删十几个没法逐个输，
              所以换成一句固定的确认词。不是你名下的项目会被跳过并列出原因。</p>
          </div>
          <div class="modal-actions">
            <button class="btn" id="bd-cancel">取消</button>
            <button class="btn danger-solid" id="bd-ok" disabled>永久删除</button>
          </div>
        </div>
      </div>` : ''}
      <div class="modal" id="edit-modal" hidden>
        <div class="modal-box">
          <div class="modal-head neutral">修改项目</div>
          <div class="modal-body">
            <label class="modal-label" for="ed-name">项目名</label>
            <input id="ed-name" class="ctl" maxlength="60" autocomplete="off">
            <label class="modal-label" for="ed-desc">项目介绍</label>
            <textarea id="ed-desc" class="ctl" rows="4" maxlength="500"
                      placeholder="一两句话说清这个项目做什么、给谁看"></textarea>
            <label class="modal-label" for="ed-tags">标签</label>
            <input id="ed-tags" class="ctl" placeholder="逗号分隔，最多 10 个">
            <p class="modal-meta" id="ed-meta"></p>
            <p class="form-err" id="ed-err" role="alert" hidden></p>
          </div>
          <div class="modal-actions">
            <button class="btn" id="ed-cancel">取消</button>
            <button class="btn primary" id="ed-save">保存</button>
          </div>
        </div>
      </div>
      <div class="modal" id="mem-modal" hidden>
        <div class="modal-box">
          <div class="modal-head neutral">协作人 · <span id="mem-proj"></span></div>
          <div class="modal-body">
            <p class="modal-meta" id="mem-owner"></p>
            <div id="mem-list"></div>
            <div class="row" style="margin-top:14px;gap:8px;flex-wrap:wrap">
              <select id="mem-add" class="ctl" style="min-width:200px"></select>
              <button class="btn" id="mem-add-btn">加为协作人</button>
            </div>
            <!-- 转移所有权：只有所有人与管理员看得见（renderMembers 里判）。
                 放在成员弹窗而不是行内按钮列，是因为它要先看到"能交给谁"这份名单 -->
            <div class="row" id="mem-transfer-row" hidden
                 style="margin-top:10px;gap:8px;flex-wrap:wrap;align-items:center">
              <span class="note">转移给</span>
              <select id="mem-transfer" class="ctl" style="min-width:200px"></select>
              <button class="btn danger-solid" id="mem-transfer-btn">转移所有权</button>
            </div>
            <p class="modal-meta">协作人可以查看和修改这个项目；删项目和管协作人只有所有人能做。
              转移所有权只能交给已加入的协作人，交出去之后你会变成协作人——仍然看得见、改得动，
              只是删项目和管人归对方了。</p>
          </div>
          <div class="modal-actions">
            <button class="btn" id="mem-close">关闭</button>
          </div>
        </div>
      </div>
    </section>`;
  },

  mount(root) {
    const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
    const goWriting = () => window.dispatchEvent(new CustomEvent('app:go', { detail: 'writing' }));
    // 页面级提示（新建成功、归档／恢复完成）：说一句话就够，不打断操作
    const noteEl = root.querySelector('#proj-msg');
    const setNote = (text) => {
      flash = text || '';            // 活到下一次渲染之后
      if (!noteEl) return;
      noteEl.textContent = text || '';
      noteEl.hidden = !text;
    };

    /* ---------- 列表：排序、状态、搜索与标签筛选（纯前端） ---------- */
    const searchEl = root.querySelector('#proj-search');
    const filters = root.querySelector('#proj-filters');
    const sortEl = root.querySelector('#proj-sort');
    const countEl = root.querySelector('#proj-count');
    const allTags = [...new Set(store.projects.flatMap((p) => p.tags))].sort();
    const tbody = root.querySelector('#proj-rows');
    const allRows = () => (tbody ? [...tbody.querySelectorAll('tr[data-id]')] : []);

    // 三种排序都在本地做：项目几十个，重排 tbody 比再打一趟接口便宜，
    // 也不会动筛选状态。默认"最近打开"与服务端 _ORDER 同口径（没打开过的沉底）。
    const cmpOpened = (a, b) => (b.dataset.opened || '').localeCompare(a.dataset.opened || '')
      || Number(b.dataset.id) - Number(a.dataset.id);
    const cmpUpdated = (a, b) => (b.dataset.updated || '').localeCompare(a.dataset.updated || '')
      || cmpOpened(a, b);
    const cmpName = (a, b) => a.dataset.name.localeCompare(b.dataset.name, 'zh-Hans-CN');
    const CMP = { opened: cmpOpened, updated: cmpUpdated, name: cmpName };
    const applySort = () => {
      if (!tbody) return;
      allRows().sort(CMP[sortMode] || cmpOpened).forEach((tr) => tbody.appendChild(tr));
    };
    applySort();

    const applyFilter = () => {
      const rows = allRows();
      const kw = (searchEl?.value || '').trim().toLowerCase();
      let shown = 0;
      const visibleIds = new Set();
      rows.forEach((tr) => {
        const name = tr.dataset.name.toLowerCase();
        const tags = JSON.parse(tr.dataset.tags || '[]');
        const hitKeyword = !kw || name.includes(kw)
          || tags.some((t) => t.toLowerCase().includes(kw));
        const hitTag = !activeTag || tags.includes(activeTag);
        const hitStatus = activeStatus === 'all' || tr.dataset.status === activeStatus;
        tr.hidden = !(hitKeyword && hitTag && hitStatus);
        if (!tr.hidden) {
          shown += 1;
          if (tr.dataset.id) visibleIds.add(Number(tr.dataset.id));
        }
      });
      if (countEl) {
        countEl.textContent = shown === rows.length
          ? `共 ${rows.length} 个`
          : `显示 ${shown} / 共 ${rows.length} 个`;
      }
      // 选中的必须是看得见的：筛完把不在可见行里的勾剪掉。不剪的话批量条数着
      // 几十行外看不见的项目，点归档就打到你没看到的行为上。
      // 集合和那一行的复选框要一起剪：只剪集合，切回原筛选时会出现"看着勾着、其实没选"
      [...picked].forEach((id) => {
        if (visibleIds.has(id)) return;
        picked.delete(id);
        const box = root.querySelector(`#proj-rows .row-pick[data-pick="${id}"]`);
        if (box) box.checked = false;
      });
      const empty = root.querySelector('#no-match');
      if (empty) {
        empty.hidden = shown > 0;
        // 归档的东西"不见了"最容易被当成丢了，空结果里主动说一句去哪找
        const hint = root.querySelector('#no-match-hint');
        if (hint && !shown && activeStatus === 'active' && archivedCount) {
          hint.textContent = `没有在产项目。有 ${archivedCount} 个已归档，切到上面「已归档」看看。`;
        }
      }
      renderBar();
    };

    if (sortEl) {
      sortEl.addEventListener('change', () => {
        sortMode = sortEl.value;
        applySort();
      });
    }

    /* ---------- 勾选与批量操作 ---------- */
    const bar = root.querySelector('#batch-bar');

    const pickAll = root.querySelector('#pick-all');
    // 归档总数在 mount 里算：render 和 mount 是两个作用域，跨过去拿不到
    const archivedCount = store.projects.filter((p) => p.status === 'archived').length;

    const visibleRows = () => [...root.querySelectorAll('#proj-rows tr[data-id]')]
      .filter((tr) => !tr.hidden);

    function renderBar() {
      if (!bar) return;
      const ids = [...picked];
      if (!ids.length) {
        bar.hidden = true;
        bar.innerHTML = '';
      } else {
        const selected = ids.map((id) => store.projects.find((p) => p.id === id)).filter(Boolean);
        const names = selected.map((p) => p.name);
        const canWrite = store.can('project:write');
        // 只给当前选择"用得上"的动作：全是归档项目时"归档"没有意义，
        // 摆着只会让人点一下发现什么都没变
        const anyActive = selected.some((p) => p.status !== 'archived');
        const anyArchived = selected.some((p) => p.status === 'archived');
        bar.innerHTML = `
          <span>已选 <b>${ids.length}</b> 个</span>
          <span class="bd-names">${escapeHtml(names.join('、'))}</span>
          <span class="right">
            ${canWrite && anyActive ? '<button class="btn sm" data-batch="archive">归档</button>' : ''}
            ${canWrite && anyArchived ? '<button class="btn sm" data-batch="restore">恢复在产</button>' : ''}
            ${canWrite ? '<button class="btn sm" data-batch="tag">加标签</button>' : ''}
            ${store.can('project:delete') ? '<button class="btn sm danger" data-batch="delete">删除</button>' : ''}
            <button class="btn sm ghost" data-batch="clear">取消选择</button>
          </span>`;
        bar.hidden = false;
      }
      if (pickAll) {
        const vis = visibleRows();
        const chosen = vis.filter((tr) => picked.has(Number(tr.dataset.id))).length;
        pickAll.checked = vis.length > 0 && chosen === vis.length;
        // 只勾了一部分时表头要显示"半选"，否则看着像一个都没勾上
        pickAll.indeterminate = chosen > 0 && chosen < vis.length;
      }
    }

    /** 批量动作。被跳过的项目不该让整批失败，所以结果要说清楚，不能只报"成功" */
    async function doBatch(action, extra = {}) {
      const ids = [...picked];
      if (!ids.length) return false;
      let res = null;
      const ok = await run(action === 'delete' ? '批量删除' : '批量操作', async () => {
        res = action === 'delete'
          ? await api.batchDeleteProjects(ids, extra.confirm)
          : await api.batchProjects({ ids, action, tags: extra.tags || [] });
        await store.load();
        picked.clear();
      });
      if (!ok || !res) return false;
      const label = { archive: '已归档', restore: '已恢复在产',
                      tag: '已加标签', delete: '已删除' }[action];
      // 成功与跳过分开报数：跳过 0 个时不提，避免"跳过 0 个"这种凑字数的句子
      let text = `${label} ${res.done.length} 个项目`;
      if (res.skipped.length) {
        text += `；跳过 ${res.skipped.length} 个：` + res.skipped.map((s) => s.reason).join('；');
      }
      if (action === 'archive' && res.done.length) text += '。切到上面「已归档」可以找回';
      if (action === 'delete' && res.skipped.length) text += '。其余项目没动';
      setNote(text);
      refresh();
      return true;
    }

    if (searchEl) {
      searchEl.addEventListener('input', () => {
        keyword = searchEl.value;
        applyFilter();
      });
      applyFilter();
    }
    if (filters) {
      // 只重画筛选条这一小段 + 重新筛行。原来这里走的是整页 refresh，两个毛病：
      // 搜索框会被重绘清空（词悄悄丢了），而且筛选本来就是纯前端的，不该为它重建表格
      const bindChips = () => {
        filters.querySelectorAll('[data-status],[data-tag]').forEach((chip) => {
          chip.addEventListener('click', () => {
            if (chip.dataset.status !== undefined) activeStatus = chip.dataset.status;
            else activeTag = chip.dataset.tag;
            filters.innerHTML = filtersHtml(allTags);
            bindChips();
            applyFilter();
          });
        });
      };
      bindChips();
    }

    if (pickAll) {
      pickAll.addEventListener('change', () => {
        const vis = visibleRows();
        vis.forEach((tr) => {
          const id = Number(tr.dataset.id);
          if (pickAll.checked) picked.add(id); else picked.delete(id);
          // 行框的视觉状态必须跟着走：只改 picked 的话，行里看着没勾、
          // 逻辑上已经选中，再点一下就"勾上但数字不变"，谁看都是坏的
          const box = tr.querySelector('.row-pick');
          if (box) box.checked = pickAll.checked;
        });
        renderBar();
      });
    }

    if (bar) {
      bar.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-batch]');
        if (!btn) return;
        const action = btn.dataset.batch;
        if (action === 'clear') {
          // 集合和复选框一起清：只清集合，行里仍显示勾着，再点一下等于"取消一个没选的"
          picked.clear();
          root.querySelectorAll('#proj-rows .row-pick, #pick-all').forEach((box) => {
            box.checked = false;
            box.indeterminate = false;
          });
          renderBar();
          return;
        }
        if (action === 'tag') { openTagModal(); return; }
        if (action === 'delete') { openBatchDelModal(); return; }
        doBatch(action);
      });
    }

    /* ---------- 行：切当前 / 打开 / 更多菜单 ----------
       "打开"是主操作：设为当前项目并进创作中心，这是回到工位最短的路。
       点整行只切当前、不跳页——两件事分开，误点一下不会把人带走。 */
    const rowMenu = root.querySelector('#row-menu');
    let menuOwner = null;

    const closeRowMenu = (returnFocus = false) => {
      disarmRowMenu();
      if (!rowMenu || rowMenu.hidden) return;
      rowMenu.hidden = true;
      rowMenu.innerHTML = '';
      root.querySelectorAll('.more[aria-expanded="true"]')
        .forEach((b) => b.setAttribute('aria-expanded', 'false'));
      if (returnFocus && menuOwner) menuOwner.focus();
      menuOwner = null;
    };

    function openRowMenu(btn, id) {
      const row = btn.closest('tr');
      const src = row?.querySelector('.row-menu-src');
      if (!rowMenu || !src) return;
      if (menuOwner === btn) { closeRowMenu(true); return; }
      rowMenu.innerHTML = src.innerHTML;
      rowMenu.dataset.project = String(id);
      rowMenu.hidden = false;
      placeRowMenu(btn);
      menuOwner = btn;
      btn.setAttribute('aria-expanded', 'true');
      rowMenu.querySelector('[role="menuitem"]')?.focus();
      // 焦点落定前不挂收起：菜单贴着视口下缘时，focus() 自己可能小滚一下
      armRowMenu(() => closeRowMenu(true));
    }

    /** 把菜单摆进视口：优先朝下，下面放不下就朝上，两头都放不下就贴进来自己滚。 */
    function placeRowMenu(btn) {
      const r = btn.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      // 必须先 hidden=false 再量：隐藏元素量出来全是 0。
      // 上限要先落到样式上再量高度，否则"两头都放不下"时会按未截断的高度算出负 top
      rowMenu.style.maxHeight = `${Math.max(120, vh - MENU_EDGE * 2)}px`;
      const w = rowMenu.offsetWidth || 186;
      const h = rowMenu.offsetHeight || 160;
      const roomBelow = vh - r.bottom - MENU_GAP - MENU_EDGE;
      const roomAbove = r.top - MENU_GAP - MENU_EDGE;
      rowMenu.style.top = Math.round(
        h + MENU_GAP <= roomBelow ? r.bottom + MENU_GAP
          : (h + MENU_GAP <= roomAbove ? r.top - MENU_GAP - h
            : (roomBelow >= roomAbove ? vh - MENU_EDGE - h : MENU_EDGE))) + 'px';
      // 横向靠按钮右缘，但整块必须留在视口里
      rowMenu.style.left = Math.round(
        Math.min(Math.max(r.right - w, MENU_EDGE), vw - w - MENU_EDGE)) + 'px';
    }

    const openProject = (id) => {
      store.select(Number(id));      // 先切当前（它自己会重画一次）
      goWriting();                   // 再进创作中心——"打开项目"就是回到工位
    };

    if (rowMenu) {
      rowMenu.addEventListener('click', (e) => {
        const item = e.target.closest('[data-act]');
        if (!item) return;
        const id = Number(rowMenu.dataset.project);
        const act = item.dataset.act;
        const name = store.projects.find((p) => p.id === id)?.name || '';
        closeRowMenu();
        if (act === 'edit') openEdit(id);
        else if (act === 'members') openMembers(id, name);
        else if (act === 'delete') openDelete(id, name);
        else if (act === 'archive') archiveOne(id, name);
        else if (act === 'restore') restoreOne(id, name);
      });
      // 点别处、按 Esc 都收起。监听挂在视图根上而不是 document：
      // 每渲染一次加一对 document 监听会越攒越多，而菜单本来就活在这张页里。
      root.addEventListener('click', (e) => {
        if (!e.target.closest('#row-menu') && !e.target.closest('[data-more]')) closeRowMenu();
      });
      root.addEventListener('keydown', (e) => {
        if (rowMenu.hidden) return;
        if (e.key === 'Escape') { e.preventDefault(); closeRowMenu(true); return; }
        const items = [...rowMenu.querySelectorAll('[role="menuitem"]')];
        const idx = items.indexOf(document.activeElement);
        if (e.key === 'ArrowDown') { e.preventDefault(); items[(idx + 1) % items.length]?.focus(); }
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          items[(idx - 1 + items.length) % items.length]?.focus();
        }
      });
    }

    root.querySelectorAll('#proj-rows tr[data-id]').forEach((tr) => {
      tr.addEventListener('click', (e) => {
        const id = Number(tr.dataset.id);
        // 复选框那一下是"选不选"，不是"切不切当前项目"，两件事不能同时发生
        if (e.target.closest('.row-pick')) return;
        if (e.target.closest('[data-more]')) { openRowMenu(e.target.closest('[data-more]'), id); return; }
        if (e.target.closest('[data-open]')) { openProject(id); return; }
        if (e.target.closest('button')) return;
        store.select(id);
      });
    });
    root.querySelectorAll('#proj-rows .row-pick').forEach((box) => {
      box.addEventListener('change', () => {
        const id = Number(box.dataset.pick);
        if (box.checked) picked.add(id); else picked.delete(id);
        renderBar();
      });
    });

    /* ---------- 新建项目（弹窗，连带指定协作人） ---------- */
    const newModal = root.querySelector('#new-modal');
    const npName = root.querySelector('#np-name');
    const npTags = root.querySelector('#np-tags');
    const npDesc = root.querySelector('#np-desc');
    const npMembers = root.querySelector('#np-members');
    const npCreate = root.querySelector('#np-create');
    const newBtn = root.querySelector('#btn-new-project');

    const closeNew = () => { newModal.hidden = true; npCreate.disabled = false; };

    if (newBtn) {
      newBtn.addEventListener('click', async () => {
        npName.value = ''; npTags.value = ''; npDesc.value = '';
        npCreate.disabled = false;
        const err = root.querySelector('#np-err');
        if (err) { err.hidden = true; err.textContent = ''; }
        npMembers.innerHTML = '<span class="t-dim">读取中…</span>';
        newModal.hidden = false;
        npName.focus();
        try {
          // 候选由后端按层级过滤：只有 project:write 的人也能拿到这份名单
          const list = await api.memberCandidates();
          npMembers.innerHTML = list.length
            ? list.map((c) => `<label class="chk">
                 <input type="checkbox" data-np-member="${c.user_id}">
                 <span>${escapeHtml(c.display_name || c.username)}</span>
               </label>`).join('')
            : '<span class="t-dim">没有可邀请的人</span>';
        } catch (err) {
          npMembers.innerHTML = `<span class="t-bad">${escapeHtml(err.message)}</span>`;
        }
      });

      // 失败要写在弹窗里、已填的内容一个字不丢：新建一次要填四样，
      // 弹一句 alert 再关掉等于让人重来一遍
      const npErr = root.querySelector('#np-err');
      const showErr = (text) => {
        npErr.textContent = text || '';
        npErr.hidden = !text;
      };

      npCreate.addEventListener('click', async () => {
        showErr('');
        const name = npName.value.trim();
        if (!name) { showErr('项目名不能为空'); npName.focus(); return; }
        // 标签先按与服务端同一条口径归一：超出的截掉并在成功提示里说明，
        // 不做静默——界面显示的和库里存的不一样，事后没人解释得清
        const { tags, cut } = normTags(npTags.value);
        const memberIds = [...npMembers.querySelectorAll('input:checked')]
          .map((el) => Number(el.dataset.npMember));
        npCreate.disabled = true;
        try {
          const created = await api.createProject({
            name, description: npDesc.value.trim(), tags, member_ids: memberIds,
          });
          await store.load();
          store.currentId = created.id;
          closeNew();
          setNote(`已创建《${created.name}》，并设为当前项目`
            + `${cut ? '；标签超出上限的部分已截掉' : ''}`);
          refresh();
        } catch (err) {
          showErr(err.message || '创建失败');
        } finally {
          npCreate.disabled = false;
        }
      });

      root.querySelector('#np-cancel').addEventListener('click', closeNew);
      newModal.addEventListener('click', (e) => { if (e.target === newModal) closeNew(); });
      // Escape：另外四个弹窗都能键盘关，新建这个原来只能点外面——补齐
      newModal.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeNew(); });
      [npName, npTags].forEach((el) => {
        el.addEventListener('keydown', (e) => { if (e.key === 'Enter') npCreate.click(); });
      });
    }

    /* ---------- 项目管理：修改（名称／介绍／标签） ----------
       原来标签有个就地编辑的小按钮。既然「修改」弹窗把三样一起摆出来了，
       就地那个就撤掉——同一个字段两个入口，早晚出现改了这个忘那个。 */
    const editModal = root.querySelector('#edit-modal');
    const edName = root.querySelector('#ed-name');
    const edDesc = root.querySelector('#ed-desc');
    const edTags = root.querySelector('#ed-tags');
    const edMeta = root.querySelector('#ed-meta');
    const edSave = root.querySelector('#ed-save');
    let editingId = null;

    const closeEdit = () => {
      editModal.hidden = true;
      editingId = null;
      edSave.disabled = false;
    };

    function openEdit(id) {
      const p = store.projects.find((x) => x.id === id);
      if (!p) return;
      editingId = id;
      edName.value = p.name;
      edDesc.value = p.description || '';
      edTags.value = p.tags.join(', ');
      // 只读那一行：计数与两个时间都不可改，摆在这里是为了"所有内容一次看全"
      const costText = (p.costs || []).length
        ? '　成本 ' + p.costs.map((c) =>
          `${formatMoney(c.total_micros, c.currency)}${c.pending ? `（${c.pending} 笔待核对）` : ''}`
        ).join(' · ')
        : '';
      edMeta.textContent = `${p.episode_count} 集 · ${p.shot_count} 镜 · `
        + `${p.asset_count} 资产　创建 ${stamp(p.created_at)}　修改 ${stamp(p.updated_at)}`
        + costText;
      editModal.hidden = false;
      edName.focus();
      edName.select();
    }

    const edErr = root.querySelector('#ed-err');
    edSave.addEventListener('click', async () => {
      if (editingId === null) return;
      edErr.hidden = true;
      const name = edName.value.trim();
      if (!name) {
        edErr.textContent = '项目名不能为空';
        edErr.hidden = false;
        edName.focus();
        return;
      }
      const { tags } = normTags(edTags.value);
      edSave.disabled = true;
      try {
        await api.updateProject(editingId, {
          name, description: edDesc.value.trim(), tags,
        });
        await store.load();
        closeEdit();
        refresh();
      } catch (err) {
        edErr.textContent = err.message || '保存失败';
        edErr.hidden = false;
      } finally {
        edSave.disabled = false;
      }
    });

    root.querySelector('#ed-cancel').addEventListener('click', closeEdit);
    editModal.addEventListener('click', (e) => { if (e.target === editModal) closeEdit(); });
    editModal.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeEdit(); });
    [edName, edTags].forEach((el) => {
      el.addEventListener('keydown', (e) => { if (e.key === 'Enter') edSave.click(); });
    });

    /* ---------- 协作人 ----------
       名单和"可加的人"一起由后端给：候选项要按当前调用者能看见谁过滤，
       前端自己拉用户列表等于绕开那层过滤。 */
    const memModal = root.querySelector('#mem-modal');
    const memList = root.querySelector('#mem-list');
    const memAdd = root.querySelector('#mem-add');
    const memAddBtn = root.querySelector('#mem-add-btn');
    let memId = null;

    const renderMembers = (data) => {
      const ownerName = store.projects.find((x) => x.id === memId)?.owner_name || '—';
      root.querySelector('#mem-owner').textContent =
        `所有人：${ownerName}${data.owner_id === store.user?.id ? '（你自己）' : ''}`;
      memList.innerHTML = data.members.length
        ? data.members.map((m) => `
            <div class="mem-row">
              <span>${escapeHtml(m.display_name || m.username)}</span>
              <span class="t-dim mono">${escapeHtml(m.username)}</span>
              <button class="btn sm" data-rm-member="${m.user_id}">移除</button>
            </div>`).join('')
        : '<p class="t-dim">还没有协作人</p>';
      memAdd.innerHTML = data.candidates.length
        ? data.candidates.map((c) => `<option value="${c.user_id}">${
            escapeHtml(c.display_name || c.username)}（${escapeHtml(c.username)}）</option>`).join('')
        : '<option value="">没有可加的人</option>';
      memAdd.disabled = !data.candidates.length;
      memAddBtn.disabled = !data.candidates.length;
      // 转移这一行只对"所有人或管理员"出现。前端只是不摆按钮，
      // 真拦在后面：ensure(..., "manage")
      const canManage = store.isManager || data.owner_id === store.user?.id;
      const transferRow = root.querySelector('#mem-transfer-row');
      const transferSel = root.querySelector('#mem-transfer');
      const transferBtn = root.querySelector('#mem-transfer-btn');
      const transferrable = data.members.filter((m) => m.user_id !== data.owner_id);
      transferRow.hidden = !(canManage && transferrable.length);
      if (!transferRow.hidden) {
        transferSel.innerHTML = transferrable.map((m) => `<option value="${m.user_id}">${
          escapeHtml(m.display_name || m.username)}（${escapeHtml(m.username)}）</option>`).join('');
        transferBtn.disabled = false;
      }
    };

    async function openMembers(id, name) {
      memId = id;
      root.querySelector('#mem-proj').textContent = name || '';
      memList.innerHTML = '<p class="t-dim">读取中…</p>';
      memModal.hidden = false;
      try {
        renderMembers(await api.projectMembers(memId));
      } catch (err) {
        memList.innerHTML = `<p class="t-bad">${escapeHtml(err.message)}</p>`;
      }
    }

    memAddBtn.addEventListener('click', async () => {
      const uid = Number(memAdd.value);
      if (!uid || memId === null) return;
      const ok = await run('添加', async () => {
        renderMembers(await api.addProjectMember(memId, uid));
      });
      if (!ok) return;
    });

    memList.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-rm-member]');
      if (!btn || memId === null) return;
      await run('移除', async () => {
        renderMembers(await api.removeProjectMember(memId, Number(btn.dataset.rmMember)));
      });
    });

    /* ---------- 转移所有权 ----------
       这一步会让人"从所有人变成协作人"，所以确认语要把后果说完：
       仍然看得见、改得动，但删项目和管人归对方了。成功后关掉弹窗并刷新列表，
       因为列表里的"所有人"三个字跟着变了，留着弹窗只会显示一份旧身份。 */
    root.querySelector('#mem-transfer-btn')?.addEventListener('click', async () => {
      const uid = Number(root.querySelector('#mem-transfer').value);
      if (!uid || memId === null) return;
      const who = root.querySelector('#mem-transfer')
        .selectedOptions[0]?.textContent?.trim() || '对方';
      const btn = root.querySelector('#mem-transfer-btn');
      if (!(await confirmDanger({
        title: '转移所有权', okText: '确认转移',
        body: `把这个项目交给 <b>${escapeHtml(who)}</b>？<br>`
            + '你会变成协作人：仍然能看和改，但删项目、管协作人归对方。',
      }))) return;
      btn.disabled = true;
      const ok = await run('转移', async () => {
        await api.transferProject(memId, uid);
        await store.load();
      });
      btn.disabled = false;
      if (!ok) {
        // 失败要留在原地让人换个人：弹窗已经被 run() 的一句话说明白，
        // 但名单可能刚变过，重取一次再让他挑
        try { renderMembers(await api.projectMembers(memId)); } catch { /* 闸门会接手 */ }
        return;
      }
      memModal.hidden = true;
      refresh();
    });

    root.querySelector('#mem-close').addEventListener('click', () => { memModal.hidden = true; });
    memModal.addEventListener('click', (e) => { if (e.target === memModal) memModal.hidden = true; });
    // 成员弹窗原来只能点外面或"关闭"钮，键盘撤不回来——四个缺 Escape 的弹窗之一
    bindModalDismiss(memModal, () => { memModal.hidden = true; });

    /* ---------- 删除项目 ---------- */
    const modal = root.querySelector('#del-modal');
    const confirmEl = root.querySelector('#del-confirm');
    const okBtn = root.querySelector('#del-ok');
    const summaryEl = root.querySelector('#del-summary');
    let pending = null;

    const closeModal = () => {
      modal.hidden = true;
      pending = null;
      confirmEl.value = '';
      okBtn.disabled = true;
    };

    async function openDelete(id, name) {
      pending = { id, name };

      summaryEl.textContent = '正在清点……';
      modal.hidden = false;
      confirmEl.value = '';
      confirmEl.placeholder = name;
      okBtn.disabled = true;
      confirmEl.focus();

      try {
        const s = await api.projectSummary(id);
        summaryEl.innerHTML = `将删除《<b>${escapeHtml(s.name)}</b>》，以及它下面的 `
          + `<b>${s.episodes}</b> 集、<b>${s.shots}</b> 条镜头、<b>${s.assets}</b> 个资产、`
          + `<b>${s.documents}</b> 份文档和 <b>${s.image_files}</b> 张已上传的定妆图。`;
      } catch (err) {
        summaryEl.textContent = '清点失败：' + err.message;
      }
    }

    /* ---------- 单项目归档／恢复 ----------
       归档是低风险（数据全留、随时能恢复），所以不弹输入框，只把后果说清；
       删除才是不可逆的，那一步仍旧要手打项目名。 */
    async function archiveOne(id, name) {
      const ok = await confirmDanger({
        title: '归档项目', okText: '归档',
        body: `把《${escapeHtml(name)}》归档？<br>它会从「在产」列表和顶栏项目菜单里隐藏，`
          + '数据一条不删；随时可以在上面「已归档」里恢复。',
      });
      if (!ok) return;
      const done = await run('归档', async () => {
        await api.setProjectStatus(id, 'archived');
        // 当前项目正是它时，store 会退到第一个在产项目——顶栏不会指着一个归档项目
        await store.load();
      });
      if (!done) return;
      setNote(`已归档《${name}》。切到上面「已归档」可以找回`);
      refresh();
    }

    async function restoreOne(id, name) {
      const done = await run('恢复', async () => {
        await api.setProjectStatus(id, 'active');
        await store.load();
      });
      if (!done) return;
      setNote(`已恢复《${name}》到在产`);
      refresh();
    }

    confirmEl.addEventListener('input', () => {
      okBtn.disabled = !pending || confirmEl.value.trim() !== pending.name;
    });
    confirmEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !okBtn.disabled) okBtn.click();
      if (e.key === 'Escape') closeModal();
    });
    root.querySelector('#del-cancel').addEventListener('click', closeModal);
    modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
    bindModalDismiss(modal, closeModal);

    okBtn.addEventListener('click', async () => {
      if (!pending) return;
      okBtn.disabled = true;
      const ok = await run('删除', async () => {
        await api.deleteProject(pending.id, confirmEl.value.trim());
        // 当前项目被删后的回退交给 store.load()：它现在优先退到在产的第一个，
        // 这里再自己挑一次 projects[0] 等于绕过那条规则
        await store.load();
      });
      okBtn.disabled = false;
      if (!ok) return;
      closeModal();
      refresh();
    });

    /* ---------- 批量加标签 / 批量删除 ----------
       两个弹窗都只服务"已选中的那一批"，所以打开时把名单和数量现算一遍，
       不缓存：中间可能又勾了两个或删了一个。 */
    const tagModal = root.querySelector('#tag-modal');
    const tagInput = root.querySelector('#tag-input');
    const tagOk = root.querySelector('#tag-ok');

    function openTagModal() {
      if (!tagModal) return;
      root.querySelector('#tag-count').textContent = `已选 ${picked.size} 个项目`;
      tagInput.value = '';
      tagModal.hidden = false;
      tagInput.focus();
    }

    tagOk?.addEventListener('click', async () => {
      const tags = tagInput.value.split(/[,，]/).map((t) => t.trim()).filter(Boolean);
      if (!tags.length) { tagInput.focus(); return; }
      tagOk.disabled = true;
      await doBatch('tag', { tags });
      tagOk.disabled = false;
    });
    root.querySelector('#tag-cancel')?.addEventListener('click', () => { tagModal.hidden = true; });
    tagModal?.addEventListener('click', (e) => { if (e.target === tagModal) tagModal.hidden = true; });
    // Escape 原来只绑在输入框上：焦点一旦在"加标签/取消"钮上就撤不回来
    if (tagModal) bindModalDismiss(tagModal, () => { tagModal.hidden = true; });
    tagInput?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') tagOk.click();
      if (e.key === 'Escape') tagModal.hidden = true;
    });

    const bdModal = root.querySelector('#batch-del-modal');
    const bdList = root.querySelector('#bd-list');
    const bdConfirm = root.querySelector('#bd-confirm');
    const bdOk = root.querySelector('#bd-ok');

    async function openBatchDelModal() {
      if (!bdModal) return;
      const ids = [...picked];
      const names = ids
        .map((id) => store.projects.find((p) => p.id === id)?.name).filter(Boolean);
      bdConfirm.value = '';
      bdOk.disabled = true;
      bdList.textContent = '正在清点影响范围……';
      bdModal.hidden = false;
      bdConfirm.focus();
      // 影响范围逐个取权威汇总（集／镜／资产／文档／定妆图），不在前端估。
      // 有一条读不到就如实说"没读到"——清点是给人看的参考，不是删除的前置条件；
      // 确认词与服务端逐项权限检查才是。
      const sums = await Promise.all(ids.map((id) => api.projectSummary(id).catch(() => null)));
      if (bdModal.hidden) return;              // 清点途中被关掉了
      const got = sums.filter(Boolean);
      const total = (key) => got.reduce((n, s) => n + (s[key] || 0), 0);
      let text = `将删除 <b>${ids.length}</b> 个项目：<b>${escapeHtml(names.join('、'))}</b>，`
        + '连同它们的作业、音频与审核记录。';
      if (got.length) {
        text += ` 合计 <b>${total('episodes')}</b> 集、<b>${total('shots')}</b> 条镜头、`
          + `<b>${total('assets')}</b> 个资产、<b>${total('documents')}</b> 份文档、`
          + `<b>${total('image_files')}</b> 张已上传的定妆图。`;
      }
      if (got.length < ids.length) {
        text += ` 其中 ${ids.length - got.length} 个项目的影响范围没读到，删除前请确认名单。`;
      }
      bdList.innerHTML = text;
    }

    bdConfirm?.addEventListener('input', () => {
      bdOk.disabled = bdConfirm.value.trim() !== BATCH_DELETE_WORD;
    });
    bdConfirm?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !bdOk.disabled) bdOk.click();
      if (e.key === 'Escape') bdModal.hidden = true;
    });
    bdOk?.addEventListener('click', async () => {
      bdOk.disabled = true;
      // 删成功会整页重渲染，弹窗自然没了；失败才把它留在原地让人改确认词
      await doBatch('delete', { confirm: bdConfirm.value.trim() });
    });
    root.querySelector('#bd-cancel')?.addEventListener('click', () => { bdModal.hidden = true; });
    bdModal?.addEventListener('click', (e) => { if (e.target === bdModal) bdModal.hidden = true; });
    if (bdModal) bindModalDismiss(bdModal, () => { bdModal.hidden = true; });

    /* ---------- 去创作中心：只跳转，不在这里写集和分镜 ---------- */
    const gotoWriting = root.querySelector('#btn-goto-writing');
    if (gotoWriting) {
      gotoWriting.addEventListener('click', () => {
        window.dispatchEvent(new CustomEvent('app:go', { detail: 'writing' }));
      });
    }
  },
};
