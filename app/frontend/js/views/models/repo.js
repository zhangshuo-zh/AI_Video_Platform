/**
 * 模型中心 · 0 模型仓库（清单 × 本机登记 的联表）。
 *
 * 本文件由 models.js（2277 行）按子页拆出。共享底座见 ./shared.js：
 * 缓存、常量、跨子页的纯函数都在那边，这里只管本子页的渲染与交互。
 */
import { api } from '../../api.js';
import { store } from '../../store.js';
import {
  agoText, bindModalDismiss, bindSubChips, confirmDanger, escapeHtml, run, subChipRow,
} from '../../util.js';
import { getSub, setSub } from '../../navstate.js';
import { HEALTH, VAL_BADGE, VAL_KIND_LABEL, alertLabel } from '../../validation.js';
import { REG_SOURCES, unitLabel } from '../../enums.js';
import {
  SUBS, cache, stateChip, healthChip, manifestAxis, adapterOf, manifestProviders,
  sourceLabels, sourceLabel, overviewCard, fillPicks, pickedModel,
  pendingFilter, setPendingFilter, setViewFilter,
} from './shared.js';

/* ================= 工作区 0 · 模型仓库 ================= */
export function tagChips(tags) {
  if (!tags || !tags.length) return '<span class="hint">没有能力标签</span>';
  return tags.map((t) => {
    // 人工声明未核验的必须一眼看得出来：清单派生是事实，人工声明是主张
    const cls = t.source === 'manifest' ? 'plain' : (t.verified ? 'blue' : 'amber');
    const tip = t.source === 'manifest'
      ? '来自适配器清单已登记的参数表，可复算'
      : (t.verified ? '人工声明，且这一档有过真实调用或产物验收' : '人工声明，尚未核验');
    return `<span class="chip ${cls}" title="${escapeHtml(tip)}">${escapeHtml(t.tag)}</span>`;
  }).join('');
}

export function priceChips(prices) {
  if (!prices || !prices.length) return '<span class="hint">无按量计价</span>';
  return prices.map((p) => {
    const yuan = (p.amount_micros / 1e6).toFixed(p.amount_micros % 1e6 ? 2 : 1);
    // 单位名从 enums.js 那一份取：这里原来自己抄了三档（second／million_token／image），
    // 抄出来的「百万 token」和后端「百万 Token」差一个大小写，而没抄的四档
    // （token／request／minute／character）会直接把英文 code 念在价格牌上。
    const unit = unitLabel(p.unit);
    const when = Object.entries(p.when || {}).map(([k, v]) => `${k}=${v}`).join(' ');
    return `<span class="chip plain" title="${escapeHtml(p.source)}">
      ${yuan} ${escapeHtml(p.currency)}/${escapeHtml(unit)}${when ? ' · ' + escapeHtml(when) : ''}</span>`;
  }).join('');
}

export function statusChip(item, labels) {
  // 三个「缺东西」的档：灰＝本机没登记（点一下就解决）、红＝清单里没有（改代码）、
  // 琥珀＝清单只有名字（等规格补齐）。颜色不同的理由见后端 STATUS_LABEL 上面那段注解。
  const tone = { available: 'green', unverified: 'amber', manifest_only: 'gray',
                 unchecked: 'amber', disabled: 'gray', not_installed: 'gray',
                 dependency_missing: 'red', connection_failed: 'red',
                 registration_only: 'red', name_only: 'amber' }[item.status] || 'gray';
  const label = labels[item.status] || item.status;
  return `<span class="chip ${tone}" title="${escapeHtml(item.status_detail || '')}">${escapeHtml(label)}</span>`;
}

/**
 * 状态＋健康两枚徽章。文字撞车时只留一枚——
 * "未检查｜未检查"这种并排不是信息，是噪声；两枚各说各的时才并排。
 * 撞车是常态而非例外：状态为 unchecked 的唯一来源就是健康探测返回 unknown，
 * 两边都译成"未检查"。
 */
