/**
 * 设定中心（M4）：
 * 1) 项目级全局画风词缀库（正/负向提示词模板，供生成中心作业弹窗一键勾选注入）
 * 2) 剧本/分镜/资产设定矛盾检查与审阅面板（支持全库扫描与逐条采纳/忽略/重开）
 */
import { api } from '../api.js';
import { gotoEntry } from '../goto.js';
import { setAgentPrefill } from '../navstate.js';
import { store } from '../store.js';
import { confirmDanger, emptyBox, errorBox, escapeHtml, run } from '../util.js';

const AFFIX_CATS = {
  general: '全局画风',
  lighting: '光影氛围',
  camera: '镜头摄影',
  quality: '画质质感',
  character: '角色专属',
};

const ISSUE_CATS = {
  character_conflict: '角色设定冲突',
  asset_missing_ref: '资产缺少参考图',
  dialogue_speaker: '台词说话人校验',
  prop_mismatch: '道具/资产未绑定',
  continuity: '场次衔接与时长',
};

const SEV_LABEL = {
  error: '严重',
  warning: '警告',
  info: '提示',
};

const STATUS_LABEL = {
  open: ['待处理', 'yellow'],
  adopted: ['已采纳', 'green'],
  ignored: ['已忽略', 'gray'],
};

let statusFilter = 'open';
let editingAffix = null;
let knAlive = true;

function affixCard(a, canWrite) {
  const catText = AFFIX_CATS[a.category] || a.category;
  return `<div class="kn-affix-card" data-affix-card="${a.id}">
    <div class="kn-card-top">
      <div class="kn-card-title">
        <span>${escapeHtml(a.name)}</span>
        <span class="chip gray">${escapeHtml(catText)}</span>
        ${a.is_default ? '<span class="chip blue">默认注入</span>' : ''}
      </div>
      ${canWrite ? `<div class="actions">
        <button class="btn sm" type="button" data-affix-toggle-default="${a.id}">
          ${a.is_default ? '取消默认' : '设为默认'}
        </button>
        <button class="btn sm" type="button" data-affix-edit="${a.id}">编辑</button>
        <button class="btn sm danger" type="button" data-affix-del="${a.id}">删除</button>
      </div>` : ''}
    </div>
    ${a.positive ? `<div class="kn-prompt-line"><b>+</b> ${escapeHtml(a.positive)}</div>` : ''}
    ${a.negative ? `<div class="kn-prompt-line"><b>-</b> ${escapeHtml(a.negative)}</div>` : ''}
  </div>`;
}

function issueCard(item, canDocWrite) {
  const sevClass = ['error', 'warning', 'info'].includes(item.severity) ? item.severity : 'info';
  const sevText = SEV_LABEL[item.severity] || item.severity;
  const catText = ISSUE_CATS[item.category] || item.category;
  const [stLabel, stTone] = STATUS_LABEL[item.status] || [item.status, 'gray'];
  const srcText = item.source_type
    ? `${item.source_type}${item.source_id ? ` #${item.source_id}` : ''}`
    : '';
  return `<div class="kn-issue-card" data-issue-id="${item.id}">
    <div class="kn-card-top">
      <div class="kn-card-title">
        <span class="kn-sev ${sevClass}">${escapeHtml(sevText)}</span>
        <span class="chip plain">${escapeHtml(catText)}</span>
        <span class="chip ${stTone}">${escapeHtml(stLabel)}</span>
        <span>${escapeHtml(item.title)}</span>
      </div>
      ${srcText ? `<span class="t-dim mono">${escapeHtml(srcText)}</span>` : ''}
    </div>
    <p class="t-desc">${escapeHtml(item.description)}</p>
    ${item.suggestion ? `<p class="hint">建议：${escapeHtml(item.suggestion)}</p>` : ''}
    ${item.resolved_at ? `<p class="hint">审阅记录：${escapeHtml(item.resolved_by_name || '系统成员')} · ${escapeHtml(item.resolved_at)}</p>` : ''}
    ${canDocWrite ? `<div class="actions">
      ${item.status === 'open'
        ? `<button class="btn sm primary" type="button" data-issue-resolve="${item.id}:adopt">采纳</button>
           <button class="btn sm" type="button" data-issue-resolve="${item.id}:ignore">忽略</button>`
        : `<button class="btn sm" type="button" data-issue-resolve="${item.id}:reopen">重新打开</button>`}
    </div>` : ''}
  </div>`;
}

