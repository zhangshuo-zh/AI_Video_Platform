import { store } from '../store.js';
import { api } from '../api.js';
import { bindModalDismiss, bindSubChips, confirmDanger, escapeHtml, errorBox, errorLine, run, subChipRow } from '../util.js';
import { getParam, getSub, setJobPrefill, setParam, setSub } from '../navstate.js';
import { closeDetail, openDetail } from '../router.js';

/**
 * 创作中心 = 集（Episode）+ 三级文档（小说／剧本／分镜）。
 * 按设计稿 §3.2，Episode 和 Shot 的东家在这里：建集、删集、加镜、删镜都只在这一页做，
 * 项目中心只显示计数。
 *
 * W05 之后，小说与剧本两页不再是"正文仍在文件里"的占位：
 * 平台读的是工作区里那份 Markdown（正文的事实来源仍然是文件＋Git，库里只有元数据），
 * 保存会生成一次 Git 提交，版本与差异在这页能翻。三条硬约定：
 * 1. 前端只传文档 id，永远不传路径（路径穿越这一类问题在界面上没有入口）。
 * 2. 保存必须带 expected_version；后端 409 时不覆盖、给对照，423 时说明谁在编辑。
 * 3. 分镜那一页的数据是 shot 表，不是文档——所以 kind 只有 novel／script 两档可建。
 * 选中的集／文档记在模块级变量里，切页回来还在。
 */
let pickedEpisodeId = null;
let pickedDocId = null;

// 编辑器状态。放在模块级而不是每次 render 重取：人正在写的东西不能被一次刷新抹掉。
// draftFor 记着这份草稿属于哪一份文档——换文档（含换项目、深链跳走再回来）必须重取，
// 否则会出现"打开的是新文档、编辑器里是旧文档那段话"，看着像数据串了。
let draft = null;
let draftFor = null;
let draftVersion = null;      // 打开/上次保存时拿到的版本令牌
let serverCopy = null;        // 409 之后拉下来的"盘上现在这份"，用来对照
let notice = '';              // 上一次保存的结果，一句话
let lockWarn = '';            // 别人持锁的提示
let dirty = false;            // 有未保存改动
let locking = false;          // 首次输入时抢锁只发一次

// 集状态。这一列 0004 建表时就有，但一直没有写入口；项目中心的
// "已定稿 N/M 集"就是它的聚合，所以入口放在这里——集的东家是创作中心
const EP_STATUS = { draft: ['草稿', 'gray'], active: ['在产', 'blue'], done: ['定稿', 'green'] };

// 文档三档。定稿不给冻住内容：那是 Review 闸口的事（W14），这里只是流程状态
const DOC_STATUS = { draft: ['草稿', 'gray'], polishing: ['润色中', 'blue'], finalized: ['定稿', 'green'] };

// 二级标签下标 → 文档类型。0 小说、1 剧本；2 是分镜（走 shot 表，不在这里）
const TAB_KIND = { 0: 'novel', 1: 'script' };
const KIND_LABEL = { novel: '小说', script: '剧本' };

/** 字数：去掉结尾换行再数。

    服务端存的文件末尾一定有一个 \n（POSIX 文本，也是 diff 不挂
    "No newline at end of file" 的前提），把它算进字数会让同一段文字
    在"刚打完"和"刷新之后"显示差一个字。
 */
function charCount(text) {
  return String(text || '').replace(/\n+$/, '').length;
}

// 侧栏二级导航里分镜排第三（小说、剧本在前），渲染后高亮它

export default {
  subIndex: 2,

  async render() {
    const tab = getSub('writing') ?? 2;
    // 页内也摆一排切换器：窄屏侧栏的二级标签会收起，只靠它等于把功能藏了
    const subRow = subChipRow(['小说', '剧本', '分镜'], tab);
    if (tab === 2) return renderShots(tab, subRow);
    return await renderDocs(tab, subRow);
  },

  mount(root) {
    const tab = getSub('writing') ?? 2;
    if (tab === 2) return mountShots(root);
    return mountDocs(root, tab);
  },
};

/* =======================================================================
   小说 / 剧本：文档列表 + 编辑器 + 版本历史
   ======================================================================= */

