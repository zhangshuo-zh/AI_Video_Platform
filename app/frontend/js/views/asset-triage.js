import { store } from '../store.js';
import { api } from '../api.js';
import { bindSubChips, confirmDanger, escapeHtml, run, subChipRow } from '../util.js';
import { WS_LABELS, setWsIndex, wsIndex } from './asset-workspaces.js';
import { ASSET_KIND_LABELS } from '../enums.js';

/**
 * 文本提取补资产：把抽取出来的候选分诊成资产。
 *
 * 这一页**不发起抽取**。提交入口只有创作中心那一个弹窗（W13 交付）：
 * 表单、模型与计价、云端确认、轮询、去重说明都已经在那儿写了一份，
 * 这里再写一份就是同一件事两套界面——将来改一边忘一边，
 * 最坏的结果是两个人对着两个说法花钱。所以这一页只做下游：
 * 列出项目里已有的 Run，把候选连同原文证据摊开，逐条接受／合并／拒绝。
 *
 * 三条口径跟着服务端走，页面不自己判：
 * - 证据有效性：`quote/start/end` 由服务端拿盘上正文复核，这里只显示；
 * - 顺序：角色→场景→道具→镜头（`ITEM_ORDER` 只决定分组显示，不重排服务端结果）；
 * - 一次一条：处理过的候选按钮禁用，刷新后以服务端状态为准。
 * 镜头候选只导出 Markdown——分镜的真相是 shot 表，自动灌进去等于绕过人确认。
 */

// 前三类走 enums.js 的单一来源（2026-11-10 裁决：统一为「角色」）；
// `shot` 不是资产类型（它只是这一屏的分组维度），所以留在这里。
const ITEM_ORDER = [
  ['character', ASSET_KIND_LABELS.character],
  ['scene', ASSET_KIND_LABELS.scene],
  ['prop', ASSET_KIND_LABELS.prop],
  ['shot', '镜头'],
];
const RUN_TONE = { pending: 'gray', done: 'green', failed: 'red', parse_failed: 'amber' };
const STATUS_TONE = { pending: 'plain', accepted: 'green', merged: 'blue', rejected: 'gray' };

let runId = null;            // 选中的 Run；换项目就清空
let runProjectId = null;
let filter = '';             // 类别筛选，只影响这一页
let notice = '';
let reqSeq = 0;
// 合并确认框要说"会补哪几格"，而那需要候选与资产两份数据。它们在 render() 里拿，
// 事件处理在 mount() 里——两个函数之间只有一条传话的路：模块级缓存。
// 只缓存这一屏刚读到的那一份，换项目/换 Run 就跟着覆盖，不攒历史。
let assetsCache = [];
let itemsCache = [];

function itemsOf(row) {
  // payload 里是模型给的字段；名称与说明各接口叫法一致，直接取
  return { name: row.payload?.name || '(没名字)', summary: row.payload?.summary || '',
           consistency: row.payload?.consistency_prompt || '',
           aliases: row.payload?.aliases || [], tags: row.payload?.tags || [],
           quote: row.evidence?.quote || '', start: row.evidence?.start,
           end: row.evidence?.end };
}

/**
 * 这个候选**是不是已经有同名/同别名的资产**。判据只有字面相等一条：
 * 名字相同就是同一条资产的可能性极大，而"看起来像"不是判据——语义相似度
 * 要模型才能给，本轮不拿字符串编辑距离冒充它（任务书 §10.4 要的是匹配建议，
 * 不是自动合并；最后一步必须还是人点）。
 */
function suggest(list, f, row) {
  const name = (f.name || '').trim();
  if (!name) return null;
  return list.find((a) => a.kind === row.kind
    && ((a.name || '').trim() === name
        || (a.aliases || []).some((x) => (x || '').trim() === name))) || null;
}

/**
 * 合并会改哪几格。**口径抄服务端的 `extraction_merge`：只补空，不覆盖**
 * （别名与标签是去重追加）。列出来是让人在点之前就知道点下去会发生什么，
 * 而不是点完猜——服务端那一份仍然是唯一真相，这里只是提前把同一件事说一遍。
 */
