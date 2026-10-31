import { store } from '../store.js';
import { api } from '../api.js';
import { escapeHtml, run } from '../util.js';

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

// 库里存的是 datetime('now','localtime') 出来的 'YYYY-MM-DD HH:MM:SS'。
// 创建和最后修改两列必须走同一个截法：一个截 10 位、一个截 16 位，
// 同一行摆出两种精度，看着就像两个不同的时间类型。改口径只改这一行。
const stamp = (t) => t.slice(0, 16);

const STATUS_LABELS = { active: '在产', archived: '已归档', all: '全部' };
const BATCH_DELETE_WORD = '批量删除';

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
    const tagHtml = p.tags.length
      ? p.tags.map((t) => `<span class="chip plain">${escapeHtml(t)}</span>`).join(' ')
      : '<span class="t-dim">—</span>';
    // 权限只决定按钮显不显示；点下去的拦截在服务端
    const canWrite = store.can('project:write');
    const canDelete = store.can('project:delete');
    // 删项目与管协作人只归所有人（与管理员），协作人只能改和看
    const canOwn = (row) => store.isManager || row.owner_id === store.user?.id;
    // 介绍放在"内容"那格下面一行：项目中心列表本来就窄，单开一列会把时间挤没
    const descHtml = p.description
      ? `<span class="t-desc" title="${escapeHtml(p.description)}">${escapeHtml(p.description)}</span>`
      : '';
    // 进度先按"已定稿几集"算：episode.status 建表时就在，不用等二期的生成队列。
    // 等 job 表落地，这一格换成"通过 N/M 镜"，口径仍然是一个 chip
    const doneHtml = p.episode_count
      ? `<span class="chip ${p.episode_done === p.episode_count ? 'green' : 'gray'}">已定稿 `
        + `${p.episode_done}/${p.episode_count} 集</span>`
      : '';
    const archived = p.status === 'archived';
    // 只能看的人不给复选框：勾出来也没有可做的动作
    const pickCell = canWrite || canDelete
      ? `<td class="c"><input type="checkbox" class="row-pick" data-pick="${p.id}"
              aria-label="选择 ${escapeHtml(p.name)}"${picked.has(p.id) ? ' checked' : ''}></td>`
      : '';
    return `
      <tr data-id="${p.id}" data-name="${escapeHtml(p.name)}" data-status="${p.status}"
          data-tags="${escapeHtml(JSON.stringify(p.tags))}"
          class="${p.id === store.currentId ? 'sel ' : ''}${archived ? 'arch' : ''}">
        ${pickCell}
        <td class="t-strong">${escapeHtml(p.name)}
          ${archived ? '<span class="chip gray">已归档</span>' : ''}
          <span class="t-desc">所有人 ${escapeHtml(p.owner_name || '—')}</span></td>
        <td class="tagcell">${tagHtml}</td>
        <td class="t-dim">${p.episode_count} 集 · ${p.shot_count} 镜 · ${p.asset_count} 资产
          ${doneHtml}${descHtml}</td>
        <td class="t-dim mono">${stamp(p.created_at)}</td>
        <td class="t-dim mono">${stamp(p.updated_at)}</td>
        <td class="r">
          ${canWrite ? `<button class="btn sm" data-edit-project="${p.id}">修改</button>` : ''}
          ${canOwn(p) ? `<button class="btn sm" data-members="${p.id}"
                  data-name="${escapeHtml(p.name)}">成员</button>` : ''}
          ${canDelete && canOwn(p) ? `<button class="btn sm danger" data-del-project="${p.id}"
                  data-name="${escapeHtml(p.name)}">删除</button>` : ''}
        </td>
      </tr>`;
  }).join('');
}