async function renderDocs(tab, subRow) {
  const project = store.current;
  if (!project) {
    return `<section class="view on">
      <div class="page-head"><h1>创作中心</h1><p>${KIND_LABEL[TAB_KIND[tab]]}</p>
        <div class="right">${subRow}</div></div>
      <div class="card"><div class="empty">
        <div class="empty-t">先选一个项目</div>
        <p>文档挂在项目下面，去项目中心建一个再回来。</p>
      </div></div>
    </section>`;
  }

  const kind = TAB_KIND[tab];
  let docs = [];
  let err = null;
  try { docs = await api.documents(project.id, kind); } catch (e) { err = e; }
  if (err) {
    return `<section class="view on">
      <div class="page-head"><h1>创作中心</h1><p>${KIND_LABEL[kind]}</p>
        <div class="right">${subRow}</div></div>
      <div class="card">${errorBox('这一页的文档', err)}</div>
    </section>`;
  }

  // 深链带进来的文档 id（地址形如 #writing/0/a12）。列表里没有就说明那条已经删了，
  // 退回列表而不是把编辑器停在一份不存在的稿子上
  const focus = getParam('writing');
  if (focus !== undefined && !Number.isNaN(Number(focus))) {
    if (docs.some((d) => d.id === Number(focus))) pickedDocId = Number(focus);
    setParam('writing', undefined);
  }
  if (!docs.some((d) => d.id === pickedDocId)) pickedDocId = docs.length ? docs[0].id : null;

  const current = docs.find((d) => d.id === pickedDocId) || null;
  const canWrite = store.can('doc:write');

  let body = '';
  let loadErr = null;
  if (current) {
    // 只有"这份文档还没读过"才拉正文：draft 里可能装着人正在写的东西，不能覆盖
    if (draft === null || draftVersion === null || draftFor !== current.id) {
      try {
        const data = await api.docContent(current.id);
        draft = data.content;
        draftVersion = data.version;
        draftFor = current.id;
        serverCopy = null;
        dirty = false;
      } catch (e) { loadErr = e; }
    }
    body = loadErr ? `<div class="card">${errorBox('这份正文', loadErr)}</div>` : editorHtml(current);
  } else {
    body = `<div class="card"><div class="empty">
      <div class="empty-t">${docs.length ? '选一份来写' : '还没有' + KIND_LABEL[kind] + '文档'}</div>
      <p>${docs.length ? '左边点一份，或者新建一份。'
                       : (canWrite ? '新建一份，正文会落到工作区里并生成第一次提交。'
                                   : '等项目里有人建第一份——只读角色看不到新建钮。')}</p>
    </div></div>`;
  }

  const list = docs.map((d) => {
    const [label, tone] = DOC_STATUS[d.status] || [d.status, 'gray'];
    return `
      <div class="t ${d.id === pickedDocId ? 'on' : ''}" data-doc="${d.id}">
        ${escapeHtml(d.title)}
        <span class="chip ${tone}">${label}</span>
        ${d.lock_holder_name && d.lock_holder_id !== store.user?.id
          ? `<span class="chip amber" title="锁到 ${escapeHtml(d.lock_expires_at)}，超时自己松开">
               ${escapeHtml(d.lock_holder_name)} 在改</span>` : ''}
        <span class="t-dim" style="float:right">${d.episode_title ? `第 ${d.episode_seq} 集` : '全项目'}
          <button class="btn sm danger" data-del-doc="${d.id}" data-title="${escapeHtml(d.title)}"
                  ${canWrite ? '' : 'disabled'}>删</button>
        </span>
      </div>`;
  }).join('');

  return `
  <section class="view on">
    <div class="page-head">
      <h1>创作中心</h1>
      <p>${KIND_LABEL[kind]} · ${escapeHtml(project.name)}${current ? ' / ' + escapeHtml(current.title) : ''}</p>
      <div class="right">${subRow}</div>
    </div>

    <div style="display:grid;grid-template-columns:280px minmax(0,1fr);gap:14px;align-items:start">
      <div class="card tree">
        <div class="grp" style="display:flex;justify-content:space-between;align-items:center">
          <span>${KIND_LABEL[kind]}文档 · ${docs.length} 份</span>
          ${canWrite ? '<button class="btn sm primary" id="doc-new">新建</button>' : ''}
        </div>
        ${docs.length ? list : '<p class="hint" style="padding:8px 10px;text-align:left">这一类还没有稿子。</p>'}
      </div>
      ${body}
    </div>

    ${newDocModal(project.id)}
  </section>`;
}

