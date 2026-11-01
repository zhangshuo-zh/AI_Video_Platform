import { store } from '../store.js';
import { api } from '../api.js';
import { bindSubChips, confirmDanger, escapeHtml, errorBox, errorLine, run, subChipRow } from '../util.js';
import { getParam, getSub, setParam, setSub } from '../navstate.js';

/**
 * 创作中心 = 集（Episode）+ 三级文档（小说／剧本／分镜）。
 * 按设计稿 §3.2，Episode 和 Shot 的东家在这里：建集、删集、加镜、删镜都只在这一页做，
 * 项目中心只显示计数。小说/剧本的正文本来就在文件系统里，平台先只管"一镜一行"这份可执行的数据。
 * 选中的集记在模块级变量里，切页回来还在。
 */
let pickedEpisodeId = null;

// 集状态。这一列 0004 建表时就有，但一直没有写入口；项目中心的
// "已定稿 N/M 集"就是它的聚合，所以入口放在这里——集的东家是创作中心
const EP_STATUS = { draft: ['草稿', 'gray'], active: ['在产', 'blue'], done: ['定稿', 'green'] };

// 侧栏二级导航里分镜排第三（小说、剧本在前），渲染后高亮它

export default {
  subIndex: 2,

  async render() {
    // 小说 / 剧本两页的正文按设计留在文件里，这里只声明现状
    const tab = getSub('writing') ?? 2;
    // 页内也摆一排切换器：窄屏侧栏的二级标签会收起，只靠它等于把功能藏了
    const subRow = subChipRow(['小说', '剧本', '分镜'], tab);
    if (tab !== 2) {
      const name = tab === 0 ? '小说' : '剧本';
      return `<section class="view on">
        <div class="page-head"><h1>创作中心</h1><p>${name}</p>
          <div class="right">${subRow}</div></div>
        <div class="card"><div class="empty">
          <div class="empty-t">${name}正文仍在文件里</div>
          <p>按设计稿的口径，小说与剧本正文留在文件系统并走 git，平台只做状态与版本索引。
             文档索引这一版还没建，先在文件里写；分镜那一页已经可用。</p>
        </div></div>
      </section>`;
    }

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
          <button class="btn sm danger" style="margin-left:6px" data-del-ep="${e.id}"
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
        <td class="r"><button class="btn sm danger" data-del-shot="${s.id}">删除</button></td>
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
  },

  mount(root) {
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
  },
};
