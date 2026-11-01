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

// 分镜页这一轮渲染拿到的三份数据。表格里的"引用资产"要能按 id 反查名字、
// 点 chip 要知道去资产中心的哪个分类；作业列要按 shot_id 归组。
// 放在模块级是给 mount（）用——render 与 mount 是两次调用，靠 DOM 传对象
// 会把数据塞进 dataset；每次 render 整体覆盖，不跨项目残留。
let shotCache = { episodeId: null, shots: [], assets: [], jobs: [] };

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

/**
 * AI 操作（W11）。一次操作只活在一个 `ai` 对象里：
 *
 * - 打开弹窗时就把选区抄下来（`start/end/text`）。弹窗盖住正文之后，
 *   人不能再改选区，偏移必须固定在这份快照上，否则预览与写回会错位。
 * - `jobId` 一旦拿到就只认它：轮询、预览、应用都带着同一个号，
 *   页面重绘不会把"正在跑的那一条"弄丢。
 * - 关弹窗一定清定时器。留着的话，人已经走了，后台还在每 1.5 秒打一次接口。
 */
let ai = null;
const AI_POLL_MS = 1500;

// 文本抽取（W13）。和 AI 改写分开：抽取的产物是候选，不是一份新正文，
// 因此它有自己的弹窗、自己的轮询与自己的"确认入库"动作。
let ex = null;
const EX_POLL_MS = 2000;


// 前端的操作表只决定"弹窗怎么显示"：能不能做、怎么做，仍由后端
// docai.OPERATIONS 说了算。scope=document 的两条（起草/续写）在界面上
// 也不给"选中段落"这个选项，免得选了又被后端悄悄改回全文。
const AI_OPS = {
  draft: { label: '起草', scope: 'document', hint: '按标题与要求起一段新正文，应用后替换整篇。' },
  expand: { label: '扩写', scope: 'any', hint: '把选中的段落写得更充分；没选中就处理全文。' },
  continue: { label: '续写', scope: 'document', hint: '紧接正文最后一句往后写，应用时追加到文末。' },
  polish: { label: '润色', scope: 'any', hint: '只改表达与节奏，不改事实和人物口吻。' },
  scriptify: { label: '剧本化', scope: 'any', hint: '把小说段落改成场景／动作／对白的剧本格式。' },
  prompt: { label: '提示词', scope: 'any', hint: '从正文提炼镜头提示词，每镜一行。' },
};
const AI_TERMINAL = new Set(['succeeded', 'failed', 'cancelled', 'needs_attention']);

// 分镜表「作业」那一列的口径。状态本身由 Job 表说了算，镜头行只是读；
// 不用 shot.status——那一列没有写入方，拿它显示等于让界面说谎。
const SHOT_JOB_STATUS = {
  queued: { label: '排队中', cls: 'gray' },
  starting: { label: '启动中', cls: 'blue' },
  running: { label: '运行中', cls: 'blue' },
  succeeded: { label: '成功', cls: 'green' },
  failed: { label: '失败', cls: 'red' },
  cancel_requested: { label: '取消中', cls: 'amber' },
  cancelled: { label: '已取消', cls: 'gray' },
  needs_attention: { label: '待核对', cls: 'amber' },
};

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

  // 文档树按"全项目共用 / 第 N 集"分组。列表接口已经按 kind → 集序 → 标题
  // 排好，这里顺着顺序切组即可，不再自己排一遍（两处排序规则一分叉，
  // 界面上就会出现"树里顺序和接口顺序不一样"这种没人解释得清的事）。
  const groups = [];
  docs.forEach((d) => {
    const key = d.episode_id == null ? 'all' : String(d.episode_id);
    let group = groups.find((g) => g.key === key);
    if (!group) {
      group = {
        key,
        title: d.episode_title ? `第 ${d.episode_seq} 集 · ${d.episode_title}` : '全项目共用',
        rows: [],
      };
      groups.push(group);
    }
    group.rows.push(d);
  });

  const docRow = (d) => {
    const [label, tone] = DOC_STATUS[d.status] || [d.status, 'gray'];
    return `
      <div class="t ${d.id === pickedDocId ? 'on' : ''}" data-doc="${d.id}">
        ${escapeHtml(d.title)}
        <span class="chip ${tone}">${label}</span>
        ${d.lock_holder_name && d.lock_holder_id !== store.user?.id
          ? `<span class="chip amber" title="锁到 ${escapeHtml(d.lock_expires_at)}，超时自己松开">
               ${escapeHtml(d.lock_holder_name)} 在改</span>` : ''}
        <span class="t-dim" style="float:right">
          <button class="btn sm danger" data-del-doc="${d.id}" data-title="${escapeHtml(d.title)}"
                  ${canWrite ? '' : 'disabled'}>删</button>
        </span>
      </div>`;
  };

  const list = groups.map((g) => `
    <div class="grp doc-group"><span>${escapeHtml(g.title)}</span>
      <span class="t-dim">${g.rows.length} 份</span></div>
    ${g.rows.map(docRow).join('')}`).join('');

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
    ${aiModalHtml()}
    ${extractModalHtml()}
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
          <button class="btn sm" id="doc-ai"
                  title="起草／扩写／续写／润色／剧本化／提示词；结果先看 diff，确认后才写回">AI 改写</button>
          <button class="btn sm" id="doc-extract"
                  title="从正文抽取角色／场景／道具／镜头候选；证据要逐字对得上，人确认后才入资产">抽取</button>
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
  closeAi();
}

/**
 * AI 弹窗的外壳。里面的内容分两幕：先填要求与确认计价，提交后原地换成
 * 预览（diff + 应用）。为什么不另开一个弹窗：换窗会让人以为"刚才那一步
 * 已经写进去了"，而实际上正文此刻一个字都没动。
 */
function aiModalHtml() {
  if (!store.can('doc:write')) return '';
  return `
    <div class="modal" id="ai-modal" hidden>
      <div class="modal-box wide">
        <div class="modal-head neutral" id="ai-head">AI 改写</div>
        <div class="modal-body" id="ai-body"></div>
        <div class="modal-actions" id="ai-actions"></div>
      </div>
    </div>`;
}