function editorHtml(doc) {
  const canWrite = store.can('doc:write');
  const heldByOther = doc.lock_holder_name && doc.lock_holder_id !== store.user?.id;
  const [label] = DOC_STATUS[doc.status] || [doc.status];
  return `
  <div class="card">
    <div class="card-head">
      <h2>${escapeHtml(doc.title)}</h2>
      <span class="sub">${escapeHtml(doc.rel_path)}</span>
      <div class="right">
        ${canWrite ? `
          <select id="doc-status" class="ctl sm" style="width:auto"
                  title="草稿 → 润色 → 定稿；定稿只改状态，不冻结内容">
            ${Object.entries(DOC_STATUS).map(([key, [text]]) => `
              <option value="${key}"${key === doc.status ? ' selected' : ''}>${text}</option>`).join('')}
          </select>
          <button class="btn sm primary" id="doc-save" ${canWrite ? '' : 'disabled'}>保存</button>`
        : `<span class="chip gray" title="只读角色没有编辑文档的权限">${label}</span>`}
        <button class="btn sm" id="doc-hist">版本</button>
      </div>
    </div>

    ${lockWarn || (heldByOther && !lockWarn) ? `<div class="bar warn">
        ${escapeHtml(lockWarn || `${doc.lock_holder_name} 正在编辑这一份（锁到 ${doc.lock_expires_at} 自己松开）。
        现在写不会覆盖对方——保存会被后端拦住，直到对方保存或锁过期。`)}
      </div>` : ''}

    ${serverCopy !== null ? `<div class="bar bad">
        <p><b>版本冲突。</b>你手上的令牌对应的正文已经不是盘上这一份了（有人在你之后保存过，
        或者在编辑器外面改过）。下面左边是你正在写的，右边是现在盘上的。</p>
        <div class="cmp">
          <div><span class="grp">我这边的草稿（还没存）</span>
            <pre class="diff keep">${escapeHtml(draft ?? '')}</pre></div>
          <div><span class="grp">工作区里现在这份</span>
            <pre class="diff">${escapeHtml(serverCopy)}</pre></div>
        </div>
        <p class="row" style="gap:8px">
          <button class="btn sm" id="doc-conflict-use">用盘上这份，丢掉我的草稿</button>
          <button class="btn sm" id="doc-conflict-refresh">刷新版本令牌</button>
        </p>
        <p class="hint" style="text-align:left">「刷新版本令牌」＝ 对照过了，确认要用我这边这份覆盖盘上那份。</p>
      </div>` : ''}

    ${notice ? `<div class="bar ok">${escapeHtml(notice)}</div>` : ''}

    <textarea id="doc-text" class="ctl doc-area" spellcheck="false"
              ${canWrite ? '' : 'readonly'} placeholder="在这里写正文……">${escapeHtml(draft ?? '')}</textarea>

    <div class="card-body" style="border-top:1px solid var(--line);padding:8px 12px;
         display:flex;gap:12px;align-items:center;flex-wrap:wrap">
      <span class="sub" id="doc-count">${charCount(draft)} 字</span>
      <span class="chip ${dirty ? 'amber' : 'green'}" id="doc-dirty">${dirty ? '未保存' : '与盘上一致'}</span>
      <span class="sub" id="doc-commit">${doc.git_commit ? '当前提交 ' + doc.git_commit.slice(0, 8) : '还没有提交记录'}</span>
      <span class="sub" id="doc-updated" data-created="${escapeHtml(doc.created_at)}"
            style="margin-left:auto">创建 ${escapeHtml(doc.created_at)}
        · 改动 ${escapeHtml(doc.updated_at)}</span>
    </div>

    <div id="doc-hist-panel" hidden class="card-body" style="border-top:1px solid var(--line)">
      <p class="hint" style="text-align:left">版本历史来自工作区的 Git 仓库，最多列最近 30 次。</p>
      <div id="doc-hist-list"><span class="sub">载入中…</span></div>
      <pre id="doc-diff" class="diff" hidden></pre>
    </div>
  </div>`;
}

function newDocModal(projectId) {
  if (!store.can('doc:write')) return '';
  return `
      <div class="modal" id="nd-modal" hidden>
        <div class="modal-box">
          <div class="modal-head neutral">新建文档</div>
          <div class="modal-body">
            <div class="form-grid">
              <label for="nd-title">标题</label>
              <input id="nd-title" class="ctl" maxlength="80" autocomplete="off"
                     placeholder="例如 第一章 取火">

              <label for="nd-ep">挂在哪个创作单元下</label>
              <select id="nd-ep" class="ctl">
                <option value="">全项目共用（总纲、人物小传这一类）</option>
              </select>

              <label for="nd-body" class="top">首版正文</label>
              <textarea id="nd-body" class="ctl" rows="5" placeholder="可以先空着，建好再写"></textarea>
            </div>
            <p class="modal-meta pf-hint">正文会写进工作区的 Markdown 文件并提交一次 Git；
              标题只影响给人看的名字，不改文件路径的规则。</p>
          </div>
          <div class="modal-actions">
            <button class="btn" id="nd-cancel">取消</button>
            <button class="btn primary" id="nd-save">建这份文档</button>
          </div>
        </div>
      </div>`;
}