export default {
  async render() {
    const projects = store.projects;
    const cur = store.current;
    const canWrite = store.can('project:write');
    const canDelete = store.can('project:delete');

    const allTags = [...new Set(projects.flatMap((p) => p.tags))].sort();
    // 选中的标签可能随着项目删光而消失；不清掉就会筛成一片空白，还看不出是为什么
    if (activeTag && !allTags.includes(activeTag)) activeTag = '';
    // 勾过的项目可能已经被删掉了，剪掉再算，免得批量条上显示一个数、实际能操作的另一个数
    [...picked].forEach((id) => { if (!projects.some((p) => p.id === id)) picked.delete(id); });
    const pickable = canWrite || canDelete;

    return `
    <section class="view on">
      <div class="page-head">
        <h1>项目中心</h1>
        <p>只管项目本身；集与分镜在创作中心，花费在生成中心</p>
        <div class="right">
          ${canWrite ? '<button class="btn primary" id="btn-new-project">新建项目</button>' : ''}
          <button class="btn" id="btn-goto-writing" ${cur ? '' : 'hidden'}>去创作中心</button>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>项目</h2><span class="sub">${projects.length} 个 · 点一行切换当前项目</span>
          <div class="right"><input id="proj-search" class="ctl sm" placeholder="搜项目名或标签"
                                    style="width:200px" value="${escapeHtml(keyword)}"></div>
        </div>
        <div class="card-body" style="padding:10px 14px;border-bottom:1px solid var(--line)">
          <div class="row" id="proj-filters" style="gap:6px;flex-wrap:wrap">
            ${filtersHtml(allTags)}
          </div>
        </div>
        ${pickable ? '<div class="batch-bar" id="batch-bar" hidden></div>' : ''}
        ${pickable ? '<div class="batch-msg note" id="batch-msg" hidden></div>' : ''}
        ${projects.length ? `
        <table>
          <thead><tr>${pickable ? `<th class="c"><input type="checkbox" class="row-pick" id="pick-all"
                       aria-label="全选当前筛选结果"></th>` : ''}<th>项目</th><th>标签</th><th>内容</th><th>创建</th><th>最后修改</th><th class="r"></th></tr></thead>
          <tbody id="proj-rows">${rowsFor(projects)}</tbody>
        </table>
        <div class="empty" id="no-match" hidden style="padding:26px 20px">
          <div class="empty-t">没有匹配的项目</div>
          <p id="no-match-hint">换个关键词或清掉标签筛选。</p>
        </div>` : `
        <div class="empty">
          <div class="empty-t">还没有项目</div>
          <p>${canWrite ? '右上角「新建项目」建第一个。' : '等项目所有人把你加为协作人。'}</p>
        </div>`}
      </div>

      <div class="modal" id="new-modal" hidden>
        <div class="modal-box">
          <div class="modal-head neutral">新建项目</div>
          <div class="modal-body">
            <div class="form-grid">
              <label for="np-name">项目名</label>
              <input id="np-name" placeholder="例如 梦华录" autocomplete="off">

              <label for="np-tags">标签</label>
              <input id="np-tags" placeholder="逗号分隔，例如 神话,短剧">

              <label for="np-desc" class="top">项目介绍</label>
              <textarea id="np-desc" maxlength="500" rows="3"
                        placeholder="一两句话说清做什么、给谁看（可留空）"></textarea>

              <!-- 协作人那行不给 .top：下面摆的是一行勾选框（或一句空态），不是文本域，
                   贴顶的 9px 内边距会把字段名压得比自己的内容还低 -->
              <span class="pf-label">协作人</span>
              <div id="np-members" class="role-pick" style="gap:12px 18px"></div>

              <!-- 提示挪进控件列：原来它挂在表单外面，左边界跑到字段名那一栏，
                   跟上面「协作人」的内容对不齐，看着像两列没对齐 -->
              <p class="modal-meta pf-hint">标签用来检索，最多 10 个。协作人建完也能在「成员」里加减。</p>
            </div>
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
            <p class="modal-meta">协作人可以查看和修改这个项目；删项目和管协作人只有所有人能做。</p>
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

    /* ---------- 列表：状态、搜索与标签筛选（纯前端） ---------- */
    const searchEl = root.querySelector('#proj-search');
    const filters = root.querySelector('#proj-filters');
    const allTags = [...new Set(store.projects.flatMap((p) => p.tags))].sort();
    const applyFilter = () => {
      const rows = [...root.querySelectorAll('#proj-rows tr')];
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

    /* ---------- 勾选与批量操作 ---------- */
    const bar = root.querySelector('#batch-bar');
    const barMsg = root.querySelector('#batch-msg');
    const pickAll = root.querySelector('#pick-all');
    // 归档总数在 mount 里算：render 和 mount 是两个作用域，跨过去拿不到
    const archivedCount = store.projects.filter((p) => p.status === 'archived').length;

    const setMsg = (text) => {
      if (!barMsg) return;
      barMsg.textContent = text || '';
      barMsg.hidden = !text;
    };
    const visibleRows = () => [...root.querySelectorAll('#proj-rows tr[data-id]')]
      .filter((tr) => !tr.hidden);

    function renderBar() {
      if (!bar) return;
      const ids = [...picked];
      if (!ids.length) {
        bar.hidden = true;
        bar.innerHTML = '';
      } else {
        const names = ids
          .map((id) => store.projects.find((p) => p.id === id)?.name).filter(Boolean);
        const canWrite = store.can('project:write');
        bar.innerHTML = `
          <span>已选 <b>${ids.length}</b> 个</span>
          <span class="bd-names">${escapeHtml(names.join('、'))}</span>
          <span class="right">
            ${canWrite ? '<button class="btn sm" data-batch="archive">归档</button>' : ''}
            ${canWrite ? '<button class="btn sm" data-batch="restore">恢复在产</button>' : ''}
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
      let text = `${label} ${res.done.length} 个项目`;
      if (action === 'archive' && res.done.length) text += '；切到上面「已归档」可以找回';
      if (res.skipped.length) {
        text += `。${res.skipped.length} 个被跳过：`
          + res.skipped.map((s) => s.reason).join('；');
      }
      setMsg(text);
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
        if (action === 'clear') { picked.clear(); renderBar(); return; }
        if (action === 'tag') { openTagModal(); return; }
        if (action === 'delete') { openBatchDelModal(); return; }
        doBatch(action);
      });
    }

    root.querySelectorAll('#proj-rows tr[data-id]').forEach((tr) => {
      tr.addEventListener('click', (e) => {
        // 复选框那一下是"选不选"，不是"切不切当前项目"，两件事不能同时发生
        if (e.target.closest('button') || e.target.closest('.row-pick')) return;
        store.select(Number(tr.dataset.id));
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

      npCreate.addEventListener('click', async () => {
        const name = npName.value.trim();
        if (!name) { npName.focus(); return; }
        const tags = npTags.value.split(/[,，]/).map((t) => t.trim()).filter(Boolean);
        const memberIds = [...npMembers.querySelectorAll('input:checked')]
          .map((el) => Number(el.dataset.npMember));
        npCreate.disabled = true;
        const ok = await run('新建', async () => {
          const created = await api.createProject({
            name, description: npDesc.value.trim(), tags, member_ids: memberIds,
          });
          await store.load();
          store.currentId = created.id;
        });
        npCreate.disabled = false;
        if (!ok) return;
        closeNew();
        refresh();
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

    root.querySelectorAll('[data-edit-project]').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const id = Number(btn.dataset.editProject);
        const p = store.projects.find((x) => x.id === id);
        if (!p) return;
        editingId = id;
        edName.value = p.name;
        edDesc.value = p.description || '';
        edTags.value = p.tags.join(', ');
        // 只读那一行：计数与两个时间都不可改，摆在这里是为了"所有内容一次看全"
        edMeta.textContent = `${p.episode_count} 集 · ${p.shot_count} 镜 · `
          + `${p.asset_count} 资产　创建 ${stamp(p.created_at)}　修改 ${stamp(p.updated_at)}`;
        editModal.hidden = false;
        edName.focus();
        edName.select();
      });
    });

    edSave.addEventListener('click', async () => {
      if (editingId === null) return;
      const name = edName.value.trim();
      if (!name) { edName.focus(); return; }
      const tags = edTags.value.split(/[,，]/).map((t) => t.trim()).filter(Boolean);
      edSave.disabled = true;
      const ok = await run('保存', async () => {
        await api.updateProject(editingId, {
          name, description: edDesc.value.trim(), tags,
        });
        await store.load();
      });
      edSave.disabled = false;
      if (!ok) return;
      closeEdit();
      refresh();
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
    };

    root.querySelectorAll('[data-members]').forEach((btn) => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        memId = Number(btn.dataset.members);
        root.querySelector('#mem-proj').textContent = btn.dataset.name || '';
        memList.innerHTML = '<p class="t-dim">读取中…</p>';
        memModal.hidden = false;
        try {
          renderMembers(await api.projectMembers(memId));
        } catch (err) {
          memList.innerHTML = `<p class="t-bad">${escapeHtml(err.message)}</p>`;
        }
      });
    });

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

    root.querySelector('#mem-close').addEventListener('click', () => { memModal.hidden = true; });
    memModal.addEventListener('click', (e) => { if (e.target === memModal) memModal.hidden = true; });

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

    root.querySelectorAll('[data-del-project]').forEach((btn) => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const id = Number(btn.dataset.delProject);
        const name = btn.dataset.name;
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
            + `<b>${s.episodes}</b> 集、<b>${s.shots}</b> 条镜头、<b>${s.assets}</b> 个资产`
            + `和 <b>${s.image_files}</b> 张已上传的定妆图。`;
        } catch (err) {
          summaryEl.textContent = '清点失败：' + err.message;
        }
      });
    });

    confirmEl.addEventListener('input', () => {
      okBtn.disabled = !pending || confirmEl.value.trim() !== pending.name;
    });
    confirmEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !okBtn.disabled) okBtn.click();
      if (e.key === 'Escape') closeModal();
    });
    root.querySelector('#del-cancel').addEventListener('click', closeModal);
    modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });

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
    tagInput?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') tagOk.click();
      if (e.key === 'Escape') tagModal.hidden = true;
    });

    const bdModal = root.querySelector('#batch-del-modal');
    const bdList = root.querySelector('#bd-list');
    const bdConfirm = root.querySelector('#bd-confirm');
    const bdOk = root.querySelector('#bd-ok');

    function openBatchDelModal() {
      if (!bdModal) return;
      const names = [...picked]
        .map((id) => store.projects.find((p) => p.id === id)?.name).filter(Boolean);
      bdList.innerHTML = `将删除 <b>${names.length}</b> 个项目：<b>${escapeHtml(names.join('、'))}</b>，`
        + '以及它们下面的全部集、镜头、资产和已上传的定妆图。';
      bdConfirm.value = '';
      bdOk.disabled = true;
      bdModal.hidden = false;
      bdConfirm.focus();
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

    /* ---------- 去创作中心：只跳转，不在这里写集和分镜 ---------- */
    const gotoWriting = root.querySelector('#btn-goto-writing');
    if (gotoWriting) {
      gotoWriting.addEventListener('click', () => {
        window.dispatchEvent(new CustomEvent('app:go', { detail: 'writing' }));
      });
    }
  },
};