function extractModalHtml() {
  if (!store.can('doc:write')) return '';
  return `
    <div class="modal" id="ex-modal" hidden>
      <div class="modal-box wide">
        <div class="modal-head neutral">抽取候选</div>
        <div class="modal-body" id="ex-body"></div>
        <div class="modal-actions" id="ex-actions"></div>
      </div>
    </div>`;
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

  /* ---------- AI 改写（W11） ---------- */
  const aiModal = root.querySelector('#ai-modal');
  if (aiModal) {
    const openAi = async () => {
      // 有没保存的改动先问一句：AI 以**盘上**正文为基准，
      // 直接提交再应用，编辑器里那些字会被覆盖，而后端从来没收到过它们。
      if (dirty && !(await confirmDanger({
        title: '先保存再做 AI',
        body: '这一份还有<b>没保存的改动</b>。AI 读的是工作区里那一份；'
            + '直接开始的话，应用结果时这些改动会被覆盖。',
        okText: '丢掉改动，继续',
      }))) return;
      const start = ta.selectionStart ?? 0;
      const end = ta.selectionEnd ?? 0;
      const selected = ta.value.slice(start, end);
      closeAi();
      ai = {
        docId,
        operation: 'polish',
        scope: selected.trim() ? 'selection' : 'document',
        start, end, text: selected,
        instruction: '',
        options: null,
        error: '',
        jobId: null, preview: null, timer: null, busy: false,
      };
      aiModal.hidden = false;
      paintAiForm(root);
    };
    root.querySelector('#doc-ai')?.addEventListener('click', openAi);
    aiModal.addEventListener('click', (e) => {
      if (e.target === aiModal) closeAi();
    });
    bindModalDismiss(aiModal, closeAi);
  }

  /* ---------- 抽取候选（W13） ---------- */
  const exModal = root.querySelector('#ex-modal');
  if (exModal) {
    const openEx = async () => {
      // 抽取读的是盘上正文；没保存的改动没进文件，抽出来会缺这一截
      if (dirty && !(await confirmDanger({
        title: '先保存再抽取',
        body: '这一份还有<b>没保存的改动</b>。抽取读的是工作区里那一份，'
            + '现在开始的话，编辑器里这些字不会进候选。',
        okText: '丢掉改动，继续',
      }))) return;
      closeEx();
      ex = {
        docId, pid: store.currentId, options: null, run: null,
        items: [], assets: [], kind: 'character', error: '', busy: false,
        instruction: '', confirm: false, timer: null,
      };
      exModal.hidden = false;
      paintEx(root);
      await loadExOptions(root);
    };
    root.querySelector('#doc-extract')?.addEventListener('click', openEx);
    exModal.addEventListener('click', (e) => { if (e.target === exModal) closeEx(); });
    bindModalDismiss(exModal, closeEx);
  }
}

function closeEx() {
  if (ex?.timer) clearTimeout(ex.timer);
  ex = null;
  const modal = document.getElementById('ex-modal');
  if (modal) modal.hidden = true;
}

const EX_FIELDS = {
  character: [['role', '定位'], ['appearance', '外形'], ['personality', '性格'],
              ['suggested_prompt', '建议提示词']],
  scene: [['time_of_day', '时间'], ['weather', '天气'], ['description', '描述'],
          ['suggested_prompt', '建议提示词']],
  prop: [['description', '描述'], ['suggested_prompt', '建议提示词']],
  shot: [['scene', '场景'], ['summary', '内容'], ['shot_size', '景别'],
         ['camera_move', '运镜'], ['duration_hint', '秒'], ['asset_refs', '引用']],
};

async function loadExOptions(root) {
  if (!ex) return;
  try {
    ex.options = await api.extractOptions(ex.pid, ex.docId);
    ex.assets = await api.assets(ex.pid);
  } catch (err) {
    ex.error = err.message;
  }
  paintEx(root);
}