function resetEditor() {
  draft = null; draftFor = null; draftVersion = null; serverCopy = null;
  notice = ''; lockWarn = ''; dirty = false; locking = false;
}

function mountDocs(root, tab) {
  const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
  bindSubChips(root, (i) => {
    if (i !== tab) resetEditor();          // 换的是另一类文档，草稿不能跟着带过去
    setSub('writing', i);
    refresh();
  });
  root.querySelector('[data-retry]')?.addEventListener('click', refresh);

  /* ---------- 列表：选一份、删一份 ---------- */
  root.querySelectorAll('[data-doc]').forEach((el) => {
    el.addEventListener('click', async (e) => {
      if (e.target.closest('button')) return;
      const id = Number(el.dataset.doc);
      if (id === pickedDocId) return;
      if (dirty && !(await confirmDanger({
        title: '切换文档',
        body: '这一份还有<b>没保存的改动</b>，切过去就丢了（后端没有它，工作区里也没有）。',
        okText: '丢掉并切换',
      }))) return;
      resetEditor();
      pickedDocId = id;
      openDetail('writing', id);           // 地址带上文档 id，刷新/粘贴回得来
    });
  });

  root.querySelectorAll('[data-del-doc]').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id = Number(btn.dataset.delDoc);
      if (!(await confirmDanger({
        title: '删除文档',
        body: `确定删除 <b>${escapeHtml(btn.dataset.title)}</b>？正文会从工作区里摘掉，`
            + '但那一稿还留在 Git 历史里，说一声就能找回。',
        okText: '删除',
      }))) return;
      const ok = await run('删除', () => api.deleteDocument(id));
      if (!ok) return;
      if (id === pickedDocId) { resetEditor(); pickedDocId = null; closeDetail('writing'); }
      refresh();
    });
  });

  /* ---------- 新建（弹窗）：低频动作不占主页面 ---------- */
  const modal = root.querySelector('#nd-modal');
  if (modal) {
    const titleEl = root.querySelector('#nd-title');
    const bodyEl = root.querySelector('#nd-body');
    const epEl = root.querySelector('#nd-ep');
    const saveBtn = root.querySelector('#nd-save');
    const close = () => { modal.hidden = true; saveBtn.disabled = false; };

    root.querySelector('#doc-new')?.addEventListener('click', async () => {
      titleEl.value = ''; bodyEl.value = '';
      epEl.innerHTML = '<option value="">全项目共用（总纲、人物小传这一类）</option>';
      // 集名单现取：这一页是集的东家，选项必须和分镜那一栏看到的是同一份名单
      try {
        const eps = await api.episodes(store.currentId);
        eps.forEach((ep) => {
          const opt = document.createElement('option');
          opt.value = ep.id;
          opt.textContent = `第 ${ep.seq} 集 · ${ep.title}`;
          epEl.appendChild(opt);
        });
      } catch { /* 拉不到就只给"全项目共用"，不挡新建 */ }
      modal.hidden = false;
      titleEl.focus();
    });

    const create = async () => {
      const title = titleEl.value.trim();
      if (!title) { titleEl.focus(); return; }
      saveBtn.disabled = true;
      let created = null;
      const ok = await run('新建', async () => {
        created = await api.createDocument(store.currentId, {
          kind: TAB_KIND[tab],
          title,
          episode_id: epEl.value ? Number(epEl.value) : null,
          content: bodyEl.value,
        });
      });
      saveBtn.disabled = false;
      if (!ok || !created) return;           // 失败时弹窗留着，填的字还在
      close();
      resetEditor();
      pickedDocId = created.id;
      refresh();
    };
    saveBtn?.addEventListener('click', create);
    root.querySelector('#nd-cancel')?.addEventListener('click', close);
    modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
    bindModalDismiss(modal, close);
  }

  /* ---------- 编辑器 ---------- */
  const ta = root.querySelector('#doc-text');
  if (!ta) return;
  const docId = pickedDocId;
  const countEl = root.querySelector('#doc-count');
  const saveBtn = root.querySelector('#doc-save');

  const setDirty = (v) => {
    dirty = v;
    const chip = root.querySelector('#doc-dirty');
    if (chip) {
      chip.className = 'chip ' + (v ? 'amber' : 'green');
      chip.textContent = v ? '未保存' : '与盘上一致';
    }
  };

  ta.addEventListener('input', () => {
    draft = ta.value;
    countEl.textContent = `${charCount(ta.value)} 字`;
    setDirty(true);
    // 一开始打字就顺手拿锁：界面能在别人那边显示"谁在改"，
    // 而不是等到保存被 423 挡回来才发现有人在写同一份
    if (!locking && draftVersion !== null) {
      locking = true;
      api.lockDocument(docId)
        .then((r) => {
          if (r.state === 'held') {
            lockWarn = `${r.holder_name} 正在编辑这一份（锁到 ${r.expires_at} 自己松开）。`
                     + '现在保存会被后端拦住，不会被覆盖。';
            paintBars(root);
          }
        })
        .catch(() => { locking = false; });   // 拿锁失败不挡住打字：保存那一关还有后端把关
    }
  });

  root.querySelector('#doc-status')?.addEventListener('change', async (e) => {
    const ok = await run('改状态', async () => {
      await api.setDocumentStatus(docId, e.target.value);
      await store.load();                 // 项目中心的"已定稿"计数跟着走
    });
    if (ok) refresh();
  });

  const save = async () => {
    if (saveBtn) saveBtn.disabled = true;
    notice = '';
    try {
      const out = await api.saveDocContent(docId, ta.value, draftVersion);
      draft = ta.value;
      draftVersion = out.version;
      serverCopy = null;
      setDirty(false);
      // 底部那行"当前提交 / 改动"要当场换掉：不换的话刚存完还写着旧的提交号与旧时间，
      // 人对着它没法判断这次到底存没存上（提示条只活到下一次操作）
      const commitEl = root.querySelector('#doc-commit');
      if (commitEl) {
        commitEl.textContent = out.git_commit
          ? `当前提交 ${out.git_commit.slice(0, 8)}` : '还没有提交记录';
      }
      const updEl = root.querySelector('#doc-updated');
      if (updEl) {
        updEl.textContent = `创建 ${updEl.dataset.created} · 改动 ${out.updated_at}`;
      }
      notice = out.changed
        ? `已保存并提交（${(out.git_commit || '').slice(0, 8)}）`
        : '内容和上一版一样，没有产生新提交';
      lockWarn = '';
      await store.load();
    } catch (err) {
      if (err.status === 409) {
        // 不覆盖、也不自动合并：把人正在写的东西留着，另把盘上那份取下来对照
        notice = '';
        lockWarn = '';
        try { serverCopy = (await api.docContent(docId)).content; } catch { serverCopy = '（取不到）'; }
      } else if (err.status === 423) {
        lockWarn = err.message;
      } else {
        notice = '';
        lockWarn = `${err.message}`;
      }
    } finally {
      if (saveBtn) saveBtn.disabled = false;
      paintBars(root);
    }
  };
  saveBtn?.addEventListener('click', save);
  ta.addEventListener('keydown', (e) => {
    // Cmd/Ctrl+S：写稿的人一定会按它。浏览器自己的"保存网页"不能放出来
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      if (saveBtn && !saveBtn.disabled) save();
    }
  });

  root.querySelector('#doc-conflict-use')?.addEventListener('click', () => {
    // 这一步会丢自己的草稿，所以二次确认
    confirmDanger({
      title: '用盘上这份',
      body: '你这边<b>没保存的草稿会被丢掉</b>，编辑器换成工作区里现在这份。',
      okText: '丢掉我的草稿',
    }).then((sure) => {
      if (!sure) return;
      draft = serverCopy;
      api.docContent(docId).then((d) => { draftVersion = d.version; }).catch(() => {});
      serverCopy = null;
      setDirty(false);
      refresh();
    });
  });
  root.querySelector('#doc-conflict-refresh')?.addEventListener('click', async () => {
    // 只换令牌，不换文本：等于"我对照过了，确认用我这份覆盖"
    const d = await api.docContent(docId).catch(() => null);
    if (!d) return;
    draftVersion = d.version;
    serverCopy = null;
    notice = '版本令牌已更新。再点「保存」会用你这边这份覆盖盘上那份。';
    refresh();
  });

  /* ---------- 版本历史 ---------- */
  const histBtn = root.querySelector('#doc-hist');
  const panel = root.querySelector('#doc-hist-panel');
  histBtn?.addEventListener('click', async () => {
    panel.hidden = !panel.hidden;
    if (panel.hidden) return;
    const listEl = root.querySelector('#doc-hist-list');
    try {
      const vs = await api.docVersions(docId);
      listEl.innerHTML = vs.length ? vs.map((v) => `
        <div class="ver" data-ver="${escapeHtml(v.commit)}">
          <span class="mono v-sha">${escapeHtml(v.commit.slice(0, 8))}</span>
          <span class="v-subj" title="${escapeHtml(v.subject)}">${escapeHtml(v.subject)}</span>
          <span class="v-when">${escapeHtml(v.at)} · ${escapeHtml(v.author)}</span>
          <button class="btn sm" data-diff>看差异</button>
        </div>`).join('') : '<p class="hint" style="text-align:left">还没有提交记录。</p>';
      listEl.querySelectorAll('[data-ver]').forEach((row) => {
        row.addEventListener('click', async () => {
          const pre = root.querySelector('#doc-diff');
          pre.hidden = false;
          pre.textContent = '载入中…';
          try {
            pre.textContent = (await api.docDiff(docId, row.dataset.ver)).diff;
          } catch (err) { pre.textContent = `读不到这一版：${err.message}`; }
        });
      });
    } catch (err) {
      listEl.innerHTML = `<p class="t-bad">${escapeHtml(err.message)}</p>`;
    }
  });
}