function mergePlan(item, target) {
  const f = itemsOf(item);
  const out = { fills: [], keeps: [], aliases: [] };
  if (!(target.summary || '').trim()) {
    if (f.summary) out.fills.push('简介'); else out.keeps.push('简介（候选也没给内容）');
  } else out.keeps.push('简介（已有，不会覆盖）');
  if (!(target.consistency_prompt || '').trim()) {
    if (f.consistency) out.fills.push('一致性提示词');
    else out.keeps.push('一致性提示词（候选也没给内容）');
  } else out.keeps.push('一致性提示词（已有，不会覆盖）');
  const name = (f.name || '').trim();
  if (name && name !== target.name && !(target.aliases || []).includes(name)) {
    out.aliases.push(name);
  }
  (f.aliases || []).forEach((x) => {
    const a = (x || '').trim();
    if (a && a !== target.name && !out.aliases.includes(a)
        && !(target.aliases || []).includes(a)) out.aliases.push(a);
  });
  return out;
}

function itemRow(row, assets) {
  const f = itemsOf(row);
  const decided = row.status !== 'pending';
  const canWrite = store.can('asset:write');
  const mergeable = assets.filter((a) => a.kind === row.kind);
  const hit = suggest(mergeable, f, row);
  return `
    <div class="as-item" data-item="${row.id}" id="as-item-${row.id}">
      <div class="as-item-head">
        <b>${escapeHtml(f.name)}</b>
        <span class="chip ${STATUS_TONE[row.status] || 'plain'}">${escapeHtml(row.status_label)}</span>
        ${row.confidence != null ? `<span class="t-dim">把握 ${Math.round(row.confidence * 100)}%</span>` : ''}
        ${row.decided_by_name ? `<span class="t-dim">${escapeHtml(row.decided_by_name)} · ${escapeHtml(row.decided_at || '')}</span>` : ''}
      </div>
      ${f.summary ? `<p class="t-desc">${escapeHtml(f.summary)}</p>` : ''}
      ${f.quote ? `<blockquote class="as-quote">「${escapeHtml(f.quote)}」
        <span class="t-dim">原文第 ${f.start ?? '?'}—${f.end ?? '?'} 字</span></blockquote>`
        : '<p class="t-desc">这一条没有原文证据，服务端不会让它进资产。</p>'}
      ${decided ? '' : `
        <div class="row" style="gap:6px;margin-top:6px;flex-wrap:wrap">
          ${hit ? `<span class="chip amber">像是已有的「${escapeHtml(hit.name)}」</span>
            <span class="t-dim">${(hit.name || '').trim() === (f.name || '').trim()
              ? '同名' : '别名命中'}；建议合并进去，不要再建一条</span>` : ''}
          ${row.kind !== 'shot' && canWrite ? `
            <button class="btn sm ${hit ? '' : 'primary'}" data-accept="${row.id}">接受成新资产</button>
            ${mergeable.length ? `<select class="ctl sm" data-merge-sel="${row.id}">
                <option value="">合并到已有…</option>
                ${mergeable.map((a) => `<option value="${a.id}"
                  ${hit && a.id === hit.id ? 'selected' : ''}>${escapeHtml(a.name)}</option>`).join('')}
              </select>
              <button class="btn sm" data-merge="${row.id}">合并</button>` : ''}` : ''
          }
          ${row.kind === 'shot' ? '<span class="t-desc">镜头候选只导出 Markdown，不自动写分镜表</span>' : ''}
          ${row.kind !== 'shot' && canWrite ? `<button class="btn sm danger" data-reject="${row.id}">拒绝</button>` : ''}
        </div>`}
    </div>`;
}