export function statusHealthChips(item) {
  const s = statusChip(item, cache.catalog.status_labels);
  // 待现查的那一档，状态与健康说的是两件事（"未检查"＋"待现查"），
  // 不能按文字撞车去重——按老规则它俩不撞车，会并排出现两个黄徽章，
  // 看着像两条不同的毛病。这里只留状态那一枚，理由写在下面。
  if (item.health?.probe_deferred) return s;
  const h = healthChip(item.health);
  const sLabel = (cache.catalog.status_labels[item.status] || item.status);
  const hLabel = (HEALTH[item.health?.state] || ['gray', '未知'])[1];
  return sLabel === hLabel ? s : `${s} ${h}`;
}

export function pageRepo() {
  const items = cache.catalog.items;
  // 筛选条件留在模块级：切走再切回来不该把人筛的东西重置（和 sub 同一个道理）
  const rows = items.map((it) => {
    const name = it.display_name || it.model_label || it.model_code || '—';
    return `<tr class="repo-row" data-key="${escapeHtml(it.provider)}/${escapeHtml(it.model_code)}"
        data-cap="${escapeHtml(it.capability)}" data-source="${escapeHtml(it.source)}"
        data-name="${escapeHtml((name + ' ' + it.model_code + ' ' + it.provider).toLowerCase())}"
        data-rid="${it.registration_id || ''}"
        ${it.registered && it.status !== 'available' ? 'data-attention="1"' : ''}
        ${it.registered ? 'data-registered="1"' : ''}
        ${it.registered && it.status === 'available' ? 'data-avail="1"' : ''}>
      <td><b>${escapeHtml(name)}</b>
        <div class="hint mono">${escapeHtml(it.provider)}/${escapeHtml(it.model_code)}</div></td>
      <td>${escapeHtml(cache.catalog.capabilities[it.capability] || it.capability)}
        <div class="param-chips">${tagChips(it.tags)}</div></td>
      <td><span class="chip ${it.backend === 'local' ? 'plain' : 'blue'}">${escapeHtml(sourceLabel(it.source))}</span></td>
      <td>${escapeHtml(it.engine_label || it.engine_code || '—')}</td>
      <td>${statusHealthChips(it)}</td>
      <td>${it.registration_id
        // 一行只留一个入口。检查／编辑／撤登记收进详情面板底部：
        // 那里有完整上下文（状态、健康、验证出处都在），做决定比在一排小按钮里点靠谱，
        // 而撤登记这种破坏性操作多走一层本来就是对的。
        ? `<button class="btn xs" data-detail="${escapeHtml(it.provider)}/${escapeHtml(it.model_code)}">详情</button>`
        : `<button class="btn xs" data-register="${escapeHtml(it.provider)}/${escapeHtml(it.model_code)}">登记</button>`}</td>
    </tr>`;
  }).join('');

  // 「全部类型」与「全部来源」两个下拉的选项都来自接口（capabilities／reg_sources），
  // 不在这里硬写。原来来源那三个 option 有两个毛病：写的是英文取值
  // （local／cloud／remote，而同一张表「运行位置」那一列念的是中文），
  // 而且后端加第四档时界面永远不会知道。登记弹窗里那份（见 #mf-be）早就是生成的，
  // 两处口径现在一致。
  return `
  <div class="card">
    <div class="card-head">
      <h2>模型仓库</h2>
      <span class="sub">清单 × 本机登记。规格住代码里，这里只登记实例</span>
      <span class="right"><span class="hint" id="repo-count"></span></span>
    </div>
    <div class="card-body" style="padding-bottom:0">
      <div class="filters" style="display:flex;gap:8px;flex-wrap:wrap">
        <input class="ctl" id="repo-search" placeholder="搜索显示名、模型标识或 provider…" style="min-width:230px">
        <!-- 两个筛选下拉要显式给宽度：浏览器原生 <select> 会按**最短选项**算宽
             （实测这四个字只给 92px），夜间主题换成自绘按钮后照抄了这个宽度，
             「全部类型／全部来源」就折成两行。给足一行文字 + 箭头的最小宽度，
             并禁止换行，白天与夜间就都不折了。 -->
        <select class="ctl repo-filter" id="repo-cap">
          <option value="">全部类型</option>
          ${Object.entries(cache.catalog.capabilities)
            .map(([k, v]) => `<option value="${k}">${escapeHtml(v)}</option>`).join('')}
        </select>
        <select class="ctl repo-filter" id="repo-source">
          <option value="">全部来源</option>
          ${Object.entries(sourceLabels())
            .map(([k, v]) => `<option value="${k}">${escapeHtml(v)}</option>`).join('')}
        </select>
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>模型</th><th>类型 / 能力</th><th>运行位置</th><th>所属引擎</th>
          <th>状态 / 健康</th><th>操作</th></tr></thead>
        <tbody id="repo-rows">${rows}</tbody>
      </table>
    </div>
    <div class="card-body" style="border-top:1px solid var(--line)">
      <p class="note"><b>三种"缺东西"缺的不是同一侧，处置也相反</b>：
        「本机未登记」＝清单里有、这台机器没登记（点一下「登记」就解决）；
        「清单无此档」＝本机登记了、清单里没有这一档（点多少次都没用，要去 <code>adapters.py</code> 补规格或撤掉这条登记）；
        「清单只有名字」＝清单只知道这一家的名字、没枚举模型档（等规格补齐，先手填模型标识）。
        只列已登记的档，"装了不等于配了"就看不见；所以第二种照样列出来，但用红徽章跟第一种分开。
        登记不会去下载权重，也不会启动任何推理。</p>
      <p class="note"><b>「撤登记」不删模型文件</b>：它只删这一行登记。
        权重仍在盘上，历史作业与验证记录一律不动。</p>
    </div>
  </div>`;
}