/**
 * 编辑器上方那三条提示（保存结果 / 别人持锁 / 版本冲突）的唯一画法。
 *
 * 为什么不用整页 refresh()：打字途中重绘会把光标位置和输入法未上屏的合成串一起弄丢，
 * 写稿的人对这两件事极其敏感。所以只动这一小块 DOM。
 * 唯一的例外是冲突对照（serverCopy）——那要摆两个 <pre>，结构变了，只能重绘整块；
 * 那时候人刚点了保存、不在输入状态，丢不了东西。
 */
function paintBars(root) {
  root.querySelectorAll('.bar').forEach((b) => b.remove());
  if (lockWarn) upsertBar(root, 'warn', lockWarn);
  if (notice && serverCopy === null) upsertBar(root, 'ok', notice);
  if (serverCopy !== null) {
    window.dispatchEvent(new CustomEvent('app:refresh'));
  }
}

function upsertBar(root, kind, text) {
  const bar = document.createElement('div');
  bar.className = `bar ${kind}`;
  bar.textContent = text;
  const ta = root.querySelector('#doc-text');
  (ta ? ta.parentElement : root.querySelector('.card')).insertBefore(
    bar, ta || root.querySelector('.card > *'));
  return bar;
}

/* =======================================================================
   分镜：一集一套（原有那页，一字不动地搬在下面）
   ======================================================================= */