function paintEx(root) {
  const body = root.querySelector('#ex-body');
  const actions = root.querySelector('#ex-actions');
  if (!body || !actions || !ex) return;
  const err = ex.error ? `<p class="modal-err">${escapeHtml(ex.error)}</p>` : '';
  if (!ex.options) {
    body.innerHTML = `<p class="modal-meta">正在取计价与健康…</p>${err}`;
    actions.innerHTML = '<button class="btn" id="ex-close">关闭</button>';
    actions.querySelector('#ex-close')?.addEventListener('click', closeEx);
    return;
  }
  const o = ex.options;
  const price = (o.unit_price_micros / 1_000_000).toFixed(2);
  if (!ex.run) {
    body.innerHTML = `
      <p class="modal-meta">从《${escapeHtml(o.doc_title)}》里抽角色／场景／道具／镜头。
        每条候选都必须带回原文逐字证据；<b>人点入库之前，资产库一行都不写</b>。</p>
      <div class="ai-price">
        <div><span class="grp">文档</span><b>${escapeHtml(o.doc_title)}</b></div>
        <div><span class="grp">正文</span><b>${o.chars} 字</b></div>
        <div><span class="grp">模型</span><b>${escapeHtml(o.provider)} / ${escapeHtml(o.model)}</b></div>
        <div><span class="grp">单价</span><b>¥${price} / 百万 Token</b></div>
        <div><span class="grp">预计用量</span><b>${o.estimated_quantity} 百万 Token</b></div>
        <div><span class="grp">健康</span><b>${escapeHtml(o.health_detail)}</b></div>
      </div>
      <label class="modal-label" for="ex-instruction">额外要求（可不填，最多 2000 字）</label>
      <textarea id="ex-instruction" class="ctl" rows="2" maxlength="2000"
                placeholder="例如：只抽这一章出场的人物；镜头按段落切">${escapeHtml(ex.instruction)}</textarea>
      <label class="ai-confirm">
        <input type="checkbox" id="ex-confirm" ${ex.confirm ? 'checked' : ''}>
        <span>我确认这次按供应商计费；同一份正文 24 小时内重复提交不会再花钱</span>
      </label>
      ${err}`;
    actions.innerHTML = `
      <button class="btn" id="ex-close">取消</button>
      <button class="btn primary" id="ex-start"
              ${o.health_state === 'ok' ? '' : 'disabled'}>开始抽取</button>`;
    body.querySelector('#ex-instruction')?.addEventListener('input', (e) => {
      ex.instruction = e.target.value;
    });
    body.querySelector('#ex-confirm')?.addEventListener('change', (e) => {
      ex.confirm = e.target.checked;
    });
    actions.querySelector('#ex-close')?.addEventListener('click', closeEx);
    actions.querySelector('#ex-start')?.addEventListener('click', () => submitEx(root));
    return;
  }

  const run = ex.run;
  if (run.status === 'pending') {
    body.innerHTML = `
      <p class="modal-meta">作业 <b>#${run.job_id}</b> 已入队；模型跑完后候选才会出现。</p>
      <div class="ai-status"><span class="chip blue">${escapeHtml(run.status_label)}</span>
        <span class="t-dim">每 2 秒看一次状态</span></div>
      ${err}`;
    actions.innerHTML = '<button class="btn" id="ex-close">收起</button>';
    actions.querySelector('#ex-close')?.addEventListener('click', closeEx);
    return;
  }
  if (run.status === 'failed' || run.status === 'parse_failed') {
    body.innerHTML = `
      <div class="bar bad">${escapeHtml(run.note || run.error_message || '这次抽取没有成功')}</div>
      <p class="modal-meta">原文与资产都没有被改。可以调整要求后重跑一次。</p>${err}`;
    actions.innerHTML = `
      <button class="btn" id="ex-close">关闭</button>
      <button class="btn primary" id="ex-again">重新抽取</button>`;
    actions.querySelector('#ex-close')?.addEventListener('click', closeEx);
    actions.querySelector('#ex-again')?.addEventListener('click', () => {
      ex.run = null; ex.items = []; ex.error = ''; paintEx(root);
    });
    return;
  }

  if (run.stale) {
    // 原文在抽取之后改过：证据偏移对的是旧版，不能拿它入库
    body.innerHTML = `<div class="bar bad">文档在抽取之后被改过，这批候选的证据已失配；
      只能看，不能入库。需要就重新抽一次。</div>`;
    actions.innerHTML = '<button class="btn" id="ex-close">关闭</button>';
    actions.querySelector('#ex-close')?.addEventListener('click', closeEx);
    return;
  }

  const kinds = ['character', 'scene', 'prop', 'shot'];
  const labels = { character: '角色', scene: '场景', prop: '道具', shot: '镜头' };
  const list = ex.items.filter((i) => i.kind === ex.kind);
  const tabs = kinds.map((k) => {
    const n = ex.items.filter((i) => i.kind === k).length;
    return `<button type="button" class="chip ${k === ex.kind ? 'blue' : 'plain'}"
              data-ex-kind="${k}">${labels[k]} ${n}</button>`;
  }).join('');
  const cards = list.map((item) => exCard(item, root)).join('')
    || '<p class="hint" style="text-align:left">这一类没有候选。</p>';
  const exportBtn = ex.kind === 'shot' && list.length
    ? `<a class="btn sm" target="_blank" rel="noopener"
          href="/api/extract/runs/${run.id}/shots.md">导出 Markdown</a>` : '';
  body.innerHTML = `
    <div class="row" style="justify-content:space-between;flex-wrap:wrap;gap:8px">
      <div class="ai-ops" id="ex-kinds">${tabs}</div>
      <span class="t-desc">共 ${ex.items.length} 条 · 待处理 ${run.counts.pending} ·
        已入库 ${run.counts.accepted} · 已合并 ${run.counts.merged} · 已驳回 ${run.counts.rejected}</span>
    </div>
    <p class="modal-meta">${escapeHtml(run.note || '')}</p>
    <div class="ex-list">${cards}</div>
    ${err}`;
  actions.innerHTML = `
    <button class="btn" id="ex-close">关闭</button>${exportBtn}
    <a class="btn" href="#assets/0" id="ex-goto-assets">去资产中心看结果</a>`;
  body.querySelectorAll('[data-ex-kind]').forEach((btn) => {
    btn.addEventListener('click', () => { ex.kind = btn.dataset.exKind; paintEx(root); });
  });
  actions.querySelector('#ex-close')?.addEventListener('click', closeEx);
  actions.querySelector('#ex-goto-assets')?.addEventListener('click', () => {
    closeEx();
    window.dispatchEvent(new CustomEvent('app:go', { detail: 'assets' }));
  });
  bindExCards(root);
}

function exCard(item, root) {
  const p = item.payload || {};
  const title = p.name || p.summary || `候选 #${item.id}`;
  const fields = (EX_FIELDS[item.kind] || []).filter(([key]) => p[key]).map(([key, label]) => {
    const value = Array.isArray(p[key]) ? p[key].join('、') : p[key];
    return `<div><span class="grp">${label}</span>${escapeHtml(String(value))}</div>`;
  }).join('');
  const evidence = item.evidence || {};
  const targets = ex.kind === 'shot' ? '' : `
      <select class="ctl sm" data-ex-target="${item.id}">
        <option value="">并到已有资产…</option>
        ${ex.assets.filter((a) => a.kind === item.kind).map((a) =>
          `<option value="${a.id}">${escapeHtml(a.name)}${a.project_id === null ? ' · 全局' : ''}</option>`).join('')}
      </select>
      <button class="btn sm" data-ex-merge="${item.id}">合并</button>
      <button class="btn sm" data-ex-accept="${item.id}">入库</button>`;
  return `
    <div class="ex-card">
      <div class="row" style="justify-content:space-between;gap:8px">
        <div><span class="chip ${item.kind === 'shot' ? 'amber' : 'blue'}">${item.kind_label}</span>
          <b>${escapeHtml(title)}</b>
          ${item.confidence != null ? `<span class="t-desc">把握 ${item.confidence}</span>` : ''}</div>
        <span class="chip ${item.status === 'pending' ? 'gray' : 'green'}">${item.status_label}</span>
      </div>
      <div class="ex-fields">${fields}</div>
      ${evidence.quote ? `<blockquote class="ex-quote">${escapeHtml(evidence.quote)}
        <span class="t-desc">原文 ${evidence.start}–${evidence.end}</span></blockquote>` : ''}
      <div class="row" style="gap:6px;flex-wrap:wrap;margin-top:8px">
        ${item.status === 'pending' ? targets : ''}
        ${item.status === 'pending' && item.kind !== 'shot'
          ? `<button class="btn sm danger" data-ex-reject="${item.id}">驳回</button>` : ''}
      </div>
    </div>`;
}