/* ================= 工作区 0 交互 ================= */
export function findItem(key) {
  return cache.catalog.items.find((i) => `${i.provider}/${i.model_code}` === key);
}

export function mountRepo(root) {
  const rows = [...root.querySelectorAll('#repo-rows tr')];
  const count = root.querySelector('#repo-count');
  /* 状态筛选是**临时视图**：概览那枚「N 个要看一眼」点进来时置位，
     人再动搜索或换筛选就清掉——它不是长期挂着的第四个下拉，
     挂上去就要回答"为什么刷新后还在"。 */
  // 切回这一屏时若有待筛意图（在别的子页点的数），消费一次就清掉——
  // 它是一次性的"跳转带过去的筛选"，不该在刷新后还阴魂不散地筛着。
  let statusFilter = pendingFilter;
  setPendingFilter('');
  // 三种临时视图，各自判据与后端 stats() 的取法逐字对应：
  //   registered＝已登记全部　available＝已登记∧可用　attention＝已登记∧没到可用
  const matchFilter = (tr) => statusFilter === 'attention'
    ? tr.dataset.attention === '1'
    : statusFilter === 'available' ? tr.dataset.avail === '1'
      : tr.dataset.registered === '1';
  const apply = () => {
    const q = (root.querySelector('#repo-search').value || '').trim().toLowerCase();
    const cap = root.querySelector('#repo-cap').value;
    const src = root.querySelector('#repo-source').value;
    let n = 0;
    rows.forEach((tr) => {
      const ok = (!q || tr.dataset.name.includes(q))
        && (!cap || tr.dataset.cap === cap) && (!src || tr.dataset.source === src)
        && (!statusFilter || matchFilter(tr));
      tr.style.display = ok ? '' : 'none';
      if (ok) n += 1;
    });
    count.textContent = `当前筛选 ${n} / ${rows.length} 档`;
  };
  ['repo-search', 'repo-cap', 'repo-source'].forEach((id) => {
    root.querySelector('#' + id).addEventListener(
      id === 'repo-search' ? 'input' : 'change', () => {
        // 人一旦自己动筛选，概览点进来的那个临时视图就交还给搜索框。
        statusFilter = '';
        apply();
      });
  });
  // 「已登记／可用／要看一眼」：点下去就地筛，筛出的行数等于那个数
  // （stats 吃的就是这批行，两处同一把尺子）。
  const jumpFilter = (kind) => () => {
    statusFilter = kind;
    root.querySelector('#repo-search').value = '';
    root.querySelector('#repo-cap').value = '';
    root.querySelector('#repo-source').value = '';
    apply();
    root.querySelector('#repo-rows')?.scrollIntoView({ block: 'nearest' });
  };
  // 本屏的三个直接在原地接（kind 在下方视图统一派发，见 mountStrip）
  setViewFilter(jumpFilter);
  apply();

  root.querySelectorAll('[data-detail]').forEach((b) => b.addEventListener('click', () => {
    const it = findItem(b.dataset.detail);
    if (!it) return;
    openDetail(root, it);
  }));
  root.querySelectorAll('[data-register]').forEach((b) => b.addEventListener('click', () => {
    const it = findItem(b.dataset.register);
    if (it) openForm(null, it);
  }));
  // 检查／编辑／撤登记三个动作搬进了详情面板底部（见 openDetail），
  // 行内只留「详情」与「登记」两个入口。
}

