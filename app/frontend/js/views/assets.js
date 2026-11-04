import { store } from '../store.js';
import { api } from '../api.js';
import { bindModalDismiss, escapeHtml, run } from '../util.js';
import { getParam } from '../navstate.js';
import { openDetail } from '../router.js';
import { bindSubChips, subChipRow } from '../util.js';
import { renderDetail, mountDetail } from './asset-detail.js';
import studio from './asset-studio.js';
import triage from './asset-triage.js';
import { WS_LABELS, setWsIndex, workerChip, wsIndex } from './asset-workspaces.js';

/**
 * 资产中心：角色／场景／道具的东家，也是三个工作区的调度者。
 *
 * 二级导航（`getSub('assets')`）从 v0.98 起表示**工作区**（资产库／资产创作／文本提取），
 * 不再是类型；类型降为资产库页内的 chip。理由与创作中心、产线中心一样：
 * 侧栏高亮和页面看到的必须是同一个值，而"按类型筛"是一个查询条件，不是一层去处。
 *
 * 这一页同时是详情的入口：地址带 `a<id>` 时（#assets/0/a57）把渲染与绑事件
 * 整个交给 asset-detail.js。分发放在这里而不是往路由注册表里再塞一个 id，
 * 因为"资产这一层的路由归谁"应该只有一个答案——侧栏高亮、二级标签、
 * 深链格式全都是它的私有细节，多一个注册 id 就多一处要和它们对齐的地方。
 */
const KINDS = [
  { code: '', label: '全部' },
  { code: 'character', label: '角色' },
  { code: 'scene', label: '场景' },
  { code: 'prop', label: '道具' },
];

const SCOPE_LABELS = { all: '全部范围', project: '本项目', global: '全局池' };
const COMPLETE_LABELS = { '': '全部状态', noimg: '缺参考图', noprompt: '缺一致性提示词' };

// 筛选状态放模块级：切分类不清空搜索词（指令 §3.1-B 明写"保留搜索词与其他条件"），
// 批量操作后整页重画也要还在。和项目中心 v0.57 同一条纪律。
let kind = '';            // 类型：以前它住在二级导航里，现在只是本页的查询条件
let keyword = '';
let scope = 'all';
let complete = '';
let sortBy = 'updated';
let lastError = '';
let lastList = [];        // 本次渲染拿到的那一批资产：搜索只重画网格，不再回库

const kindCode = () => kind;

/** 完善状态：缺图／缺提示词。口径只在这里算一次，卡片与统计共用。 */
function gaps(a) {
  return { noImg: !a.ref_images.length, noPrompt: !a.consistency_prompt.trim() };
}

function matches(a) {
  const kw = keyword.trim().toLowerCase();
  if (kw) {
    const hay = [a.name, ...a.tags, ...a.aliases, a.summary, a.consistency_prompt]
      .join(' ').toLowerCase();
    if (!hay.includes(kw)) return false;
  }
  const g = gaps(a);
  if (complete === 'noimg' && !g.noImg) return false;
  if (complete === 'noprompt' && !g.noPrompt) return false;
  return true;
}

function coverOf(a) {
  return a.ref_images.length ? `/files/${a.ref_images[0]}` : '';
}