function bindExCards(root) {
  const refresh = async () => {
    if (!ex) return;
    try {
      ex.run = await api.extractRun(ex.run.id);
      ex.items = await api.extractItems(ex.run.id);
      ex.error = '';
    } catch (err) { ex.error = err.message; }
    paintEx(root);
  };
  root.querySelectorAll('[data-ex-accept]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await api.extractAccept(Number(btn.dataset.exAccept));
      } catch (err) { ex.error = err.message; btn.disabled = false; paintEx(root); return; }
      await refresh();
    });
  });
  root.querySelectorAll('[data-ex-merge]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const id = Number(btn.dataset.exMerge);
      const select = root.querySelector(`[data-ex-target="${id}"]`);
      const target = Number(select?.value || 0);
      if (!target) { ex.error = '先在右边选一个已有资产再合并'; paintEx(root); return; }
      btn.disabled = true;
      try {
        await api.extractMerge(id, target);
      } catch (err) { ex.error = err.message; btn.disabled = false; paintEx(root); return; }
      await refresh();
    });
  });
  root.querySelectorAll('[data-ex-reject]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await api.extractReject(Number(btn.dataset.exReject), '人工驳回');
      } catch (err) { ex.error = err.message; btn.disabled = false; paintEx(root); return; }
      await refresh();
    });
  });
}

async function submitEx(root) {
  if (!ex || ex.busy) return;
  if (!ex.confirm) { ex.error = '先勾选“我确认这次按供应商计费”'; paintEx(root); return; }
  if (!ex.options || ex.options.health_state !== 'ok') {
    ex.error = '文本 provider 现在不可用'; paintEx(root); return;
  }
  const o = ex.options;
  ex.busy = true;
  ex.error = '';
  try {
    const run = await api.extractSubmit(ex.pid, {
      project_id: ex.pid, doc_id: ex.docId, instruction: ex.instruction,
      estimate: { quantity: o.estimated_quantity, unit: o.unit,
                  unit_price_micros: o.unit_price_micros,
                  currency: o.currency, source: o.source },
      user_confirmed_cloud: true,
    });
    ex.run = run;
    ex.items = [];
    if (run.status === 'pending') {
      paintEx(root);
      pollEx(root);
    } else {
      ex.items = await api.extractItems(run.id);
      paintEx(root);
    }
  } catch (err) {
    ex.error = err.message;
    paintEx(root);
  } finally {
    ex.busy = false;
  }
}

function pollEx(root) {
  if (!ex) return;
  ex.timer = setTimeout(async () => {
    if (!ex || !ex.run) return;
    try {
      ex.run = await api.extractRun(ex.run.id);
      if (ex.run.status === 'pending') { paintEx(root); pollEx(root); return; }
      ex.items = await api.extractItems(ex.run.id);
    } catch (err) { ex.error = err.message; }
    paintEx(root);
  }, EX_POLL_MS);
}

function closeAi() {
  if (ai?.timer) clearTimeout(ai.timer);
  ai = null;
  const modal = document.getElementById('ai-modal');
  if (modal) modal.hidden = true;
}

/** 弹窗第一幕：操作、范围、附加要求、计价与确认。 */
function paintAiForm(root) {
  const body = root.querySelector('#ai-body');
  const actions = root.querySelector('#ai-actions');
  if (!body || !actions || !ai) return;
  const locked = AI_OPS[ai.operation]?.scope === 'document';
  body.innerHTML = `
    <p class="modal-meta">选一种操作，必要时写两句附加要求。提交后先看 diff，
      <b>确认之前正文不会变</b>；应用时写回工作区并生成一次 Git 提交。</p>

    <span class="modal-label">操作</span>
    <div class="ai-ops" id="ai-ops">
      ${Object.entries(AI_OPS).map(([key, info]) => `
        <button type="button" class="chip ${key === ai.operation ? 'blue' : 'plain'}"
                data-ai-op="${key}">${info.label}</button>`).join('')}
    </div>

    <span class="modal-label">处理范围</span>
    <div class="ai-ops" id="ai-scopes">
      <button type="button" class="chip plain" data-ai-scope="document">整篇</button>
      <button type="button" class="chip plain" data-ai-scope="selection"
              ${ai.text.trim() ? '' : 'disabled title="先在正文里选中一段"'}>选中段落</button>
    </div>
    <p class="modal-meta" id="ai-scope-note"></p>

    <label class="modal-label" for="ai-instruction">附加要求（可不填，最多 2000 字）</label>
    <textarea id="ai-instruction" class="ctl" rows="3" maxlength="2000"
              placeholder="例如：保留第一人称；对白更口语；不要新增人物">${escapeHtml(ai.instruction)}</textarea>

    <span class="modal-label">这次要花多少钱</span>
    <div id="ai-price" class="ai-price"><span class="t-dim">正在取计价…</span></div>

    <label class="ai-confirm">
      <input type="checkbox" id="ai-confirm">
      <span>我确认这次按供应商计费（文本同样是云端调用，价格以清单为准）</span>
    </label>
    <p class="modal-err" id="ai-error">${escapeHtml(ai.error)}</p>`;
  actions.innerHTML = `
    <button class="btn" id="ai-cancel">取消</button>
    <button class="btn primary" id="ai-submit">提交并预览</button>`;

  body.querySelectorAll('[data-ai-op]').forEach((btn) => {
    btn.addEventListener('click', () => {
      ai.operation = btn.dataset.aiOp;
      if (AI_OPS[ai.operation].scope === 'document') ai.scope = 'document';
      ai.options = null;
      syncAiForm(root);
      loadAiOptions(root);
    });
  });
  body.querySelectorAll('[data-ai-scope]').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (btn.disabled) return;
      ai.scope = btn.dataset.aiScope;
      ai.options = null;
      syncAiForm(root);
      loadAiOptions(root);
    });
  });
  body.querySelector('#ai-instruction')?.addEventListener('input', (e) => {
    ai.instruction = e.target.value;
  });
  actions.querySelector('#ai-cancel')?.addEventListener('click', closeAi);
  actions.querySelector('#ai-submit')?.addEventListener('click', () => submitAi(root));
  syncAiForm(root);
  loadAiOptions(root);
}