export default {
  async render() {
    const project = store.current;
    if (!project) {
      editingAffix = null;
      return `<section class="view on"><div class="card"><div class="empty">
        <div class="empty-t">先选一个项目</div>
        <p>全局画风词缀库与设定矛盾审阅按项目独立保存；去项目中心选一个再回来。</p>
        <p><button class="btn primary" id="kn-topick" type="button">去项目中心选</button></p>
      </div></div></section>`;
    }

    const canAssetWrite = store.can('asset:write');
    const canDocWrite = store.can('doc:write');
    let affixes = [];
    let issues = [];
    let err = null;
    try {
      [affixes, issues] = await Promise.all([
        api.knowledgeAffixes(project.id),
        api.knowledgeConsistency(project.id, statusFilter === 'all' ? '' : statusFilter),
      ]);
    } catch (e) {
      err = e;
    }
    this._affixes = affixes;
    this._issues = issues;

    const editing = editingAffix
      ? affixes.find((a) => a.id === editingAffix) || null
      : null;
    const openCount = issues.filter((i) => i.status === 'open').length;

    return `<section class="view on">
      <div class="page-head">
        <h1>设定中心</h1>
        <p>项目级画风词缀模板（生成弹窗一键注入）与剧本/分镜/资产一致性审阅</p>
        <div class="right">
          <span class="chip blue">词缀 ${affixes.length}</span>
          <span class="chip ${openCount ? 'yellow' : 'green'}">当前视图 ${issues.length} 条</span>
          ${store.can('job:submit') ? '<button class="btn sm" id="kn-agent-audit" title="前往智能体中心发起全项目设定与资产矛盾扫描">智能体扫描矛盾</button>' : ''}
        </div>
      </div>
      ${err ? `<div class="card">${errorBox('设定中心', err)}</div>` : ''}
      <div class="kn-grid">
        <section class="kn-panel">
          <div class="kn-panel-head">
            <h2>全局画风词缀库</h2>
            <span class="hint">勾选“默认注入”将在新建生成作业时自动高亮推荐</span>
          </div>
          ${canAssetWrite ? `<form class="kn-form" id="kn-affix-form">
            <div class="kn-form-row">
              <input type="text" id="kn-affix-name" maxlength="60" required
                     placeholder="词缀名称（如：新海诚透亮光影、日系赛璐璐）"
                     value="${escapeHtml(editing?.name || '')}">
              <select id="kn-affix-cat">
                ${Object.entries(AFFIX_CATS).map(([k, lbl]) => `
                  <option value="${k}" ${(editing?.category || 'general') === k ? 'selected' : ''}>${escapeHtml(lbl)}</option>
                `).join('')}
              </select>
            </div>
            <textarea id="kn-affix-pos" rows="2" maxlength="1500"
                      placeholder="正向提示词前缀/后缀（例如：cel shading, vibrant sky, crisp lineart）">${escapeHtml(editing?.positive || '')}</textarea>
            <textarea id="kn-affix-neg" rows="2" maxlength="1500"
                      placeholder="负向提示词追加（可选，例如：3d render, photorealistic, blurry）">${escapeHtml(editing?.negative || '')}</textarea>
            <div class="kn-card-top">
              <label class="hint kn-check">
                <input type="checkbox" id="kn-affix-default" ${editing?.is_default ? 'checked' : ''}>
                新作业默认推荐勾选
              </label>
              <div class="actions">
                ${editing ? '<button class="btn sm" type="button" id="kn-affix-cancel">取消编辑</button>' : ''}
                <button class="btn sm primary" type="submit" id="kn-affix-save">
                  ${editing ? '保存修改' : '新增词缀'}
                </button>
              </div>
            </div>
          </form>` : '<p class="hint">只读视图：需 <code>asset:write</code> 权限才能增改词缀模板。</p>'}
          <div class="kn-affix-list">
            ${affixes.length
              ? affixes.map((a) => affixCard(a, canAssetWrite)).join('')
              : emptyBox('还没有画风词缀', '新增一条正向/负向画风模板后，可在生成中心弹窗一键注入。')}
          </div>
        </section>

        <section class="kn-panel">
          <div class="kn-panel-head">
            <h2>设定矛盾检查与审阅</h2>
            ${canDocWrite
              ? '<button class="btn sm primary" type="button" id="kn-scan-btn">扫描设定矛盾</button>'
              : ''}
          </div>
          <div class="kn-filter-row">
            ${[
              ['open', '待处理'],
              ['adopted', '已采纳'],
              ['ignored', '已忽略'],
              ['all', '全部'],
            ].map(([key, label]) => `
              <button class="btn sm ${statusFilter === key ? 'primary' : ''}"
                      type="button" data-kn-filter="${key}">${label}</button>
            `).join('')}
          </div>
          <div class="kn-issue-list">
            ${issues.length
              ? issues.map((item) => issueCard(item, canDocWrite)).join('')
              : emptyBox(
                statusFilter === 'open' ? '当前没有待处理矛盾' : '该筛选下暂无记录',
                '点击上方「扫描设定矛盾」，检查剧本说话人、分镜资产绑定与参考图缺口。',
              )}
          </div>
        </section>
      </div>
    </section>`;
  },

  mount(root) {
    knAlive = true;
    root.querySelector('#kn-topick')?.addEventListener('click', () => {
      window.dispatchEvent(new CustomEvent('app:go', { detail: 'projects' }));
    });
    if (!store.current) return;
    const pid = store.current.id;
    const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
    const toast = (text, kind = 'ok') => window.dispatchEvent(
      new CustomEvent('app:toast', { detail: { text, kind } }),
    );

    root.querySelectorAll('[data-retry]').forEach((btn) =>
      btn.addEventListener('click', refresh));

    root.querySelector('#kn-affix-cancel')?.addEventListener('click', () => {
      editingAffix = null;
      refresh();
    });

    root.querySelector('#kn-affix-form')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      const name = root.querySelector('#kn-affix-name')?.value.trim() || '';
      const category = root.querySelector('#kn-affix-cat')?.value || 'general';
      const positive = root.querySelector('#kn-affix-pos')?.value.trim() || '';
      const negative = root.querySelector('#kn-affix-neg')?.value.trim() || '';
      const isDefault = Boolean(root.querySelector('#kn-affix-default')?.checked);
      if (!name) { root.querySelector('#kn-affix-name')?.focus(); return; }
      if (!positive && !negative) {
        toast('正向词缀与负向词缀至少填写一项', 'err');
        root.querySelector('#kn-affix-pos')?.focus();
        return;
      }
      const payload = {
        name,
        category,
        positive,
        negative,
        is_default: isDefault,
        sort_order: 0,
      };
      const editingId = editingAffix;
      const ok = await run(editingId ? '保存词缀' : '新增词缀', async () => {
        if (editingId) await api.updateKnowledgeAffix(pid, editingId, payload);
        else await api.createKnowledgeAffix(pid, payload);
      }, { btn: root.querySelector('#kn-affix-save') });
      if (!ok || !knAlive) return;
      editingAffix = null;
      toast(editingId ? '词缀已更新' : '词缀已创建');
      refresh();
    });

    root.querySelectorAll('[data-affix-edit]').forEach((btn) => {
      btn.addEventListener('click', () => {
        editingAffix = Number(btn.dataset.affixEdit);
        refresh();
      });
    });

    root.querySelectorAll('[data-affix-toggle-default]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const id = Number(btn.dataset.affixToggleDefault);
        const target = (this._affixes || []).find((a) => a.id === id);
        if (!target) return;
        const ok = await run('切换默认词缀', () => api.updateKnowledgeAffix(pid, id, {
          name: target.name,
          category: target.category,
          positive: target.positive,
          negative: target.negative,
          is_default: !target.is_default,
          sort_order: target.sort_order || 0,
        }), { btn });
        if (ok && knAlive) refresh();
      });
    });

    root.querySelectorAll('[data-affix-del]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const id = Number(btn.dataset.affixDel);
        if (!(await confirmDanger({
          title: '删除画风词缀',
          body: '删除后生成中心弹窗将不再展示该快捷词缀模板。',
          okText: '删除',
        }))) return;
        const ok = await run('删除词缀', () => api.deleteKnowledgeAffix(pid, id), { btn });
        if (ok && knAlive) {
          if (editingAffix === id) editingAffix = null;
          toast('词缀已删除');
          refresh();
        }
      });
    });

    root.querySelectorAll('[data-kn-filter]').forEach((btn) => {
      btn.addEventListener('click', () => {
        statusFilter = btn.dataset.knFilter || 'open';
        refresh();
      });
    });

    root.querySelector('#kn-scan-btn')?.addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      let res = null;
      const ok = await run('扫描设定矛盾', async () => {
        res = await api.scanKnowledgeConsistency(pid);
      }, { btn });
      if (!ok || !knAlive || !res) return;
      toast(`扫描完成：新增 ${res.new_count} 条 · 重开 ${res.reopened_count} 条 · 待处理共 ${res.open_count} 条`);
      refresh();
    });

    root.querySelectorAll('[data-issue-resolve]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const [rawId, action] = String(btn.dataset.issueResolve || '').split(':');
        const id = Number(rawId);
        if (!id || !action) return;
        const ok = await run('更新审阅状态', () => api.resolveKnowledgeIssue(pid, id, action), { btn });
        if (ok && knAlive) {
          toast(action === 'adopt' ? '已采纳并执行联动处理' : action === 'ignore' ? '已忽略该提示' : '已重新打开');
          refresh();
        }
      });
    });
    root.querySelector('#kn-agent-audit')?.addEventListener('click', () => {
      setAgentPrefill({
        goal: '对当前项目执行设定与资产一致性矛盾扫描，并将新发现的矛盾项写入设定中心审阅列表。',
        episode_id: null,
      });
      gotoEntry({ module: 'agents', sub: 2 });
    });
    return () => { knAlive = false; };
  },

  unmount() {
    knAlive = false;
  },
};