async function renderShots(tab, subRow) {
  const project = store.current;
    if (!project) {
      return `<section class="view on"><div class="card"><div class="empty">
        <div class="empty-t">先选一个项目</div>
        <p>集挂在项目下面，去项目中心建一个再回来。</p>
      </div></div></section>`;
    }

    let episodes = [];
    let epErr = null;
    try { episodes = await api.episodes(project.id); } catch (err) { epErr = err; }
    if (epErr) {
      // 原来这里 `catch { episodes = [] }`，于是 403/500 会显示成"还没有集"——
      // 人以为数据没了，其实只是这一页没读到
      return `<section class="view on">
        <div class="page-head"><h1>创作中心</h1><p>分镜表</p></div>
        <div class="card">${errorBox('这一页的集', epErr)}</div>
      </section>`;
    }
    // 资产列表只为把镜头里的 asset_ids 翻成名字；资产内容不在这一页改。
    // 读不到不拦整页：镜头照常列，名字退化成 #id，并在上方说一句为什么退化
    let assets = [];
    let assetErr = null;
    try { assets = await api.assets(project.id); } catch (err) { assetErr = err; }
    const assetNames = new Map(assets.map((a) => [a.id, a.name]));

    const createEp = `
      <div class="card-body" style="border-top:1px solid var(--line);padding:8px">
        <div class="row" style="gap:6px">
          <input id="ep-title" class="ctl sm" placeholder="新集标题" style="flex:1;min-width:0">
          <button class="btn sm primary" id="ep-create">建一集</button>
        </div>
      </div>`;

    if (!episodes.length) {
      return `<section class="view on">
        <div class="page-head"><h1>创作中心</h1><p>分镜表</p></div>
        <div class="card">
          <div class="empty">
            <div class="empty-t">还没有集</div>
            <p>分镜挂在集下面，一集一套剧本与分镜。先建第一集。</p>
          </div>
          ${createEp}
        </div>
      </section>`;
    }

    // 从资产详情点「去创作中心」跳过来时带着要定位的那一集：
    // 用 navstate 的 param 槽传一次，读到就清掉，不越界去改对方的状态
    const focus = getParam('writing');
    if (focus) { pickedEpisodeId = focus; setParam('writing', undefined); }

    if (!episodes.some((e) => e.id === pickedEpisodeId)) {
      pickedEpisodeId = episodes[0].id;
    }
    const current = episodes.find((e) => e.id === pickedEpisodeId);

    let shots = [];
    let shotErr = null;
    try { shots = await api.shots(current.id); } catch (err) { shotErr = err; }

    const epList = episodes.map((e) => {
      const [label, tone] = EP_STATUS[e.status] || [e.status, 'gray'];
      return `
      <div class="t ${e.id === pickedEpisodeId ? 'on' : ''}" data-pick="${e.id}">
        第 ${e.seq} 集 · ${escapeHtml(e.title)}
        <span class="chip ${tone}" title="本集状态，改它用右侧的下拉">${label}</span>
        <span class="t-dim" style="float:right">${e.shot_count} 镜
          <button class="btn sm danger" data-del-ep="${e.id}"
                  data-label="第 ${e.seq} 集「${escapeHtml(e.title)}」"
                  data-shots="${e.shot_count}">删</button>
        </span>
      </div>`;
    }).join('');

    const shotRows = shots.map((s) => `
      <tr data-sid="${s.id}">
        <td class="mono t-dim">${String(s.seq).padStart(3, '0')}</td>
        <td>${escapeHtml(s.summary)}</td>
        <td>${s.asset_ids.length
          ? s.asset_ids.map((id) => `<span class="chip plain" data-goto-asset style="cursor:pointer"
                    title="资产内容在资产中心改">${escapeHtml(assetNames.get(id) || `#${id}`)}</span>`).join(' ')
          : '<span class="t-dim">—</span>'}</td>
        <td class="num">${s.duration}s</td>
        <td><span class="chip ${s.status === 'done' ? 'green' : s.status === 'failed' ? 'red' : 'gray'}">${
          { todo: '待生成', queued: '排队中', done: '已出片', failed: '失败' }[s.status] || s.status}</span></td>
        <td class="r">
          <button class="btn sm" data-gen-shot="${s.id}"
                  data-shot-summary="${escapeHtml(s.summary)}"
                  data-shot-prompt="${escapeHtml(s.prompt || '')}">生成</button>
          <button class="btn sm danger" data-del-shot="${s.id}">删除</button>
        </td>
      </tr>`).join('');

    return `
    <section class="view on">
      <div class="page-head">
        <h1>创作中心</h1>
        <p>分镜表 · ${escapeHtml(project.name)} / ${escapeHtml(current.title)}</p>
        <div class="right"><span class="chip plain">${shots.length} 镜</span>${subRow}</div>
      </div>

      <div style="display:grid;grid-template-columns:240px minmax(0,1fr);gap:14px;align-items:start">
        <div class="card tree">
          <div class="grp">创作单元 · 建与删都在这一栏</div>
          ${epList}
          ${createEp}
        </div>

        <div class="card">
          <div class="card-head">
            <h2>分镜</h2><span class="sub">一镜一行，生成作业按行提交</span>
            <div class="right">
              <span class="sub">本集状态</span>
              <select id="ep-status" class="ctl sm" style="width:auto"
                      title="改成「定稿」后，项目中心那一页的进度会跟着走">
                ${Object.entries(EP_STATUS).map(([key, [text]]) => `
                  <option value="${key}"${key === current.status ? ' selected' : ''}>${text}</option>`).join('')}
              </select>
            </div>
          </div>
          ${assetErr ? errorLine('资产名单', assetErr) : ''}
          ${shotErr ? errorBox('这一集的镜头', shotErr) : shots.length ? `
          <div class="table-wrap">
            <table>
            <thead><tr><th>镜号</th><th>内容</th><th>引用资产</th><th class="r">时长</th><th>状态</th><th class="r"></th></tr></thead>
            <tbody>${shotRows}</tbody>
            </table>
            </div>` : `
            <div class="empty">
            <div class="empty-t">这一集还没有镜头</div>
            <p>在下面加第一镜。提示词留空时，生成时用内容那一栏。</p>
            </div>`}

            <div class="card-body" style="border-top:1px solid var(--line)">
            <div class="empty-form" style="justify-content:flex-start">
            <input id="shot-summary" placeholder="这一镜发生什么" style="min-width:240px;flex:1">
            <input id="shot-prompt" placeholder="提示词（可留空）" style="min-width:180px;flex:1">
            <input id="shot-duration" type="number" min="1" max="60" step="1" value="5"
            class="ctl" style="min-width:72px;width:72px">
            <button class="btn primary" id="shot-create">加一镜</button>
            </div>
            <p class="hint" style="text-align:left;margin-top:8px">
            引用的资产在这里只显示名字，定妆图与一致性提示词到资产中心改。
            </p>
          </div>
        </div>
      </div>
    </section>`;
}