/** 只改选中态与提示，不重画输入框——重画会把刚写的附加要求洗掉。 */
function syncAiForm(root) {
  if (!ai) return;
  const info = AI_OPS[ai.operation];
  const locked = info.scope === 'document';
  root.querySelectorAll('[data-ai-op]').forEach((btn) => {
    btn.className = `chip ${btn.dataset.aiOp === ai.operation ? 'blue' : 'plain'}`;
  });
  root.querySelectorAll('[data-ai-scope]').forEach((btn) => {
    const on = btn.dataset.aiScope === ai.scope;
    btn.className = `chip ${on ? 'blue' : 'plain'}`;
    if (locked && btn.dataset.aiScope === 'selection') btn.disabled = true;
  });
  const note = root.querySelector('#ai-scope-note');
  if (note) {
    note.textContent = locked
      ? `${info.label}固定处理整篇：${info.hint}`
      : (ai.scope === 'selection'
          ? `只处理选中的 ${charCount(ai.text)} 字：${info.hint}`
          : `处理整篇 ${charCount(draft ?? '')} 字：${info.hint}`);
  }
}

async function loadAiOptions(root) {
  if (!ai) return;
  const box = root.querySelector('#ai-price');
  if (!box) return;
  const wanted = `${ai.operation}/${ai.scope}/${ai.start}/${ai.end}`;
  box.innerHTML = '<span class="t-dim">正在取计价…</span>';
  try {
    const data = await api.docAiOptions(ai.docId, {
      operation: ai.operation, scope: ai.scope,
      start: ai.start, end: ai.end, selection_text: ai.text,
    });
    // 期间人又换了操作：这次回来的旧报价不能盖掉新的
    if (!ai || `${ai.operation}/${ai.scope}/${ai.start}/${ai.end}` !== wanted) return;
    ai.options = data;
    box.innerHTML = aiPriceHtml(data);
    const btn = root.querySelector('#ai-submit');
    if (btn) btn.disabled = data.health_state !== 'ok';
  } catch (err) {
    if (!ai) return;
    ai.options = null;
    box.innerHTML = `<span class="t-bad">${escapeHtml('取不到计价：' + err.message)}</span>`;
    const btn = root.querySelector('#ai-submit');
    if (btn) btn.disabled = true;
  }
}

function aiPriceHtml(o) {
  const unit = { million_token: '百万 Token', token: 'Token', character: '千字' }[o.unit] || o.unit;
  const priceYuan = (o.unit_price_micros / 1_000_000).toFixed(2);
  const costMicros = Math.round(o.estimated_quantity * o.unit_price_micros);
  const costYuan = (costMicros / 1_000_000).toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
  const tokens = Math.round(o.estimated_quantity * 1_000_000);
  const health = o.health_state === 'ok'
    ? '<span class="chip green">可用</span>'
    : `<span class="chip amber">${escapeHtml(o.health_state)}</span>
       <span class="t-dim">${escapeHtml(o.health_detail)}</span>`;
  return `
    <div><span class="grp">供应商 / 模型</span><b>${escapeHtml(o.provider)} · ${escapeHtml(o.model)}</b></div>
    <div><span class="grp">健康</span>${health}</div>
    <div><span class="grp">单价</span><b>¥${priceYuan}</b> / ${escapeHtml(unit)}
      <span class="t-dim">（清单价）</span></div>
    <div><span class="grp">本次输入</span><b>${o.chars}</b> 字，输出上限 ${o.max_tokens} token</div>
    <div><span class="grp">预计用量</span><b>${o.estimated_quantity}</b> ${escapeHtml(unit)}
      <span class="t-dim">≈ ${tokens.toLocaleString()} token</span></div>
    <div><span class="grp">预计费用</span><b>¥${costYuan}</b>
      <span class="t-dim">（${escapeHtml(o.currency)}，向上取整到 0.001）</span></div>
    <div class="ai-source"><span class="grp">价格来源</span>
      <span class="t-dim">${escapeHtml(o.source)}</span></div>`;
}

async function submitAi(root) {
  if (!ai || ai.busy) return;
  const errEl = root.querySelector('#ai-error');
  const setErr = (text) => { if (errEl) errEl.textContent = text; };
  if (!ai.options) { setErr('计价还没取到，稍等一下再提交'); return; }
  if (ai.options.health_state !== 'ok') {
    setErr(`文本 provider 现在不可用：${ai.options.health_detail}`);
    return;
  }
  if (!root.querySelector('#ai-confirm')?.checked) {
    setErr('先勾选"确认按供应商计费"，再提交');
    return;
  }
  ai.busy = true;
  const btn = root.querySelector('#ai-submit');
  if (btn) btn.disabled = true;
  setErr('');
  const o = ai.options;
  try {
    const job = await api.docAiSubmit(ai.docId, {
      operation: ai.operation,
      instruction: ai.instruction,
      scope: ai.scope,
      start: ai.start,
      end: ai.end,
      selection_text: ai.text,
      // 数量与单价来自服务端刚才那份报价，人只勾"确认"，不填总价
      estimate: {
        quantity: o.estimated_quantity, unit: o.unit,
        unit_price_micros: o.unit_price_micros, currency: o.currency,
        source: o.source,
      },
      user_confirmed_cloud: true,
    });
    ai.jobId = job.job_id;
    ai.preview = { ...job, can_apply: false, conflict: false, diff: '', content: '',
                   note: '作业已排队，原文现在一个字都没变。' };
    paintAiPreview(root);
    pollAi(root, ai.docId, job.job_id);
  } catch (err) {
    setErr(err.message);
  } finally {
    if (ai) ai.busy = false;
    const again = root.querySelector('#ai-submit');
    if (again) again.disabled = false;
  }
}