export default {
  async render() {
    const project = store.current;
    if (!project) {
      return `<section class="view on"><div class="card"><div class="empty">
        <div class="empty-t">先选一个项目</div><p>抽取候选按项目组织。</p></div></div></section>`;
    }
    if (runProjectId !== project.id) { runId = null; runProjectId = project.id; }
    const wanted = ++reqSeq;
    let runs = [];
    let assets = [];
    let err = '';
    try {
      [runs, assets] = await Promise.all([api.extractRuns(project.id), api.assets(project.id)]);
    } catch (e) {
      err = e.message || String(e);
    }
    if (wanted !== reqSeq) return '<section class="view on"></section>';
    if (err) {
      return `<section class="view on">
        <div class="page-head"><h1>文本提取补资产</h1>
          <div class="right">${subChipRow(WS_LABELS, wsIndex())}</div></div>
        <div class="card"><div class="empty err" style="padding:26px 20px">
          <div class="empty-t">读不到抽取记录</div><p>${escapeHtml(err)}</p>
          <p><button class="btn" data-retry>重试</button></p></div></div></section>`;
    }
    if (!runId && runs.length) runId = runs[0].id;

    let items = [];
    let runRow = runs.find((r) => r.id === runId) || null;
    let itemsErr = '';
    if (runId) {
      try {
        items = await api.extractItems(runId);
        if (wanted !== reqSeq) return '<section class="view on"></section>';
      } catch (e) {
        itemsErr = e.message || String(e);
      }
    }
    assetsCache = assets;
    itemsCache = items;
    const shown = filter ? items.filter((r) => r.kind === filter) : items;
    const groups = ITEM_ORDER.map(([kind, label]) => ({
      kind, label, rows: shown.filter((r) => r.kind === kind),
    })).filter((g) => g.rows.length);

    return `
    <section class="view on">
      <div class="page-head">
        <h1>文本提取补资产</h1>
        <p>${escapeHtml(project.name)} · 候选必须带逐字原文证据；接受与合并都只在这一页落资产</p>
        <div class="right">${subChipRow(WS_LABELS, wsIndex())}
          <button class="btn" data-goto-extract>去创作中心发起抽取</button></div>
      </div>
      ${notice ? `<div class="card"><div class="bar bad">${escapeHtml(notice)}</div></div>` : ''}

      <div class="as-split" style="display:grid;grid-template-columns:280px minmax(0,1fr);gap:14px;align-items:start">
        <div class="card tree">
          <div class="card-head"><h2>抽取记录</h2><span class="sub">${runs.length} 次</span></div>
          <div class="card-body">
            ${runs.length ? runs.map((r) => `
              <div class="t ${r.id === runId ? 'on' : ''}" data-pick-run="${r.id}">
                ${escapeHtml(r.doc_title || '（文档已删）')}
                <span class="chip ${RUN_TONE[r.status] || 'gray'}">${escapeHtml(r.status_label)}</span>
                <div class="t-dim">${escapeHtml(r.model_code)} · ${r.created_at}</div>
                ${r.deduped ? '<div class="t-dim">24 小时内同稿同模型，复用了旧结果</div>' : ''}
                ${r.stale ? '<div class="t-dim">正文改过，这份候选对应的是旧版</div>' : ''}
              </div>`).join('')
              : '<p class="t-desc">还没有抽取记录。到创作中心的文档页点「抽取候选」发起一次。</p>'}
          </div>
        </div>

        <div class="card">
          <div class="card-head"><h2>候选</h2>
            <span class="sub">${runRow ? `第 ${runRow.id} 次抽取 · ${shown.length} 条` : '没选中抽取记录'}</span>
            <div class="right">
              <button type="button" class="chip ${filter === '' ? 'blue' : 'plain'}" data-filter="">全部</button>
              ${ITEM_ORDER.map(([k, label]) => `<button type="button"
                class="chip ${filter === k ? 'blue' : 'plain'}" data-filter="${k}">${label}</button>`).join('')}
            </div>
          </div>
          <div class="card-body" id="as-items">
            ${itemsErr ? `<div class="bar bad">候选读不到：${escapeHtml(itemsErr)}</div>` : ''}
            ${!itemsErr && groups.length ? groups.map((g) => `
              <div class="grp">${g.label} · ${g.rows.length} 条</div>
              ${g.rows.map((row) => itemRow(row, assets)).join('')}`).join('') : ''}
            ${!itemsErr && !groups.length ? `<div class="empty">
              <div class="empty-t">${runId ? (filter ? '这一类没有候选' : '这次抽取没有候选') : '还没有抽取记录'}</div>
              <p>${runId ? '换一类看看，或到创作中心再抽一次别的文档。' : '先发起一次抽取，回到这一页处理候选。'}</p>
            </div>` : ''}
            ${runId && items.some((r) => r.kind === 'shot') ? `
              <p style="margin-top:10px"><a class="btn" id="as-shots-md"
                 href="/api/extract/runs/${runId}/shots.md" download>导出镜头候选 Markdown</a>
                 <span class="t-dim">镜头不进分镜表，导出去由人抄</span></p>` : ''}
          </div>
        </div>
      </div>
    </section>`;
  },

  mount(root) {
    const project = store.current;
    if (!project) return;
    const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
    const clear = () => { notice = ''; };

    root.querySelector('[data-retry]')?.addEventListener('click', refresh);
    // 与资产创作同一个理由：窄屏侧栏二级导航收起，这一排 chip 是唯一出口
    bindSubChips(root, (i) => { setWsIndex(i); refresh(); });
    root.querySelector('[data-goto-extract]')?.addEventListener('click', () => {
      window.dispatchEvent(new CustomEvent('app:go', { detail: 'writing' }));
    });
    root.querySelectorAll('[data-pick-run]').forEach((el) => {
      el.addEventListener('click', () => { clear(); runId = Number(el.dataset.pickRun); refresh(); });
    });
    root.querySelectorAll('[data-filter]').forEach((el) => {
      el.addEventListener('click', () => { filter = el.dataset.filter; refresh(); });
    });

    root.querySelectorAll('[data-accept]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const host = btn.closest('[data-item]');
        const name = host?.querySelector('b')?.textContent || '';
        const ok = await run('接受候选', () => api.extractAccept(Number(btn.dataset.accept), {}), { btn: btn });
        if (ok) {
          notice = `「${name}」已建成资产。要补定妆图或一致性提示词，去资产库点开它。`;
          refresh();
        }
      });
    });
    root.querySelectorAll('[data-merge]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const sel = root.querySelector(`[data-merge-sel="${btn.dataset.merge}"]`);
        if (!sel || !sel.value) {
          notice = '先选一条要合并进去的已有资产。';
          refresh();
          return;
        }
        const item = itemsCache.find((x) => x.id === Number(btn.dataset.merge));
        const target = assetsCache.find((a) => a.id === Number(sel.value));
        if (item && target) {
          const plan = mergePlan(item, target);
          const lines = [
            plan.fills.length ? `会补：${escapeHtml(plan.fills.join('、'))}` : '没有空格子可补',
            plan.aliases.length ? `别名追加：${escapeHtml(plan.aliases.join('、'))}` : '别名不追加',
            plan.keeps.length ? `不动：${escapeHtml(plan.keeps.join('、'))}` : '',
          ].filter(Boolean).map((x) => `<div>${x}</div>`).join('');
          const go = await confirmDanger({
            title: `合并到「${target.name}」`, danger: false, okText: '确认合并',
            body: `<p>合并只补空着的格子，已有内容一个字都不覆盖；`
                + `候选里的名字会进它的别名。这条候选处理完就变成「已合并」。</p>${lines}`,
          });
          if (!go) return;
        }
        const ok = await run('合并候选', () => api.extractMerge(Number(btn.dataset.merge), Number(sel.value)), { btn: btn });
        if (ok) { notice = '已合并：只补空字段，别名与标签去重，不覆盖已填内容。'; refresh(); }
      });
    });
    root.querySelectorAll('[data-reject]').forEach((btn) => {
      btn.addEventListener('click', async () => {
      const ok = await run('拒绝候选', () => api.extractReject(Number(btn.dataset.reject), ''), { btn: btn });
        if (ok) refresh();
      });
    });
  },
};