export function openDetail(root, it) {
  root.querySelector('#mc-detail-title').textContent =
    `${it.display_name || it.model_label || it.model_code} · ${it.provider}/${it.model_code}`;
  const params = (it.params || []).map((p) => {
    const bits = [];
    if (p.default !== null && p.default !== undefined) bits.push(`默认 ${p.default}`);
    if (p.min != null && p.max != null) bits.push(`${p.min}—${p.max}`);
    if (p.multiple_of) bits.push(`${p.multiple_of} 的倍数`);
    return `<div class="kv"><dt><code>${escapeHtml(p.name)}</code></dt><dd>${escapeHtml(bits.join(' · '))}
      ${p.note ? `<div class="t-desc">${escapeHtml(p.note)}</div>` : ''}</dd></div>`;
  }).join('');
  root.querySelector('#mc-detail-body').innerHTML = `
    <div class="kv-grid">
      <div class="kv"><dt>状态</dt><dd>${statusChip(it, cache.catalog.status_labels)}
        <span class="t-desc">${escapeHtml(it.status_detail || '')}</span></dd></div>
      <div class="kv"><dt>健康</dt><dd>${healthChip(it.health)}
        <span class="t-desc">${escapeHtml(it.health?.detail || '')}</span></dd></div>
      <div class="kv"><dt>运行位置</dt><dd>${escapeHtml(sourceLabel(it.source))} ·
        ${escapeHtml(it.engine_label || it.engine_code || '—')}</dd></div>
      <div class="kv"><dt>版本</dt><dd>${escapeHtml(it.version || '未填')}</dd></div>
      <div class="kv"><dt>权重路径</dt><dd class="mono">${escapeHtml(it.artifact_path || '未填')}</dd></div>
      <div class="kv"><dt>远程 ID</dt><dd class="mono">${escapeHtml(it.remote_model_id || '未填')}</dd></div>
      <div class="kv"><dt>能力标签</dt><dd>${tagChips(it.tags)}</dd></div>
      <div class="kv"><dt>计价</dt><dd>${priceChips(it.prices)}</dd></div>
      <div class="kv"><dt>目录变量</dt><dd class="mono">${escapeHtml(it.home_env || '不适用')}</dd></div>
      <div class="kv"><dt>清单说明</dt><dd class="t-desc">${escapeHtml(it.manifest_note || '无')}</dd></div>
      <div class="kv"><dt>验证出处</dt><dd>${(it.validation_kinds || []).map((k) =>
        `<span class="chip green">${escapeHtml(VAL_KIND_LABEL[k] || k)}</span>`).join(' ')
        || '<span class="hint">一条验证记录都没有</span>'}</dd></div>
      <div class="kv"><dt>备注</dt><dd class="t-desc">${escapeHtml(it.note || '无')}</dd></div>
    </div>
    ${params ? `<h3 class="drawer-h">允许参数（来自清单）</h3><div class="kv-grid">${params}</div>` : ''}`;

  /* 动作跟着详情一起进来：这里看得见状态、健康、验证出处与路径，
     做"撤登记"这种决定比在一排小按钮里点靠谱。
     每次开面板都重绑——面板是同一个 DOM，但对应的行每次都可能不同。 */
  const acts = root.querySelector('#mc-detail-acts');
  if (!it.registration_id) {
    acts.innerHTML = `<button class="btn" data-act-register="${escapeHtml(`${it.provider}/${it.model_code}`)}">登记这一档</button>`;
  } else {
    acts.innerHTML = `
      <button class="btn" data-act-check="${it.registration_id}">环境检查</button>
      <button class="btn" data-act-edit="${it.registration_id}">编辑登记</button>
      <button class="btn danger" data-act-remove="${it.registration_id}">撤登记…</button>`;
  }
  const close = () => { root.querySelector('#mc-detail').hidden = true; };
  acts.querySelector('[data-act-check]')?.addEventListener('click', async (e) => {
    e.target.disabled = true;
    const ok = await run('检查', () => api.checkModel(Number(e.target.dataset.actCheck)));
    e.target.disabled = false;
    if (ok) { close(); window.dispatchEvent(new CustomEvent('app:refresh')); }
  });
  acts.querySelector('[data-act-edit]')?.addEventListener('click', () => {
    close(); openForm(it.registration_id, it);
  });
  acts.querySelector('[data-act-register]')?.addEventListener('click', () => {
    close(); openForm(null, it);
  });
  acts.querySelector('[data-act-remove]')?.addEventListener('click', async () => {
    const yes = await confirmDanger({
      title: '撤登记',
      body: '这一步只删除这条登记：权重文件仍在盘上，历史作业与验证记录一律不动。'
            + '要改默认轨请去「引擎管理」的能力轨表。',
      okText: '撤登记',
    });
    if (!yes) return;
    close();
    await run('撤登记', () => api.removeModel(it.registration_id));
    window.dispatchEvent(new CustomEvent('app:refresh'));
  });
  root.querySelector('#mc-detail').hidden = false;
}