function mountShots(root) {
  const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));

    // 页内切换器与侧栏二级标签写同一个下标，两处永远同步
    bindSubChips(root, (i) => { setSub('writing', i); refresh(); });

    root.querySelectorAll('[data-pick]').forEach((el) => {
      el.addEventListener('click', (e) => {
        if (e.target.closest('button')) return;
        pickedEpisodeId = Number(el.dataset.pick);
        refresh();
      });
    });

    /* ---------- 集：建与删（Episode 的东家就是这一页） ---------- */
    const epTitle = root.querySelector('#ep-title');
    const epBtn = root.querySelector('#ep-create');
    if (epBtn) {
      const createEpisode = async () => {
        const title = epTitle.value.trim();
        if (!title) { epTitle.focus(); return; }
        epBtn.disabled = true;
        const ok = await run('新建', () => api.createEpisode(store.currentId, title));
        epBtn.disabled = false;
        if (!ok) return;
        refresh();
      };
      epBtn.addEventListener('click', createEpisode);
      epTitle.addEventListener('keydown', (e) => { if (e.key === 'Enter') createEpisode(); });
    }

    root.querySelectorAll('[data-del-ep]').forEach((btn) => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const n = Number(btn.dataset.shots);
        // 原来用原生 confirm()：样式跟两套主题脱节，而且"删一集"和"知道了"
        // 长得一模一样。危险操作统一走自绘弹窗（util.confirmDanger）
        const sure = await confirmDanger({
          title: '删除创作单元',
          body: `确定删除 <b>${escapeHtml(btn.dataset.label)}</b>？`
              + `它下面 <b>${n}</b> 条镜头会一起没，此操作不可撤销。`,
          okText: '删除',
        });
        if (!sure) return;
        const ok = await run('删除', () => api.deleteEpisode(Number(btn.dataset.delEp)));
        if (!ok) return;
        // 删的正是当前选中那集时，让 render() 退回列表第一集
        if (Number(btn.dataset.delEp) === pickedEpisodeId) pickedEpisodeId = null;
        refresh();
      });
    });

    // 本集状态：改完要顺手重拉项目列表，否则项目中心那一格的"已定稿 N/M 集"
    // 会停在旧数字上——它是 /api/projects 聚合出来的，不是本地能推出来的
    const epStatus = root.querySelector('#ep-status');
    epStatus?.addEventListener('change', async () => {
      const ok = await run('改状态', async () => {
        await api.setEpisodeStatus(pickedEpisodeId, epStatus.value);
        await store.load();
      });
      if (ok) refresh();
    });

    const summaryEl = root.querySelector('#shot-summary');
    const promptEl = root.querySelector('#shot-prompt');
    const durationEl = root.querySelector('#shot-duration');
    const createBtn = root.querySelector('#shot-create');

    if (createBtn) {
      const addShot = async () => {
        const summary = summaryEl.value.trim();
        if (!summary) { summaryEl.focus(); return; }
        createBtn.disabled = true;
        const ok = await run('添加', () => api.createShot(pickedEpisodeId, {
          summary,
          prompt: promptEl.value.trim(),
          duration: Number(durationEl.value) || 5,
        }));
        createBtn.disabled = false;
        if (!ok) return;
        refresh();
      };
      createBtn.addEventListener('click', addShot);
      summaryEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') addShot(); });
    }

    // 分镜 → 生成中心：把项目/集/镜/提示词一次交棒，不在这一页开第二个提交框。
    // 走 navstate 的一次性草稿而不是地址栏参数：刷新后不应该再弹一次。
    root.querySelectorAll('[data-gen-shot]').forEach((btn) => {
      btn.addEventListener('click', () => {
        setJobPrefill({
          projectId: store.currentId,
          episodeId: pickedEpisodeId,
          shotId: Number(btn.dataset.genShot),
          capability: 'video',
          prompt: (btn.dataset.shotPrompt || '').trim() || btn.dataset.shotSummary || '',
        });
        window.dispatchEvent(new CustomEvent('app:go', { detail: 'generate' }));
      });
    });

    root.querySelectorAll('[data-del-shot]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        if (!(await confirmDanger({ title: '删除镜头',
                                   body: '这一镜的内容、提示词与引用都会一起删掉。',
                                   okText: '删除' }))) return;
        // 同 assets.js：不包 run() 的话后端拒绝=静默，页面还照样刷新成"已删除"
        const ok = await run('删镜头', () => api.deleteShot(Number(btn.dataset.delShot)));
        if (!ok) return;
        refresh();
      });
    });

    /* ---------- 资产引用：只跳过去，不在这一页改 ---------- */
    root.querySelectorAll('[data-goto-asset]').forEach((chip) => {
      chip.addEventListener('click', () => {
        window.dispatchEvent(new CustomEvent('app:go', { detail: 'assets' }));
      });
    });
}