/** 类型占位图：没有参考图时卡片不能是一块死白。用线性图标而不是汉字（外观纪律）。 */
function kindIcon(kind) {
  const paths = {
    character: '<circle cx="12" cy="8" r="3.4"/><path d="M5.5 20c.6-3.8 3.3-5.8 6.5-5.8s5.9 2 6.5 5.8"/>',
    scene: '<path d="M3 17l5-6 3.5 4L15 11l6 6"/><rect x="3" y="4.5" width="18" height="15" rx="2"/>',
    prop: '<path d="M12 3.5l7 4.2v8.6l-7 4.2-7-4.2V7.7z"/><path d="M12 12l7-4.3M12 12v8.5M12 12L5 7.7"/>',
  };
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
    stroke-linecap="round" aria-hidden="true">${paths[kind] || paths.prop}</svg>`;
}

function cardHtml(a) {
  const g = gaps(a);
  const isGlobal = a.project_id === null;
  const badge = g.noImg || g.noPrompt
    ? `<span class="chip amber">${g.noImg && g.noPrompt ? '缺图与提示词'
        : g.noImg ? '缺参考图' : '缺提示词'}</span>`
    : '<span class="chip green">完整</span>';
  return `
    <div class="asset-card" data-open="${a.id}" title="打开 ${escapeHtml(a.name)}">
      <div class="asset-cover">
        ${coverOf(a)
          ? `<img src="${coverOf(a)}" alt="${escapeHtml(a.name)} 的封面" loading="lazy"
                 data-preview="${coverOf(a)}">`
          : `<span class="asset-noimg">${kindIcon(a.kind)}</span>`}
        ${isGlobal ? '<span class="asset-scope">全局</span>' : ''}
      </div>
      <div class="asset-body">
        <button class="asset-title" data-open="${a.id}">${escapeHtml(a.name)}</button>
        <div class="asset-tags">
          <span class="chip plain">${a.kind_label}</span>
          ${(a.tags || []).slice(0, 3).map((t) => `<span class="chip">${escapeHtml(t)}</span>`).join('')}
          ${(a.tags || []).length > 3 ? `<span class="chip gray">＋${a.tags.length - 3}</span>` : ''}
        </div>
        <p class="asset-sum">${escapeHtml(a.summary || (a.aliases.length
          ? '别名 ' + a.aliases.join('、') : '还没有简介'))}</p>
      </div>
      <div class="asset-foot">
        <span class="t-dim">${a.ref_images.length} 图 · 关联 ${a.ref_shot_count} 镜</span>
        ${badge}
      </div>
    </div>`;
}

/**
 * 网格与空态/错误态那一段。抽出来是因为搜索要**只重画这一段**：
 * 原来每敲一个字都发一次 app:refresh，整页重渲染不但白打一次接口，
 * 还会把焦点从搜索框上摘走——实际效果是一次只能输入一个字。
 * 分镜弹窗的资产筛选（writing.js 的 `paintPicker`）早就是这个写法，这里跟上。
 */
function gridHtml(shown, total, filtering, canWrite) {
  if (lastError) {
    return `<div class="empty" style="padding:26px 20px">
      <div class="empty-t">读不到资产</div>
      <p>${escapeHtml(lastError)}</p>
      <p><button class="btn" data-retry>重试</button></p></div>`;
  }
  if (shown.length) return `<div class="asset-grid">${shown.map(cardHtml).join('')}</div>`;
  return `<div class="empty" style="padding:30px 20px">
          <div class="empty-t">${filtering ? '没有符合筛选条件的资产' : '还没有资产'}</div>
          <p>${filtering ? '换个关键词，或把类型／范围／状态调回全部。'
              : (canWrite ? '点右上角「新建资产」登记第一个角色或场景；也可以从创作中心的正文里抽取候选再确认入库。'
                          : '等项目里有人登记资产。')}</p>
          ${filtering ? '<p><button class="btn ghost" data-clear>清除筛选</button></p>' : ''}
        </div>`;
}

/** 只更新网格与那一格计数；搜索框、chip、焦点一概不碰。 */
function paintGrid(root, list) {
  const shown = sortShown(list);
  const box = root.querySelector('#asset-grid-box');
  const canWrite = store.can('asset:write');
  const filtering = Boolean(keyword.trim() || complete || kindCode() || scope !== 'all');
  if (box) box.innerHTML = gridHtml(shown, list.length, filtering, canWrite);
  const count = root.querySelector('#asset-count');
  if (count) count.textContent = `${shown.length} / ${list.length} 条`;
  bindCards(root);
  bindGridHelp(root);
}

/**
 * 空态与错误态里的两颗钮。
 *
 * 它们**只能在这里绑**：这两颗是 paintGrid 刚画出来的新节点，而 mount 跑的时候
 * 网格里还是首屏那一批（有结果时压根没有这两颗），`querySelector` 拿到 null 等于
 * 从来没绑上——于是"搜一个没命中的词之后点『清除筛选』没反应"（本轮修的第三条）。
 * 每次重画都是新节点，所以这里不需要防重复绑定。
 */
function bindGridHelp(root) {
  const reload = () => window.dispatchEvent(new CustomEvent('app:refresh'));
  root.querySelector('#asset-grid-box [data-retry]')?.addEventListener('click', reload);
  root.querySelector('#asset-grid-box [data-clear]')?.addEventListener('click', () => {
    // 清除的是"这一页所有筛选条件"，包括类型与范围——不然按钮名不副实
    keyword = ''; complete = ''; scope = 'all'; kind = '';
    reload();
  });
}

function sortShown(list) {
  return list.filter(matches).sort((x, y) => (sortBy === 'name'
    ? x.name.localeCompare(y.name, 'zh')
    : (y.updated_at || '').localeCompare(x.updated_at || '')));
}

/** 卡片点开的绑定。网格重画后要再来一次，所以单独一条。 */
function bindCards(root) {
  root.querySelectorAll('[data-open]').forEach((el) => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('button') && e.target !== el) return;
      openDetail('assets', Number(el.dataset.open));
    });
  });
}

export default {
  async render() {
    const detailId = getParam('assets');
    if (detailId) return renderDetail(detailId);
    // 另外两个工作区各自成页：它们读的是同一批接口，但界面结构完全不同，
    // 塞进这一张卡片里只会让两边的显隐条件互相打架
    if (wsIndex() === 1) return studio.render();
    if (wsIndex() === 2) return triage.render();

    const project = store.current;
    if (!project) {
      return `<section class="view on">
        <div class="page-head"><h1>资产中心</h1><p>角色／场景／道具与它们的定妆图、一致性提示词</p></div>
        <div class="card"><div class="empty">
          <div class="empty-t">还没有选择项目</div>
          <p>资产要么属于某个项目，要么在全局资产池里。先选一个项目再往下看。</p>
          <p><button class="btn primary" id="assets-topick">去项目中心选</button></p>
        </div></div></section>`;
    }

    let list = [];
    lastError = '';
    try {
      list = await api.assets(project.id, kindCode() || undefined, scope);
    } catch (err) {
      // 不再 catch 成空列表：那会把"读不到"演成"还没有"，看着像数据丢了
      lastError = err.message || '未知错误';
      list = [];
    }
    const shown = sortShown(list);
    lastList = list;
    const stats = {
      total: list.length,
      character: list.filter((a) => a.kind === 'character').length,
      scene: list.filter((a) => a.kind === 'scene').length,
      prop: list.filter((a) => a.kind === 'prop').length,
      incomplete: list.filter((a) => { const g = gaps(a); return g.noImg || g.noPrompt; }).length,
      global: list.filter((a) => a.project_id === null).length,
    };
    const canWrite = store.can('asset:write');
    // 空态要分清"这一页没有"和"这个 project 真的还没有"：类型与范围也是筛选条件，
    // 只看关键词就把"场景里没有"说成"还没有资产"，人会以为数据丢了
    const filtering = Boolean(keyword.trim() || complete || kindCode() || scope !== 'all');

    const chips = KINDS.map((k) => `
      <button type="button" class="chip ${kind === k.code ? 'blue' : 'plain'}"
              data-kind="${escapeHtml(k.code)}">${k.label}</button>`).join('');
    const scopeChips = Object.entries(SCOPE_LABELS).map(([key, label]) => `
      <button type="button" class="chip ${scope === key ? 'blue' : 'plain'}"
              data-scope="${key}">${label}</button>`).join('');
    const completeChips = Object.entries(COMPLETE_LABELS).map(([key, label]) => `
      <button type="button" class="chip ${complete === key ? 'blue' : 'plain'}"
              data-complete="${key}">${label}</button>`).join('');

    return `
    <section class="view on">
      <div class="page-head">
        <h1>资产中心</h1>
        <p>${escapeHtml(project.name)} · 定妆图与一致性提示词只在这里改，镜头只引用</p>
        <div class="right">
          ${canWrite ? '<button class="btn primary" id="btn-new-asset">新建资产</button>' : ''}
          ${subChipRow(WS_LABELS, wsIndex())}
        </div>
      </div>

      <div class="asset-stats">
        <span><b>${stats.total}</b> 条可见资产</span>
        <span class="t-dim">角色 ${stats.character} · 场景 ${stats.scene} · 道具 ${stats.prop}</span>
        <span class="t-dim">其中全局 ${stats.global}</span>
        ${stats.incomplete ? `<span class="chip amber">${stats.incomplete} 条待完善</span>`
                           : '<span class="chip green">都已完善</span>'}
        ${workerChip()}
      </div>

      <div class="card">
        <div class="card-head">
          <h2>资产</h2><span class="sub" id="asset-count">${shown.length} / ${list.length} 条</span>
          <div class="right"><input id="asset-search" class="ctl sm" placeholder="搜名称、标签、别名、提示词"
                                    style="width:240px" value="${escapeHtml(keyword)}"></div>
        </div>
        <div class="card-body asset-filters" id="asset-filters">
          <span class="note">类型</span>${chips}
          <span class="note">范围</span>${scopeChips}
          <span class="note">状态</span>${completeChips}
          <span class="note">排序</span>
          <select id="asset-sort" class="ctl sm">
            <option value="updated"${sortBy === 'updated' ? ' selected' : ''}>最近修改</option>
            <option value="name"${sortBy === 'name' ? ' selected' : ''}>名称</option>
          </select>
        </div>
        <div class="card-body" id="asset-grid-box">
          ${gridHtml(shown, list.length, filtering, canWrite)}
        </div>
      </div>

      <div class="modal" id="na-modal" hidden>
        <div class="modal-box">
          <div class="modal-head neutral">新建资产</div>
          <div class="modal-body">
            <div class="form-grid">
              <label for="na-kind">资产类型</label>
              <select id="na-kind" class="ctl">
                <option value="character">角色</option>
                <option value="scene">场景</option>
                <option value="prop">道具</option>
              </select>

              <label for="na-name">名称</label>
              <input id="na-name" class="ctl" maxlength="60" autocomplete="off"
                     placeholder="例如 掌灯人">

              <label for="na-summary" class="top">简介</label>
              <textarea id="na-summary" class="ctl" rows="2" maxlength="500"
                        placeholder="一句话说清这是谁／什么地方／什么东西"></textarea>

              <label for="na-prompt" class="top">一致性提示词</label>
              <textarea id="na-prompt" class="ctl" rows="3" maxlength="1000"
                        placeholder="跨镜头必须长一样的那些细节：外形、配色、材质、禁忌"></textarea>

              <label for="na-tags">标签</label>
              <input id="na-tags" class="ctl" placeholder="逗号分隔，最多 10 个">

              <label for="na-aliases">别名</label>
              <input id="na-aliases" class="ctl" placeholder="逗号分隔，抽取合并时靠它对上同一个">

              ${store.isManager ? `
              <span class="pf-label">归属</span>
              <label class="chk"><input type="checkbox" id="na-global">
                <span>放进全局资产池（各项目都能引用，不属于当前项目）</span></label>` : ''}
            </div>
            <p class="modal-meta pf-hint">同一范围、同一类型里名称唯一；重名说明该合并，不是新建。</p>
          </div>
          <div class="modal-actions">
            <button class="btn" id="na-cancel">取消</button>
            <button class="btn primary" id="na-save">登记</button>
          </div>
        </div>
      </div>
    </section>`;
  },

  mount(root) {
    const detailId = getParam('assets');
    if (detailId) return mountDetail(root, detailId);
    if (wsIndex() === 1) return studio.mount(root);
    if (wsIndex() === 2) return triage.mount(root);

    // 工作区切换器（窄屏才可见）：它改的是二级导航，与侧栏同一个值。
    // **必须带 refresh()**：≤860px 时侧栏那一层整块隐藏（app.css 的 .nav-sub），
    // 这一排 chip 是唯一的出口；只写 setWsIndex 就是"点了没反应、下次刷新莫名跳过去"
    // ——全仓八处 bindSubChips 只有这一处漏了（本轮修的第四条）。
    bindSubChips(root, (i) => { setWsIndex(i); window.dispatchEvent(new CustomEvent('app:refresh')); });

    const toDetail = root.querySelector('#assets-topick');
    toDetail?.addEventListener('click', () => {
      window.dispatchEvent(new CustomEvent('app:go', { detail: 'projects' }));
    });
    if (detailId || !store.current) return;

    const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
    const searchEl = root.querySelector('#asset-search');
    const filters = root.querySelector('#asset-filters');

    searchEl?.addEventListener('input', () => {
      keyword = searchEl.value;
      // 只重画网格：搜索词命中的是已经拿到手的那一批，回一次库、重渲染一次整页
      // 都是白付的钱，而重渲染会把焦点从人正在敲的这一个框上摘走。
      paintGrid(root, lastList);
    });
    root.querySelector('#asset-sort')?.addEventListener('change', (e) => {
      sortBy = e.target.value;
      paintGrid(root, lastList);
    });
    // 「重试」「清除筛选」不在这里绑——它们是网格里的节点，随每次重画换新，
    // 绑定住在 paintGrid 末尾（见 bindGridHelp 那段注释）

    // 类型只是本页的查询条件，不进侧栏：侧栏那一层现在是工作区。
    // 范围与状态同理，三个都留在模块变量里，切页回来还在。
    // 这三个要走整页刷新：范围与类型是**服务端**的过滤条件，不是本地筛筛就完事。
    filters?.querySelectorAll('[data-kind]').forEach((el) => {
      el.addEventListener('click', () => { kind = el.dataset.kind; refresh(); });
    });
    filters?.querySelectorAll('[data-scope]').forEach((el) => {
      el.addEventListener('click', () => { scope = el.dataset.scope; refresh(); });
    });
    filters?.querySelectorAll('[data-complete]').forEach((el) => {
      el.addEventListener('click', () => { complete = el.dataset.complete; refresh(); });
    });

    // 卡片：整块可点，标题是真按钮（键盘停得住）。卡内若有别的控件要记得 stopPropagation
    bindCards(root);

    /* ---------- 新建资产（弹窗，先选类型再填字段） ---------- */
    const modal = root.querySelector('#na-modal');
    const nameEl = root.querySelector('#na-name');
    const saveBtn = root.querySelector('#na-save');
    const close = () => { modal.hidden = true; saveBtn.disabled = false; };

    root.querySelector('#btn-new-asset')?.addEventListener('click', () => {
      nameEl.value = '';
      root.querySelector('#na-summary').value = '';
      root.querySelector('#na-prompt').value = '';
      root.querySelector('#na-tags').value = '';
      root.querySelector('#na-aliases').value = '';
      const g = root.querySelector('#na-global'); if (g) g.checked = false;
      root.querySelector('#na-kind').value = kindCode() || 'character';
      // 从"角色"那一栏点新建，默认就该是角色；选"全部"时才退回人物
      modal.hidden = false;
      nameEl.focus();
    });

    const split = (v) => v.split(/[,，]/).map((x) => x.trim()).filter(Boolean);
    const create = async () => {
      const name = nameEl.value.trim();
      if (!name) { nameEl.focus(); return; }
      saveBtn.disabled = true;
      const globalEl = root.querySelector('#na-global');
      const ok = await run('登记', () => api.createAsset(store.currentId, {
        kind: root.querySelector('#na-kind').value,
        name,
        summary: root.querySelector('#na-summary').value.trim(),
        consistency_prompt: root.querySelector('#na-prompt').value.trim(),
        tags: split(root.querySelector('#na-tags').value),
        aliases: split(root.querySelector('#na-aliases').value),
        scope: globalEl?.checked ? 'global' : 'project',
      }));
      saveBtn.disabled = false;
      if (!ok) return;                       // 失败时弹窗留着，用户填的还在
      close();
      refresh();
    };
    saveBtn?.addEventListener('click', create);
    root.querySelector('#na-cancel')?.addEventListener('click', close);
    modal?.addEventListener('click', (e) => { if (e.target === modal) close(); });
    // Escape 交给 util 那一个实现（绑在弹窗上＝焦点必须在弹窗内才收得到）；
    // Enter 只在输入框里提交，留着——它要的是"焦点在哪个控件"这个信息。
    if (modal) bindModalDismiss(modal, close);
    modal?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target.tagName === 'INPUT') create();
    });
  },
};