/** 登记弹窗：regId 给了就是编辑，不给就是新增；preset 用来从清单行直接带出键。 */
export function openForm(regId, preset) {
  const modal = document.querySelector('#mc-form');
  if (!modal) return;
  const err = modal.querySelector('#mf-error');
  err.textContent = '';
  modal.querySelector('#mc-form-title').textContent = regId ? '编辑登记' : '登记模型';
  const capSel = modal.querySelector('#mf-cap');
  capSel.innerHTML = Object.entries(cache.catalog.capabilities)
    .map(([k, v]) => `<option value="${k}">${escapeHtml(v)}</option>`).join('');
  // 运行位置的取值也来自接口：登记层比选型层多一档 remote，
  // 原来这里硬写三个 option，后端加第四档时界面永远不会知道
  modal.querySelector('#mf-be').innerHTML = Object.entries(sourceLabels())
    .map(([k, v]) => `<option value="${k}">${escapeHtml(v)}</option>`).join('');
  const set = (id, value) => { modal.querySelector(id).value = value ?? ''; };
  const beSel = modal.querySelector('#mf-be');
  const provSel = modal.querySelector('#mf-prov');
  const modelFree = modal.querySelector('#mf-model-free');
  capSel.value = preset?.capability || 'image';
  beSel.value = preset?.backend || 'local';
  // 这两格的选项跟着能力／运行位置走，所以先打「键已锁」标记再填：
  // 编辑态键是登记行的主键，重填时也得保持灰的。
  modal.dataset.keysLocked = regId ? '1' : '';
  fillPicks(modal, capSel.value, beSel.value, preset?.provider,
            preset?.model_code, !!regId);
  // onchange 用赋值而不是 addEventListener：弹窗 DOM 是常驻的那一份，
  // 每次 openForm 再 add 一遍就会在人改一次能力时跑 N 遍重填。
  const rePick = () => fillPicks(modal, capSel.value, beSel.value,
                                 provSel.value, pickedModel(modal), false);
  capSel.onchange = rePick;
  beSel.onchange = rePick;
  provSel.onchange = rePick;
  set('#mf-name', preset?.display_name); set('#mf-ver', preset?.version);
  set('#mf-path', preset?.artifact_path); set('#mf-remote', preset?.remote_model_id);
  set('#mf-caps', (preset?.tags || []).filter((t) => t.source === 'manual')
    .map((t) => t.tag).join(', '));
  set('#mf-note', preset?.note);
  modal.querySelector('#mf-enabled').checked = preset ? !!preset.enabled : true;
  // 编辑时能力／运行位置／provider／模型标识是登记行的键，改键等于换一档——
  // 那应该是撤了再登记。只禁用而不在提交里删字段是不够的：用户会以为改了，
  // 保存后却什么都没变。后端 PATCH 也不收 backend/capability，口径两边一致。
  provSel.disabled = !!regId;
  modal.querySelector('#mf-model').disabled = !!regId;
  modelFree.disabled = !!regId;
  capSel.disabled = !!regId;
  beSel.disabled = !!regId;
  // 光把控件 disabled 不够：`.ctl` 原来没有禁用态样式，点不动会被当成页面坏了。
  // 这里把「为什么点不动、想改该怎么办」写在表单里，并在四个键上挂同一句 title。
  const lockNote = modal.querySelector('#mf-lock-note');
  if (lockNote) {
    lockNote.hidden = !regId;
    lockNote.textContent = regId
      ? '编辑只改显示名、版本、权重路径、能力声明与备注；'
        + '要换能力／运行位置／provider／模型标识，请先撤登记再重新登记（改键等于换一档）。'
      : '';
  }
  if (regId) {
    const why = '编辑登记时这一格锁住了：改键等于换一档，请先撤登记再重新登记。';
    for (const el of [capSel, beSel, provSel, modal.querySelector('#mf-model')]) {
      if (el) el.title = why;
    }
  }
  modal.hidden = false;

  const submit = modal.querySelector('#mf-submit');
  submit.onclick = async () => {
    err.textContent = '';
    const provider = provSel.value.trim();
    const modelCode = pickedModel(modal);
    if (!provider || !modelCode) {
      // 两种空法说的话不一样：下拉压根没得选＝清单里没有这一档，
      // 只漏了模型 ID＝那一档的清单只登记了名字、要人手填。
      err.textContent = provider
        ? '模型标识不能空——provider 与模型标识这两个键决定登记的是哪一档。'
        : `清单里没有「${cache.catalog.capabilities[capSel.value] || capSel.value}`
          + ` × ${sourceLabels()[beSel.value] || beSel.value}」这一档，界面登记不了。`
          + '要加 provider 得先把适配器写进 adapters.py（规格住代码）。';
      return;
    }
    const caps = modal.querySelector('#mf-caps').value.split(/[,，]/)
      .map((s) => s.trim()).filter(Boolean);
    const body = {
      // source 是"运行位置"的人话口径，必须和这次选的后端一起提交：
      // 后端字段默认空串会跟 backend 走，但显式带上更不容易再漂。
      capability: capSel.value, backend: modal.querySelector('#mf-be').value,
      source: modal.querySelector('#mf-be').value,
      provider, model_code: modelCode,
      display_name: modal.querySelector('#mf-name').value.trim(),
      version: modal.querySelector('#mf-ver').value.trim(),
      artifact_path: modal.querySelector('#mf-path').value.trim(),
      remote_model_id: modal.querySelector('#mf-remote').value.trim(),
      capabilities: caps, note: modal.querySelector('#mf-note').value.trim(),
      enabled: modal.querySelector('#mf-enabled').checked,
    };
    submit.disabled = true;
    try {
      if (regId) {
        const patch = { ...body };
        delete patch.provider; delete patch.model_code;
        delete patch.capability; delete patch.backend;
        // source 跟着这四个键一起固定：界面没有独立的"人话运行位置"输入，
        // 放它进 patch 会把接口侧手工标的 remote 静默改回本地。
        delete patch.source;
        await api.patchModel(regId, patch);
      } else {
        await api.createModel(body);
      }
    } catch (e) {
      // 表单里的错就写在表单里。全局 `run()` 用 alert，headless 下会被自动
      // 关掉、人只看到"点了没反应"；而 422 的文案本来就是写给填表人看的
      // （例如「这一档已经登记过了，要改就编辑那一条」），原样贴进来最有用。
      // 401 不贴：那是"会话过期"，由统一闸门去弹登录，不该混进表单错误。
      if (!(e && e.status === 401)) {
        err.textContent = (e && e.message) ? e.message : String(e);
      }
      submit.disabled = false;
      return;
    }
    submit.disabled = false;
    modal.hidden = true;
    window.dispatchEvent(new CustomEvent('app:refresh'));
  };
}