/** 第二幕：作业状态与 diff。轮询期间每一拍都重画这一块。 */
function paintAiPreview(root) {
  const body = root.querySelector('#ai-body');
  const actions = root.querySelector('#ai-actions');
  const head = root.querySelector('#ai-head');
  if (!body || !actions || !ai) return;
  const p = ai.preview || {};
  const done = p.status === 'succeeded';
  const terminal = AI_TERMINAL.has(p.status);
  if (head) head.textContent = `AI ${p.operation_label || AI_OPS[ai.operation]?.label || '改写'} · 作业 #${ai.jobId}`;

  const tone = p.status === 'succeeded' ? 'green'
    : p.status === 'failed' || p.status === 'cancelled' ? 'red' : 'blue';
  body.innerHTML = `
    <p class="modal-meta">结果先放在这里对照，<b>还没有写进正文</b>。
      应用后写工作区并生成一次 Git 提交；失败时原文保持不动。</p>
    <div class="ai-status">
      <span class="chip ${tone}">${escapeHtml(p.status_label || p.status || '排队中')}</span>
      ${p.base_version ? `<span class="t-dim">基线 ${escapeHtml(String(p.base_version).slice(0, 18))}…</span>` : ''}
      ${!terminal ? '<span class="t-dim">worker 正在跑，界面每 1.5 秒问一次</span>' : ''}
    </div>
    ${p.note ? `<p class="modal-meta">${escapeHtml(p.note)}</p>` : ''}
    ${p.error_message ? `<p class="modal-err">${escapeHtml(p.error_message)}</p>` : ''}
    ${ai.error ? `<p class="modal-err">${escapeHtml(ai.error)}</p>` : ''}
    ${p.diff ? `<pre class="diff ai" id="ai-diff">${escapeHtml(p.diff)}</pre>` : ''}
    ${done && p.can_apply ? `<p class="modal-meta">应用后约 ${p.applied_chars} 字
      （AI 结果 ${p.result_chars} 字，盘上原文 ${p.base_chars} 字）。</p>` : ''}`;

  actions.innerHTML = `
    <button class="btn" id="ai-close">关闭</button>
    ${done && p.can_apply ? '<button class="btn primary" id="ai-apply">应用并提交</button>' : ''}
    ${terminal && !p.can_apply ? '<button class="btn" id="ai-again">改一下再试</button>' : ''}`;
  actions.querySelector('#ai-close')?.addEventListener('click', closeAi);
  actions.querySelector('#ai-again')?.addEventListener('click', () => {
    if (!ai) return;
    ai.jobId = null; ai.preview = null; ai.error = '';
    if (root.querySelector('#ai-head')) root.querySelector('#ai-head').textContent = 'AI 改写';
    paintAiForm(root);
  });
  actions.querySelector('#ai-apply')?.addEventListener('click', () => applyAi(root));
}

async function pollAi(root, docId, jobId) {
  if (!ai || ai.jobId !== jobId) return;
  // 人已经切到别的页：这一块 DOM 不在文档里了，继续轮询只会白打接口
  if (!document.body.contains(root)) { closeAi(); return; }
  try {
    const data = await api.docAiJob(docId, jobId);
    if (!ai || ai.jobId !== jobId) return;
    ai.preview = data;
    paintAiPreview(root);
    if (!AI_TERMINAL.has(data.status)) {
      ai.timer = setTimeout(() => pollAi(root, docId, jobId), AI_POLL_MS);
    }
  } catch (err) {
    if (!ai || ai.jobId !== jobId) return;
    ai.error = `查不到作业状态：${err.message}`;
    paintAiPreview(root);
  }
}

async function applyAi(root) {
  if (!ai || ai.busy || !ai.jobId) return;
  ai.busy = true;
  ai.error = '';
  const btn = root.querySelector('#ai-apply');
  if (btn) { btn.disabled = true; btn.textContent = '正在写回…'; }
  try {
    const out = await api.docAiApply(ai.docId, ai.jobId);
    const label = out.operation_label || '改写';
    // 不自己拼"应用后的正文"：让下一次 render 重新读盘。
    // 把三件缓存清空就够了——服务端那份才是唯一的真相。
    draft = null; draftFor = null; draftVersion = null;
    serverCopy = null; dirty = false; lockWarn = '';
    const short = (out.git_commit || '').slice(0, 8);
    notice = `AI ${label}已应用并提交${short ? `（${short}）` : ''}`;
    closeAi();
    window.dispatchEvent(new CustomEvent('app:refresh'));
  } catch (err) {
    ai.busy = false;
    ai.error = err.status === 423
      ? `${err.message}。等对方保存或锁过期，再点一次应用。`
      : err.status === 409
        ? `${err.message}。原文没有动，重新跑一次更稳。`
        : err.message;
    paintAiPreview(root);
  }
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
    const canWrite = store.can('doc:write');
    const canSubmit = store.can('job:submit');

    let shots = [];
    let shotErr = null;
    try { shots = await api.shots(current.id); } catch (err) { shotErr = err; }

    // 一集的作业一次取回，前端按 shot_id 分组。不按行各发一条：
    // 20 镜的集就会变成 20 个请求，而作业列只需要"几条 + 最新状态"。
    let jobRows = [];
    let jobErr = null;
    try { jobRows = await api.jobs({ episode_id: current.id, limit: 200 }); }
    catch (err) { jobErr = err; }
    const jobsByShot = new Map();
    jobRows.forEach((job) => {
      if (job.shot_id == null) return;
      if (!jobsByShot.has(job.shot_id)) jobsByShot.set(job.shot_id, []);
      jobsByShot.get(job.shot_id).push(job);
    });

    shotCache = { episodeId: current.id, shots, assets, jobs: jobRows };

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

    const assetChip = (id) => {
      const asset = assets.find((a) => a.id === id);
      const name = asset ? asset.name : `#${id}`;
      const kind = asset ? asset.kind_label : '资产';
      const scope = asset && asset.project_id === null ? ' · 全局' : '';
      return `<span class="chip plain shot-asset" data-goto-asset="${id}"
                    title="${escapeHtml(kind + scope + '：到资产中心查看，不在这一页改')}">
                ${escapeHtml(name)}</span>`;
    };

    // 作业列只说事实：几条、最新那条什么状态。没有按钮——
    // 改作业状态不在这一页，重试/取消都在生成中心。
    const jobCell = (list) => {
      if (!list.length) return '<span class="chip gray">未提交</span>';
      const latest = list[0];
      const meta = SHOT_JOB_STATUS[latest.status] || { label: latest.status, cls: 'gray' };
      const detail = list.map((j) => `#${j.id} ${j.status_label}`).join('；');
      return `<span class="chip ${meta.cls}" title="${escapeHtml(detail)}">${meta.label}</span>
              <span class="t-desc" title="${escapeHtml(detail)}">${list.length} 条 · #${latest.id}</span>`;
    };

    const shotRows = shots.map((s) => `
      <tr data-sid="${s.id}">
        <td class="mono t-dim">${String(s.seq).padStart(3, '0')}</td>
        <td>
          <div class="shot-summary">${escapeHtml(s.summary)}</div>
          ${s.prompt ? `<div class="t-desc shot-prompt" title="${escapeHtml(s.prompt)}">
            ${escapeHtml(s.prompt)}</div>` : ''}
        </td>
        <td>${s.asset_ids.length
          ? s.asset_ids.map(assetChip).join(' ')
          : '<span class="t-dim">—</span>'}</td>
        <td class="num">${s.duration}s</td>
        <td class="shot-jobs">${jobCell(jobsByShot.get(s.id) || [])}</td>
        <td class="r">
          ${canWrite ? `<button class="btn sm" data-edit-shot="${s.id}">编辑</button>` : ''}
          ${canSubmit ? `<button class="btn sm" data-gen-shot="${s.id}">生成</button>` : ''}
          ${canWrite ? `<button class="btn sm danger" data-del-shot="${s.id}">删除</button>` : ''}
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
            <h2>分镜</h2><span class="sub">一镜一行；提交作业去生成中心</span>
            <div class="right">
              ${canWrite ? '<button class="btn sm primary" id="shot-new">新建镜头</button>' : ''}
              <span class="sub">本集状态</span>
              <select id="ep-status" class="ctl sm" style="width:auto"
                      title="改成「定稿」后，项目中心那一页的进度会跟着走">
                ${Object.entries(EP_STATUS).map(([key, [text]]) => `
                  <option value="${key}"${key === current.status ? ' selected' : ''}>${text}</option>`).join('')}
              </select>
            </div>
          </div>
          ${assetErr ? errorLine('资产名单', assetErr) : ''}
          ${jobErr ? errorLine('作业状态', jobErr) : ''}
          ${shotErr ? errorBox('这一集的镜头', shotErr) : shots.length ? `
          <div class="table-wrap">
            <table>
            <thead><tr><th>镜号</th><th>内容 / 提示词</th><th>引用资产</th>
              <th class="r">时长</th><th>作业</th><th class="r"></th></tr></thead>
            <tbody>${shotRows}</tbody>
            </table>
            </div>` : `
            <div class="empty">
            <div class="empty-t">这一集还没有镜头</div>
            <p>一镜一行，内容与提示词可以在同一张表里编辑；资产只选引用。</p>
            ${canWrite ? '<p><button class="btn primary" id="shot-new-empty">新建第一镜</button></p>' : ''}
            </div>`}

          <div class="card-body" style="border-top:1px solid var(--line);padding:8px 12px">
            <p class="hint" style="text-align:left;margin:0">
            提示词留空时，生成用内容那一栏；引用资产只读，点名字到资产中心。
            作业状态由队列说了算，这一页只显示、不修改。
            </p>
          </div>
        </div>
      </div>

      ${shotModalHtml()}
    </section>`;
}

function shotModalHtml() {
  if (!store.can('doc:write')) return '';
  return `
    <div class="modal" id="shot-modal" hidden>
      <div class="modal-box wide">
        <div class="modal-head neutral" id="shot-modal-title">编辑镜头</div>
        <div class="modal-body">
          <div class="form-grid">
            <label for="sm-summary">内容</label>
            <textarea id="sm-summary" class="ctl" rows="3" maxlength="500"
                      placeholder="这一镜发生什么"></textarea>

            <label for="sm-prompt">提示词</label>
            <textarea id="sm-prompt" class="ctl" rows="4" maxlength="2000"
                      placeholder="可留空；生成时用内容那一栏"></textarea>

            <label for="sm-duration">时长（秒）</label>
            <input id="sm-duration" class="ctl" type="number" min="1" max="60"
                   step="1" value="5" style="width:120px">

            <label class="top">引用资产</label>
            <div class="shot-pick">
              <input id="sm-asset-q" class="ctl" placeholder="搜名字、标签或别名">
              <div id="sm-assets" class="asset-pick"></div>
              <p class="hint" style="text-align:left">只选引用；角色／场景／道具的内容到资产中心改。</p>
            </div>
          </div>
          <p class="modal-err" id="sm-error"></p>
        </div>
        <div class="modal-actions">
          <button class="btn" id="sm-cancel">取消</button>
          <button class="btn primary" id="sm-save">保存镜头</button>
        </div>
      </div>
    </div>`;
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

    /* ---------- 镜头：新建与编辑共用一张弹窗 ---------- */
    const shotModal = root.querySelector('#shot-modal');
    let shotEdit = null;      // { id: number|null, selected: Set, query: string }

    const paintPicker = () => {
      const box = root.querySelector('#sm-assets');
      if (!box || !shotEdit) return;
      const q = shotEdit.query.trim().toLowerCase();
      const match = (a) => !q || [a.name, a.kind_label, a.summary,
        ...(a.aliases || []), ...(a.tags || [])]
        .join(' ').toLowerCase().includes(q);
      const groups = [['character', '角色'], ['scene', '场景'], ['prop', '道具']];
      const blocks = [];
      groups.forEach(([kind, label]) => {
        const list = (shotCache.assets || []).filter((a) => a.kind === kind && match(a));
        if (!list.length) return;
        const chips = list.map((a) => {
          const on = shotEdit.selected.has(a.id);
          const scope = a.project_id === null ? ' · 全局' : '';
          return `<button type="button" class="chip ${on ? 'blue' : 'plain'}"
            data-pick-asset="${a.id}" title="${escapeHtml(a.kind_label + scope)}">
            ${escapeHtml(a.name)}</button>`;
        }).join('');
        // 组名独占一行：三类混在一行里读起来像一串没有层次的标签云
        blocks.push(`<div class="pick-block">
          <div class="pick-grp">${label}</div><div class="pick-row">${chips}</div></div>`);
      });
      if (!blocks.length) {
        blocks.push('<span class="t-dim">没有匹配的资产。</span>');
      }
      const picked = [...shotEdit.selected].map((id) => {
        const a = (shotCache.assets || []).find((x) => x.id === id);
        return `<button type="button" class="chip green" data-unpick="${id}"
                  title="点一下取消引用">${escapeHtml(a ? a.name : `#${id}`)} ×</button>`;
      });
      box.innerHTML = `
        ${picked.length ? `<div class="pick-picked"><span class="grp">已选 ${picked.length}</span>
          ${picked.join(' ')}</div>` : ''}
        <div class="pick-list">${blocks.join('')}</div>`;
      box.querySelectorAll('[data-pick-asset]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const id = Number(btn.dataset.pickAsset);
          if (shotEdit.selected.has(id)) shotEdit.selected.delete(id);
          else shotEdit.selected.add(id);
          paintPicker();
        });
      });
      box.querySelectorAll('[data-unpick]').forEach((btn) => {
        btn.addEventListener('click', () => {
          shotEdit.selected.delete(Number(btn.dataset.unpick));
          paintPicker();
        });
      });
    };

    const closeShot = () => {
      shotEdit = null;
      if (shotModal) shotModal.hidden = true;
    };

    const openShot = (shot) => {
      if (!shotModal) return;
      shotEdit = {
        id: shot ? shot.id : null,
        selected: new Set(shot ? shot.asset_ids : []),
        query: '',
      };
      root.querySelector('#shot-modal-title').textContent = shot
        ? `编辑第 ${shot.seq} 镜` : '新建镜头';
      root.querySelector('#sm-summary').value = shot ? shot.summary : '';
      root.querySelector('#sm-prompt').value = shot ? shot.prompt : '';
      root.querySelector('#sm-duration').value = shot ? shot.duration : 5;
      const qEl = root.querySelector('#sm-asset-q');
      qEl.value = '';
      qEl.oninput = () => { if (shotEdit) { shotEdit.query = qEl.value; paintPicker(); } };
      root.querySelector('#sm-error').textContent = '';
      paintPicker();
      shotModal.hidden = false;
      root.querySelector('#sm-summary').focus();
    };

    const saveShot = async () => {
      if (!shotEdit) return;
      const summary = root.querySelector('#sm-summary').value.trim();
      const errEl = root.querySelector('#sm-error');
      if (!summary) { errEl.textContent = '内容不能为空'; return; }
      const duration = Number(root.querySelector('#sm-duration').value);
      if (!(duration > 0 && duration <= 60)) {
        errEl.textContent = '时长要在 1—60 秒之间';
        return;
      }
      const body = {
        summary,
        prompt: root.querySelector('#sm-prompt').value.trim(),
        duration,
        asset_ids: [...shotEdit.selected],
      };
      const saveBtn = root.querySelector('#sm-save');
      saveBtn.disabled = true;
      errEl.textContent = '';
      const editing = shotEdit.id;
      const ok = editing
        ? await run('保存镜头', () => api.updateShot(editing, body))
        : await run('新建镜头', () => api.createShot(pickedEpisodeId, body));
      saveBtn.disabled = false;
      if (!ok) return;          // 失败时弹窗留着，填的字还在
      closeShot();
      refresh();
    };

    root.querySelector('#shot-new')?.addEventListener('click', () => openShot(null));
    root.querySelector('#shot-new-empty')?.addEventListener('click', () => openShot(null));
    root.querySelectorAll('[data-edit-shot]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const shot = shotCache.shots.find((s) => s.id === Number(btn.dataset.editShot));
        if (shot) openShot(shot);
      });
    });
    if (shotModal) {
      root.querySelector('#sm-cancel')?.addEventListener('click', closeShot);
      root.querySelector('#sm-save')?.addEventListener('click', saveShot);
      shotModal.addEventListener('click', (e) => { if (e.target === shotModal) closeShot(); });
      bindModalDismiss(shotModal, closeShot);
    }

    // 分镜 → 生成中心：把项目/集/镜/提示词一次交棒，不在这一页开第二个提交框。
    // 走 navstate 的一次性草稿而不是地址栏参数：刷新后不应该再弹一次。
    root.querySelectorAll('[data-gen-shot]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const shot = shotCache.shots.find((s) => s.id === Number(btn.dataset.genShot));
        setJobPrefill({
          projectId: store.currentId,
          episodeId: pickedEpisodeId,
          shotId: Number(btn.dataset.genShot),
          capability: 'video',
          prompt: shot ? ((shot.prompt || '').trim() || shot.summary) : '',
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

    /* ---------- 资产引用：只读 chip，点它去资产详情 ---------- */
    root.querySelectorAll('[data-goto-asset]').forEach((chip) => {
      chip.addEventListener('click', () => {
        const id = Number(chip.dataset.gotoAsset);
        const asset = shotCache.assets.find((a) => a.id === id);
        const sub = { character: 1, scene: 2, prop: 3 }[asset?.kind] ?? 0;
        setSub('assets', sub);
        setParam('assets', id);
        window.dispatchEvent(new CustomEvent('app:go', { detail: 'assets' }));
      });
    });
}
