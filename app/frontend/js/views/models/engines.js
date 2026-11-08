/**
 * 模型中心 · 1 引擎管理（能力轨十行）＋ 1b 引擎实例（0040）。
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
  sourceLabels, sourceLabel, overviewCard, alertNotify, VAL_STATE, HISTORY_PAGE,
  resolveSeq, bumpResolveSeq, showWorkerHistory, toggleWorkerHistory,
} from './shared.js';

/* ================= 工作区 1 · 引擎管理 ================= */
/* 下面这一段是原 system-config.js 的「模型与密钥」整页搬过来：
   十行能力轨、允许参数、密钥变量名、健康、验证列、环境检查、验证记录弹窗、
   整批保存——一行功能都没减。搬动的理由只有一个：模型这件事的入口现在只有一个。 */

export function paramChips(model) {
  if (!model || !model.params || !model.params.length) return '';
  return model.params.map((p) => {
    const bits = [];
    if (p.default !== null && p.default !== undefined) bits.push(`=${p.default}`);
    if (p.min !== undefined && p.max !== undefined) bits.push(`${p.min}—${p.max}`);
    if (p.multiple_of) bits.push(`${p.multiple_of} 的倍数`);
    const title = [p.type, ...bits, p.note || ''].filter(Boolean).join(' · ');
    return `<span class="chip plain" title="${escapeHtml(title)}">${escapeHtml(p.name)}</span>`;
  }).join('');
}

export function paramsCell(adapter, model, provider) {
  if (!provider) return '<span class="hint">选 provider 后按清单显示</span>';
  if (!adapter) return '<span class="hint">清单里没有这一家 provider；参数不猜，运行方式也不猜</span>';
  if (!adapter.params_known) {
    return '<span class="hint">参数未核验，平台不预填（接云端 adapter 时按官方 schema 补齐）</span>';
  }
  if (!adapter.models.length) return '<span class="hint">清单里没有模型档</span>';
  if (!model) return '<span class="hint">从「模型」列选一档，或按清单输入</span>';
  const spec = adapter.models.find((m) => m.code === model);
  if (!spec) return '<span class="hint">这个模型 ID 不在清单里；参数不猜</span>';
  const chips = paramChips(spec);
  return chips ? `<div class="param-chips">${chips}</div>` : '<span class="hint">这一档没有可调参数</span>';
}

export function noteText(adapter, provider) {
  if (!provider) return '清单：选 provider 后显示适配器与出处';
  if (!adapter) return '清单：没有登记这个 provider，参数与运行方式不会被猜';
  return `清单：${escapeHtml(adapter.label)} · ${escapeHtml(adapter.source)}`;
}

export function healthCell(health) {
  const [tone, label] = health?.probe_deferred
    ? ['amber', '待现查']
    : (HEALTH[health?.state] || ['gray', '未知']);
  const detail = health?.detail || '';
  return `<span class="chip ${tone}" title="${escapeHtml(detail)}">${label}</span>
          <span class="hint">${escapeHtml(detail)}</span>`;
}

export function badgeOf(v) {
  for (const kind of ['target_machine', 'artifact_review', 'real_call', 'mock', 'env_check']) {
    if ((v?.[kind] || {}).state === 'ok') return kind;
  }
  return 'never';
}

export function validationCell(r) {
  const v = r.validation || {};
  const kind = badgeOf(v);
  const [tone, label] = VAL_BADGE[kind];
  const tip = ['env_check', 'mock', 'real_call', 'target_machine', 'artifact_review'].map((k) => {
    const item = v[k] || {};
    const st = (VAL_STATE[item.state] || ['', item.state || '没有记录'])[1];
    return `${VAL_KIND_LABEL[k]}：${st}${item.at ? '（' + String(item.at).slice(5, 16) + '）' : ''}`;
  }).join('\n');
  /* 生产可用与否用徽章而不是 hint：hint 会被状态列的密度收口藏掉，
     而"未达生产可用"恰恰是最该常驻可见的那半句。 */
  const ready = v.production_ready
    ? '<span class="chip green">生产可用</span>'
    : '<span class="chip gray">未达生产可用</span>';
  return `<span class="chip ${tone}" title="${escapeHtml(tip)}">${label}</span> ${ready}
    <span class="mc-acts">
      <button class="btn xs" data-env-check="${r.id}">环境检查</button>
      <button class="btn xs" data-val-history="${r.id}">验证记录</button>
    </span>`;
}

/** 健康与验证并成一列：两件事都是"这一轨现在算什么状态"，
 *  拆两列各自窄到折行，不如并一列让它自己换行。 */
export function statusCell(r) {
  return `<div class="mc-status">
      <span class="mc-health">${healthCell(r.health)}</span>
      <span class="mc-validation">${validationCell(r)}</span>
    </div>`;
}

/* 展开态记在模块级：切走再切回来不该把人打开的行全关上，
   但也不写进地址——这不是"你在看哪一条"，只是临时视图状态。 */

// 运行监控里"已离线的 worker 进程"默认收着。这是视图状态、不是业务状态：
// 它不进地址栏、不进库，但必须活过轮询重绘——否则每 5 秒刷新一次，
// 人刚点开历史就被折叠回去，等于这个开关白做（与 production.js 的结果条同一类坑）。

const slotOpen = new Set();
const emptyOpen = new Set();

/* 展开态的细节行。两个按钮留在主行——"检查"是这一页最常用的动作，
   不该藏两层；这里只补主行为了密度而藏起来的那句长解释。 */
export function slotDetail(r, adapter) {
  const model = adapter?.models?.find((m) => m.code === r.model);
  const params = model && model.params && model.params.length
    ? `<div class="mc-params" style="display:flex;flex-wrap:wrap;gap:4px;align-self:center">
         ${paramChips(model)}</div>`
    : adapter && !adapter.params_known
      ? '<span class="hint">参数未核验，平台不预填</span>'
      : '<span class="hint">这一档没有可调参数</span>';
  const keyLine = r.backend === 'cloud'
    ? `<div class="mc-detail-actions" style="align-items:center;flex-wrap:wrap">
         <input class="ctl sm mono" style="max-width:260px" data-f="key_env"
                value="${escapeHtml(r.key_env_custom)}"
                placeholder="${escapeHtml(r.key_env || 'FV_KEY_<PROVIDER>')}">
         <span class="hint">实际读 <code>${escapeHtml(r.key_env || '按 provider 推')}</code> ·
           ${r.has_key ? '<b>已配置</b>' : '未配置'}；值永远只从进程环境读</span></div>`
    : `<span class="hint">本机运行时不需要密钥；权重目录走
         <code>${escapeHtml((adapter && adapter.home_env) || 'FASTVIDEO_<CODE>_HOME')}</code></span>`;
  return `<tr class="mc-detail" data-detail-for="${r.id}">
    <td colspan="6"><div class="form-grid">
      <label class="top">清单</label>
      <span class="hint mc-src">${noteText(adapter, r.provider)}</span>
      <label>允许参数</label>${params}
      <label>密钥</label>${keyLine}
      <label>健康说明</label><div class="mc-detail-actions">
        <span class="hint">${escapeHtml(r.health.detail || '没有健康检查记录')}</span></div>
      <label>备注</label><input class="ctl" data-n="notes" value="${escapeHtml(r.notes)}"
        placeholder="为什么选它、计价出处、哪天核过">
    </div></td></tr>`;
}

export function slotRow(r, providerLists) {
  const adapter = adapterOf(cache.manifest, r.capability, r.backend, r.provider);
  const providerOptions = (providerLists[`${r.capability}|${r.backend}`] || [])
    .map((a) => `<option value="${escapeHtml(a.code)}">${escapeHtml(a.label)}</option>`).join('');
  const modelOptions = (adapter?.models || [])
    .map((m) => `<option value="${escapeHtml(m.code)}">${escapeHtml(m.label)}</option>`).join('');
  const videoCloud = r.capability === 'video' && r.backend === 'cloud';
  const main = `<tr class="slot"
      data-id="${r.id}" data-cap="${r.capability}" data-backend="${r.backend}">
    <td class="t-strong">${r.capability_label}</td>
    <td><span class="chip ${r.backend === 'local' ? 'plain' : 'blue'}">${r.backend_label}</span></td>
    <td><div class="mc-picks">
        <input class="ctl sm" data-f="provider" list="prov-${r.id}"
               value="${escapeHtml(r.provider)}" placeholder="provider 标识">
        <input class="ctl sm" data-f="model" list="model-${r.id}"
               value="${escapeHtml(r.model)}" placeholder="模型 ID">
        <datalist id="prov-${r.id}">${providerOptions}</datalist>
        <datalist id="model-${r.id}">${modelOptions}</datalist>
      </div></td>
    <td>${statusCell(r)}</td>
    <td class="c"><input type="checkbox" data-f="enabled" ${r.enabled ? 'checked' : ''}></td>
    <td class="c"><input type="radio" name="def-${r.capability}" data-f="default"
             ${r.is_default ? 'checked' : ''} ${videoCloud ? 'disabled' : ''}
             ${videoCloud ? 'title="云端视频只能逐镜头显式选择，不能设为默认"' : ''}></td>
  </tr>`;
  return main + (slotOpen.has(r.id) ? slotDetail(r, adapter) : '');
}

/* 没填 provider 的轨折成一行灰字。十行里本机只填了三行，
   剩下七行不该和已配置的等重——这是这一屏最大的噪声源。 */
export function slotCollapsed(r) {
  if (emptyOpen.has(r.id)) return slotRow(r, {});
  const marked = '';
  return `<tr class="mc-collapsed" data-empty-for="${r.id}">
    <td class="t-strong">${r.capability_label}</td>
    <td><span class="chip gray">${r.backend_label}</span></td>
    <td colspan="4">未配置 · <button class="mc-open" data-open-empty="${r.id}">去配置</button>
      ${marked}</td>
  </tr>`;
}

export function pageEngines() {
  const manifest = cache.catalog && cache.manifest;
  const providerLists = {};
  (manifest?.adapters || []).forEach((a) => {
    (providerLists[`${a.capability}|${a.backend}`] ||= []).push(a);
  });

  /* 一槽一行；没填 provider 的折成一行灰字，填过的点开才看细节。
     上一版每槽固定两行、备注带里又塞四层，十槽 20 行往上，
     一屏看不见四类能力——这是这一屏最大的噪声源。 */
  const body = cache.providers.map((r) =>
    (r.provider || slotOpen.has(r.id) || emptyOpen.has(r.id))
      ? slotRow(r, providerLists) : slotCollapsed(r)).join('');

  /* 引擎从 13 张卡收成一张表。13 行数据用"每项一行"的卡片是负资产：
     整页 4582px，一屏看不见三张卡，比较"哪家参数核过"要靠滚动记忆。 */
  const byCode = {};
  (manifest?.adapters || []).forEach((a) => { (byCode[a.code] ||= []).push(a); });
  const lastCheck = {};
  (cache.validations?.items || []).forEach((v) => {
    const key = v.adapter_code || v.provider;
    if (key && !lastCheck[key]) lastCheck[key] = v.created_at;
  });
  const capLabel = cache.catalog.capabilities;
  const engineRows = Object.entries(byCode).map(([code, list]) => {
    const tiers = list.reduce((n, a) => n + a.models.length, 0);
    const conn = [...new Set(list.map((a) => a.home_env || a.key_env).filter(Boolean))]
      .map((v) => `<code>${escapeHtml(v)}</code>`).join(' ');
    const checked = lastCheck[code] ? String(lastCheck[code]).slice(0, 16) : '';
    return `<tr>
      <td class="t-strong">${escapeHtml(list[0].label)}
        <div class="hint mono">${escapeHtml(code)}</div></td>
      <td>${list.map((a) => `<span class="chip ${a.backend === 'local' ? 'plain' : 'blue'}">${a.backend}</span>`).join(' ')}</td>
      <td><span class="hint">${[...new Set(list.map((a) => capLabel[a.capability] || a.capability))].join(' · ')}</span></td>
      <td class="c">${tiers || '<span class="chip amber">只登记名字</span>'}</td>
      <td>${list.every((a) => a.params_known)
          ? '<span class="chip green">已核验</span>'
          : '<span class="chip amber">未核验</span>'}</td>
      <td><span class="hint">${conn || '—'}</span></td>
      <td class="t-dim">${escapeHtml(checked || '没有记录')}</td>
    </tr>`;
  }).join('');

  /* 未接入行与"能力轨折叠行"必须分两个类名：前者整行没有按钮，
     后者带一个「去配置」。共用一个类的话，"未接入行没有可点按钮"这条断言
     会被折叠行里的按钮判成假——上一轮就是这么撞上的。 */
  const unintegrated = (cache.catalog.unintegrated_engines || []).map((e) => `<tr class="mc-offline">
    <td class="t-strong">${escapeHtml(e.label)}<div class="hint mono">${escapeHtml(e.code)}</div></td>
    <td><span class="chip gray">${escapeHtml(e.kind)}</span></td>
    <td colspan="3"><span class="hint">${escapeHtml(e.note)}</span></td>
    <td><span class="chip gray">未接入</span></td>
    <td><span class="hint">没有连接检查：平台里没有它的适配器</span></td>
  </tr>`).join('');

  return `
  <div class="card" style="margin-bottom:12px">
    <div class="card-head">
      <h2>能力轨</h2>
      <span class="sub">默认轨走打勾那条；云端视频只能逐镜头另选</span>
      <span class="right">
        <span class="chip plain" id="save-state" hidden>已保存</span>
        <!-- 与右边那颗 primary 同档（sm）：两者并排在 card-head 里，
             用 xs 会矮 10px、字号小一档，看着像"主按钮旁边挂了个表格内小按钮"。
             全站 card-head .right 里的按钮都走 sm，这里没有理由是例外。 -->
        <button class="btn sm" id="slots-expand">${slotOpen.size ? '全部收起' : '展开全部细节'}</button>
        <!-- 主按钮也走 sm：全站 card-head .right 里都是 btn sm primary
             （agents／composition／pipeline／review 一致），只有这里用了默认档,
             于是和旁边那颗 28px 的次级按钮并排时高出 5px。 -->
        <button class="btn sm primary" id="save-providers">保存能力轨</button>
      </span>
    </div>
    <div class="table-wrap">
      <table>
      <thead><tr>
        <th>能力</th><th>后端</th><th>选型（Provider / 模型）</th>
        <th>状态（健康 · 验证）</th><th class="c">启用</th><th class="c">默认</th>
      </tr></thead>
      <tbody>${body}</tbody>
      </table>
    </div>
    <div class="card-body" style="border-top:1px solid var(--line)">
      <p class="note flush" ><b>点「能力」那一格展开细节</b>：清单出处、允许参数、
        密钥变量名、环境检查与验证记录、备注都在展开里。
        这一张表是默认轨的唯一来源——调用策略的建议轨与备用轨只是提示，本地失败不会自动转云端。
        健康与验证也不是一回事：健康只说环境齐不齐，验证说凭什么认为真能产出。</p>
    </div>
  </div>

  <div class="card">
    <div class="card-head"><h2>引擎</h2>
      <span class="sub">清单里登记过的适配器；最后一排是本机装了但平台未接入的</span></div>
    <div class="table-wrap"><table>
      <thead><tr><th>引擎</th><th>位置</th><th>能力</th><th class="c">模型档</th>
        <th>参数核验</th><th>连接方式（变量名）</th><th>最近检查</th></tr></thead>
      <tbody>${engineRows}${unintegrated}</tbody>
    </table></div>
    <div class="card-body" style="border-top:1px solid var(--line)">
      <p class="note flush" ><b>未接入那一排没有「测试连接」按钮</b>：
        平台里没有它们的适配器，点了也不会有真结果。接 ComfyUI 是独立工作包。</p>
    </div>
  </div>

  ${engineInstancesCard()}`;
}

/* ================= 工作区 1b · 引擎实例（0040） ================= */
/**
 * 这一张卡是"端点住在哪"的那一格。后端 W-B 就建好了表与五个接口，
 * 但界面一直没接——于是 `engines.probe()` 的提示「在「引擎管理 · 引擎实例」里
 * 加一条端点」指向一个不存在的界面，而人只能去改库。
 *
 * 三条与后端同一口径的边界，卡片里必须写出来：
 *   * engine_code 必须在清单里（实例不能凭空发明 provider）；
 *   * 健康是**现查**的，库里没有那一列，所以"停用的实例不探测"；
 *   * 密钥只到"变量名 + 有没有值"，值永不显示。
 */
/**
 * 「上次探测」那一行（0042）。
 *
 * 为什么卡面上要有它：`GET /api/engines` 的健康是**这一次**现查的，而
 * 「上次是什么时候探的」是另一件事——engine 那一族以前不落任何痕，所以
 * "这台上周还连不连得上"谁也答不上来。现在有了留痕，就得把最近的结论摆出来，
 * 否则人还是会以为"我看到的这个绿色是刚才测的"。
 *
 * 三档写法，各有各的必要：
 *   从没探过 → 明说"从没探测过"，不假装刚查过；
 *   建实例时顺手探的 → 标明"建实例时"，别让人以为是自己点过；
 *   人按过「探测」    → 给时间（相对时间，全站同一定义）与耗时。
 */
export function probeHistoryLine(it) {
  if (!it.last_probe_at) {
    return '<div class="hint">还没有探测记录</div>';
  }
  const when = String(it.last_probe_at).slice(0, 16);
  const rel = agoText(it.last_probe_age_seconds);
  const via = it.last_probe_source === 'create' ? '建实例时顺手探的' : '手动探测';
  const ms = it.last_probe_latency_ms != null ? ` · ${it.last_probe_latency_ms} ms` : '';
  return `<div class="hint" title="${escapeHtml(it.last_probe_detail || '')}">
    上次 ${escapeHtml(when)}${rel ? `（${escapeHtml(rel)}）` : ''}
    · ${escapeHtml(via)}${escapeHtml(ms)}</div>`;
}

/**
 * 一条实例的探测历史（0042）。
 *
 * 为什么用弹窗而不是卡面上多一行：卡面那一行只回答「上次什么时候探的」，
 * 而「这台是不是三天两头掉线」要看几条才答得上来。20 条塞进表格一行会把
 * 整张表撑成三层楼，所以另开一屏。
 *
 * 三条要知道的事都写在这一屏里：
 *  1. 历史**只回答过去**——现在通不通仍看卡面上那枚现查的徽章；
 *  2. `create` 那条是「建实例时顺手探的」，不是人点过；
 *  3. 每行都给了耗时与距上一次的间隔，掉线有没有规律一眼看得出来。
 */
export function openProbeHistory(data) {
  const items = data.items || [];
  const rows = items.map((it, i) => {
    const next = items[i + 1];
    // 与更早那条之间隔了多久：同一个端点连着探两次都说连不上，
    // 和「隔了三天才探一次」，说的不是一回事。
    const gap = next && it.age_seconds != null && next.age_seconds != null
      ? fmtGap(it.age_seconds - next.age_seconds) : '';
    return `<tr>
      <td class="t-dim">${escapeHtml(String(it.created_at || '').slice(0, 16))}</td>
      <td>${healthChip({ state: it.state, detail: it.detail })}</td>
      <td class="hint">${it.source === 'create' ? '建实例时顺手探的' : '手动探测'}</td>
      <td class="hint mono">${escapeHtml(it.engine_model || '—')}</td>
      <td class="num t-dim">${it.latency_ms != null ? it.latency_ms + ' ms' : '—'}</td>
      <td class="hint">${escapeHtml(gap || '')}</td>
    </tr>`;
  }).join('');
  const body = items.length
    ? `<div class="table-wrap"><table><thead><tr>
        <th>什么时候</th><th>结果</th><th>怎么探的</th><th>引擎自述的模型</th>
        <th class="num">耗时</th><th>距上一次</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
    : '<p class="hint">这条实例还没有探测记录。点「探测」会留下第一条。</p>';
  const host = document.createElement('div');
  host.className = 'modal';
  host.innerHTML = `<div class="modal-box wide">
      <div class="modal-head neutral">探测历史</div>
      <div class="modal-body">${body}
        <p class="note" style="margin:12px 0 0"><b>这一屏只讲过去。</b>
          现在通不通，看引擎实例卡上那枚状态徽章——它是每次现查的，
          和这里的任何一条都没关系。${escapeHtml((data && data.note) || '')}</p>
      </div>
      <div class="modal-actions"><button class="btn" data-close-probes>关闭</button></div>
    </div>`;
  document.body.appendChild(host);
  const close = () => host.remove();
  host.addEventListener('click', (e) => {
    if (e.target === host || e.target.closest('[data-close-probes]')) close();
  });
  host.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
}

/**
 * 引擎连接检查结果窗（页头「检查引擎连接」按完弹出）。
 *
 * 数据来自 `GET /api/engines`，**这是"引擎列表"的权威口径**：库里每一台
 * 登记过的实例一行，带着它自己的端点（`engine_instance` 的行），
 * 后端逐台按那个端点真发一次 HTTP 现查（列表档 0.8 秒超时）。
 *
 * 为什么不用 `POST /api/models/probe` 的 `probed`：那个数组是按
 * `engine_code` **去重**过的，回答的是"这一家引擎通不通"，不是"我登记的
 * 这几台在哪、通不通"。同一家引擎登记了两台机器时，probed 只留第一台
 * （`engine_instance.pick()` 选出来的那台）的结论，另一台的死活它根本
 * 看不见——照它渲染的结果窗会替没探过的那台印一个它没有的结论。
 *
 * 三档状态各说各的，不许互相冒充：
 *   · `ok`——连上了。只证明进程在线、端点答话。
 *   · `missing`——配了但服务不在（人要去把应用和 API Server 打开）。
 *   · `unknown`——平台没查：已停用，或连接方式还没有探测分支。
 *
 * 「引擎在线」与「模型可用」是两件事：这一屏只讲前者。后者要过验证那几关，
 * 后端 `_status_of` 里任何一档都不许往上跳，这里也不许替它跳。
 *
 * 第二段是**云端**（2026-11-08 补）。云端引擎不在实例表里，权威口径是
 * `POST /api/models/probe` 返回的 `cloud` 数组（后端 `cloud_probe.py`）：
 * 逐家去供应商网关问一次"路通不通、钥匙对不对"。它和第一段问的不是同一个
 * 问题，所以各说各话、不合并成一句总数——把"云端 2 家"混进"现查 N 台"里
 * 会让人以为本机有台机器没连上。
 */
export function openProbeResults(payload, cloudRows) {
  const inst = payload || {};
  const items = inst.items || [];
  const service = new Set(inst.service_engines || []);
  const cloud = Array.isArray(cloudRows) ? cloudRows : [];

  const rowsNow = items.map((it) => {
    const name = it.display_name || it.engine_label || it.engine_code;
    const served = service.has(it.engine_code);
    // 三档分开：停用/没有探测分支的是「没查」，不是「连不上」。
    let chip;
    if (!it.enabled) {
      chip = '<span class="chip gray">已停用</span>';
    } else if (it.probe_unsupported) {
      chip = '<span class="chip amber" title="连接方式还没有探测分支，平台不拿一次 HTTP 的结论冒充它">探测未实现</span>';
    } else {
      // 这一屏人问的就是"连上没有"，所以直接用这两个词，不再叠全局徽章的
      // 通用译名（`missing` 在别处叫「缺东西」，那是清单/密钥那一侧的语境；
      // 摆在这里会变成"连不上 缺东西"两句说同一件事）。口径说明放进 title，
      // 想看的人 hover 得到。
      chip = it.health_state === 'ok'
        ? `<span class="chip green" title="${escapeHtml(it.health_detail || '')}">连上了</span>`
        : it.health_state === 'missing'
          ? `<span class="chip red" title="${escapeHtml(it.health_detail || '')}">连不上</span>`
          : `<span class="chip amber" title="${escapeHtml(it.health_detail || '')}">查不了</span>`;
    }
    const queried = it.enabled && !it.probe_unsupported;
    return `<tr>
      <td class="t-strong">${escapeHtml(name)}
        <div class="hint mono">${escapeHtml(it.engine_code)}${
          served ? '' : ' · 非服务型'}</div></td>
      <td><code>${escapeHtml(it.endpoint || '—')}</code>
        <div class="hint">${escapeHtml(it.connection_type || '')}</div></td>
      <td>${chip}
        ${queried && it.engine_model
          ? `<div class="hint mono">${escapeHtml(it.engine_model)}</div>` : ''}
        <div class="hint">${escapeHtml(it.health_detail || '')}</div></td>
      <td class="hint">${escapeHtml(it.note || '')}</td>
    </tr>`;
  }).join('');

  const queried = items.filter((it) => it.enabled && !it.probe_unsupported);
  const okN = queried.filter((it) => it.health_state === 'ok').length;
  const badN = queried.filter((it) => it.health_state === 'missing').length;
  const skipped = items.length - queried.length;

  // 一句结论：人想知道的首先就是"通了没有"，不该让他去数徽章。
  const verdict = !items.length
    ? `<p class="note" style="margin:0 0 10px"><b>还没有登记过引擎实例。</b>
      Draw Things、Ollama 这类"跑成一台常驻服务"的引擎，要先在「引擎管理」
      里给它写一条端点（例如 <code>http://127.0.0.1:7860</code>），
      这里才有可查的对象。</p>`
    : `<p class="note" style="margin:0 0 10px">
      <b>本次现查 ${queried.length} 台：通 ${okN} 台${badN ? `，不通 ${badN} 台` : ''}。</b>
      ${skipped ? `另有 ${skipped} 台没查（已停用或连接方式还没有探测分支）。` : ''}
      <b>连上只说明引擎进程在线、端点答话，不等于那一档模型能用</b>——
      「可用」还要过验证那几关，这一屏不替它下这个结论。</p>`;

  const body = items.length
    ? `<div class="table-wrap"><table><thead><tr>
        <th>引擎实例</th><th>端点</th><th>状态</th><th>备注</th>
      </tr></thead><tbody>${rowsNow}</tbody></table></div>`
    : '';

  // 云端一段：逐家供应商一行，结论来自 cloud_probe（去供应商网关问一次）。
  // 五档各说各的，chip 用的就是"要不要动、动什么"的词，不互相冒充。
  const CLOUD_CHIP = {
    ok: ['green', '路通、钥匙被接受'],
    auth_rejected: ['red', '路通、钥匙被拒'],
    unreachable: ['red', '连不上'],
    no_key: ['amber', '没配密钥'],
    no_endpoint: ['amber', '无可探端点'],
  };
  const cloudRowsNow = cloud.map((c) => {
    const [tone, word] = CLOUD_CHIP[c.state] || ['amber', c.state || '未知'];
    const where = c.url ? `<code>${escapeHtml(c.url)}</code>`
                        : `<span class="hint">${escapeHtml(c.key_env || '')}</span>`;
    return `<tr>
      <td class="t-strong">${escapeHtml(c.label || c.provider)}
        <div class="hint mono">${escapeHtml(c.provider)} · ${escapeHtml(c.capability || '')}</div></td>
      <td>${where}</td>
      <td><span class="chip ${tone}" title="${escapeHtml(c.detail || '')}">${escapeHtml(word)}</span>
        <div class="hint">${escapeHtml(c.detail || '')}</div></td>
    </tr>`;
  }).join('');

  const cloudVerdict = cloud.length
    ? `<p class="note" style="margin:14px 0 10px">
        <b>云端 ${cloud.length} 家：现查过供应商网关。</b>
        这里只回答"路通不通、钥匙被不被接受"，<b>不等于那一档模型能用</b>；
        「没配密钥」「无可探端点」两档没发出请求，是配置层面的事实，不是网络故障。</p>`
    : '';
  const cloudBody = cloud.length
    ? `<div class="table-wrap"><table><thead><tr>
        <th>云端引擎</th><th>探测目标</th><th>状态</th>
      </tr></thead><tbody>${cloudRowsNow}</tbody></table></div>`
    : '';

  const host = document.createElement('div');
  host.className = 'modal';
  host.innerHTML = `<div class="modal-box wide">
      <div class="modal-head neutral">引擎连接检查 · 结果</div>
      <div class="modal-body">
        ${verdict}
        ${body}
        ${cloud.length ? '<div class="modal-head neutral" style="margin-top:16px">云端引擎 · 现查</div>' : ''}
        ${cloudVerdict}
        ${cloudBody}
        <p class="note" style="margin:12px 0 0">
          这一屏是<b>刚刚这一轮</b>的现查结论。本机实例用的是每台实例自己的端点，
          列表档超时 0.8 秒，服务半死时可能被判成不通，要问到底请到
          「引擎管理」点那一行的<b>探测</b>（5 秒档），那边还留着历史。
          云端引擎这一轮<b>真去供应商网关发了一次 GET</b>（5 秒档）：探的是
          "路通不通、钥匙对不对"，没配密钥或没登记端点的不会发出请求。
          两类都不等于那一档模型可用——可用要过验证那几关。</p>
      </div>
      <div class="modal-actions"><button class="btn" data-close-probe-res>关闭</button></div>
    </div>`;
  document.body.appendChild(host);
  const close = () => host.remove();
  host.addEventListener('click', (e) => {
    if (e.target === host || e.target.closest('[data-close-probe-res]')) close();
  });
  host.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
}

/** 两条探测之间隔了多久。数字都是秒；负值（同一秒内）当 0。 */
export function fmtGap(seconds) {
  const n = Math.max(0, Number(seconds) || 0);
  if (n < 60) return `${n} 秒`;
  if (n < 3600) return `${Math.floor(n / 60)} 分钟`;
  if (n < 86400) return `${Math.floor(n / 3600)} 小时`;
  return `${Math.floor(n / 86400)} 天`;
}

/** 引擎实例那一张卡（0040 端点表 ＋ 0042 探测留痕）。 */
export function engineInstancesCard() {
  const inst = cache.engines
    || { items: [], connection_types: ['local_http'], service_engines: [] };
  const items = inst.items || [];
  const service = inst.service_engines || [];

  // 可选的引擎 code：清单里出现过的都算（后端 `_adapter_for` 只认清单），
  // 但服务型排前面并标出来——只有它们会真发 HTTP 去问端点。
  const seen = new Set();
  const choices = [];
  (cache.manifest?.adapters || []).forEach((a) => {
    if (seen.has(a.code)) return;
    seen.add(a.code);
    choices.push({ code: a.code, label: a.label, served: service.includes(a.code) });
  });
  choices.sort((a, b) => (b.served - a.served) || a.code.localeCompare(b.code));
  const codeOpts = choices.map((c) => `<option value="${escapeHtml(c.code)}">${
    escapeHtml(c.label)}（${escapeHtml(c.code)}）${c.served ? '' : ' · 非服务型'}</option>`).join('');

  const rows = items.map((it) => {
    const engine = choices.find((c) => c.code === it.engine_code);
    const name = it.display_name || it.engine_label || it.engine_code;
    const secret = it.shared_secret_env
      ? `<code>${escapeHtml(it.shared_secret_env)}</code>
         ${it.secret_present ? '<span class="chip green">有值</span>'
                            : '<span class="chip amber">变量没值</span>'}`
      : '<span class="hint">—</span>';
    return `<tr class="engine-inst" data-inst="${it.id}">
      <td class="t-strong">${escapeHtml(name)}
        <div class="hint mono">${escapeHtml(it.engine_code)}${
          engine && !engine.served ? ' · 非服务型' : ''}</div></td>
      <td><code>${escapeHtml(it.endpoint)}</code></td>
      <td><span class="hint">${escapeHtml(it.connection_type)}</span> ${secret}</td>
      <td>${it.enabled
          ? (it.probe_unsupported
              ? '<span class="chip amber">探测未实现</span>'
              : healthChip({ state: it.health_state, detail: it.health_detail }))
          : '<span class="chip gray">已停用</span>'}
        ${it.engine_model ? `<div class="hint mono">${escapeHtml(it.engine_model)}</div>` : ''}
        ${it.health_detail ? `<div class="hint">${escapeHtml(it.health_detail)}</div>` : ''}
        ${probeHistoryLine(it)}</td>
      <td class="t-dim">${escapeHtml(it.note || '')}</td>
      <td class="c" style="white-space:nowrap">
        <button class="btn xs" data-act-probe="${it.id}">探测</button>
        <button class="btn xs" data-act-probes="${it.id}">看历史</button>
        <button class="btn xs" data-act-toggle="${it.id}" data-to="${it.enabled ? 0 : 1}">${
          it.enabled ? '停用' : '启用'}</button>
        <button class="btn xs danger" data-act-uninst="${it.id}">撤实例</button>
      </td>
    </tr>`;
  }).join('');

  const empty = items.length ? '' : `<tr><td colspan="6"><span class="hint">
    还没有启用中的引擎实例。Draw Things、Ollama 这类"跑成一台常驻服务"的引擎，
    要先在这里写一条端点（例如 <code>http://127.0.0.1:7860</code>），
    模型仓库那边才查得到它的健康状况。</span></td></tr>`;

  return `
  <div class="card">
    <div class="card-head"><h2>引擎实例</h2>
      <span class="sub">这一档引擎在哪台机器、哪个端口；健康是每次现查，不落库</span></div>
    <div class="table-wrap"><table>
      <thead><tr><th>实例</th><th>端点</th><th>连接方式（密钥变量）</th>
        <th>状态</th><th>备注</th><th class="c">操作</th></tr></thead>
      <tbody>${rows}${empty}
        <tr class="mc-add-inst">
          <td><select class="ctl sm" id="eng-code">${codeOpts || '<option value="">清单里还没有引擎</option>'}</select></td>
          <td><input class="ctl sm mono" id="eng-endpoint" maxlength="200"
                     placeholder="http://127.0.0.1:7860"></td>
          <td><input class="ctl sm" id="eng-name" maxlength="80" placeholder="显示名（可空）">
            <input class="ctl sm mono" id="eng-secret" maxlength="60" style="margin-top:4px"
                   placeholder="密钥变量名（可空，值只从进程环境读）"></td>
          <td colspan="2"><input class="ctl sm" id="eng-note" maxlength="300"
                   placeholder="备注：这台是谁的、为什么换机（可空）"></td>
          <td class="c"><button class="btn xs primary" id="eng-add">加实例</button></td>
        </tr>
      </tbody>
    </table></div>
    <div class="card-body" style="border-top:1px solid var(--line)">
      <p class="note flush" ><b>端点改不了</b>：端点连同引擎 code 是这一行的键，
        要换机器请新建一条再停旧的——否则历史作业的「当时打的是哪台」就说不清了。
        「非服务型」那几档也能登记（给远程执行器留的位置），但现在没有探测分支，
        加进来只会显示"不是本机服务型引擎，平台不猜它的状态"。</p>
      <p class="note"><b>连接方式今天只有 local_http 一档能选</b>：迁移 0040 的 CHECK 还收
        <code>grpc</code> 与 <code>cli</code>，但探测与执行都只写了 HTTP 那一条路。
        把它们摆进下拉，等于给一个选进去看不到任何行为的选项——所以接口直接拒，
        并明说"要先写探测与执行分支"。库里若有历史行用了别的值，这一屏照样列出来，
        状态写成「探测未实现」，不拿一次 HTTP 的失败冒充它的结论。</p>
    </div>
  </div>`;
}

/* ================= 工作区 1 交互（能力轨，功能原样搬自系统设置） ================= */
export function mountEngines(root) {
  const saveBtn = root.querySelector('#save-providers');
  const saveStateChip = root.querySelector('#save-state');
  let rowsCache = cache.providers;

  /* 展开/收起。整页重绘是唯一改 DOM 的路子，但会把正在输入的框和光标一起弄丢，
     所以只在"展开态变了"时重绘；改 provider/model 走下面 refresh() 的局部更新。 */
  /* 展开/收起要重画整页：这是全站切页与保存后共用的那一条路（app:refresh），
     不另发明一套局部重绘。代价是此刻若有人正在输入框里打字会被打断——
     所以只在"展开态变了"时走它，改 provider/model 走下面 refresh() 的局部更新。 */
  const repaint = () => window.dispatchEvent(new CustomEvent('app:refresh'));
  root.querySelectorAll('tr.slot td:first-child').forEach((td) => {
    td.style.cursor = 'pointer';
    td.title = '点开看清单出处、允许参数、密钥与验证动作';
    td.addEventListener('click', () => {
      const id = Number(td.closest('tr').dataset.id);
      slotOpen.has(id) ? slotOpen.delete(id) : slotOpen.add(id);
      repaint();
    });
  });
  root.querySelectorAll('[data-open-empty]').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    emptyOpen.add(Number(b.dataset.openEmpty));
    slotOpen.add(Number(b.dataset.openEmpty));   // 去配置的人下一步就是要填细节
    repaint();
  }));
  root.querySelector('#slots-expand')?.addEventListener('click', () => {
    if (slotOpen.size) {
      slotOpen.clear();
    } else {
      cache.providers.filter((r) => r.provider).forEach((r) => slotOpen.add(r.id));
    }
    repaint();
  });

  root.querySelectorAll('input[data-f="default"]').forEach((radio) => {
    radio.addEventListener('change', () => {
      radio.closest('tr').querySelector('[data-f="enabled"]').checked = true;
    });
  });

  // 改 provider/model 只刷新这一行的提示，不重绘整页——重绘会丢输入框焦点
  root.querySelectorAll('tr.slot').forEach((tr) => {
    const refresh = () => {
      const provider = tr.querySelector('[data-f="provider"]').value;
      const model = tr.querySelector('[data-f="model"]').value;
      const adapter = adapterOf(cache.manifest, tr.dataset.cap, tr.dataset.backend, provider);
      const dl = tr.querySelector(`datalist[id="model-${tr.dataset.id}"]`);
      if (dl) {
        dl.innerHTML = (adapter?.models || [])
          .map((m) => `<option value="${escapeHtml(m.code)}">${escapeHtml(m.label)}</option>`).join('');
      }
      // 展开态才有细节行；没展开就只等下一次重绘
      const detail = root.querySelector(`tr.mc-detail[data-detail-for="${tr.dataset.id}"]`);
      if (detail) {
        detail.querySelector('.mc-src').innerHTML = noteText(adapter, provider);
        detail.querySelector('.mc-params').innerHTML = paramsCell(adapter, model, provider);
        const keyInput = detail.querySelector('[data-f="key_env"]');
        if (keyInput) keyInput.placeholder = adapter?.key_env || 'FV_KEY_<PROVIDER>';
      }
      // 健康是服务端按"已保存的 provider"算的；这一行还没保存时先别摆旧结论，
      // 否则改完 provider 还亮着"未配置"，看着像输入没生效。
      tr.querySelector('.mc-health').innerHTML =
        '<span class="chip gray">待保存</span><span class="hint">保存后按新 provider 重新检查</span>';
    };
    tr.querySelector('[data-f="provider"]').addEventListener('input', refresh);
    tr.querySelector('[data-f="model"]').addEventListener('input', refresh);
  });

  saveBtn.addEventListener('click', async () => {
    // 必须以**服务端那十行**为骨架，不能只扫 DOM 里的 tr.slot：
    // 密度收口之后，没填 provider 的槽位折成 tr.mc-collapsed（不带输入框），
    // 只扫 tr.slot 就只提交 3 行，而 PUT /api/providers 要求"每类能力恰好一个默认"
    // ——图像／配音／音乐三类的默认行恰好都在折叠行里，于是整批被拒：
    // 「每类能力都要选一个默认后端：图像、配音、音乐」。按钮点了没反应，
    // 界面只会弹一句像是自己填错的话。
    const rows = (rowsCache.length ? rowsCache : cache.providers).map((saved) => {
      const tr = root.querySelector(`tr.slot[data-id="${saved.id}"]`);
      const detail = root.querySelector(`tr.mc-detail[data-detail-for="${saved.id}"]`);
      if (!tr) {
        // 折叠行：原样回读服务端那份。把这一槽的 provider／model／启用／默认
        // 一起丢了，等于"整批保存成功、顺带把没展开的槽位清空"
        return {
          id: saved.id, provider: saved.provider, model: saved.model,
          enabled: saved.enabled, is_default: saved.is_default,
          key_env: saved.key_env_custom ?? saved.key_env ?? '',
          notes: saved.notes ?? '',
          // 折叠行也要带令牌：这一行同样可能被别人改过，漏掉它就等于
          // 只给"看得见的那三行"上闸，另外七行照样能被静默盖掉。
          seen_version: saved.version || '',
        };
      }
      return {
        id: saved.id,
        provider: tr.querySelector('[data-f="provider"]').value,
        model: tr.querySelector('[data-f="model"]').value,
        enabled: tr.querySelector('[data-f="enabled"]').checked,
        is_default: tr.querySelector('[data-f="default"]').checked,
        key_env: detail
          ? detail.querySelector('[data-f="key_env"]').value
          : (saved.key_env_custom ?? saved.key_env ?? ''),
        notes: detail ? detail.querySelector('[data-n="notes"]').value : (saved.notes ?? ''),
        // 弱版本闸：带回"我打开时看到的那一版"。服务端一比就知道有没有人插进来改过——
        // 这一页是整批覆盖，没这道闸时后保存的人会把前一个人改的那一行静默盖回旧值。
        seen_version: saved.version || '',
      };
    });
    saveBtn.disabled = true;
    let ok = true;
    try {
      await api.saveProviders(rows);
    } catch (err) {
      ok = false;
      if (err.status === 401) return;
      // 409 ＝ 这十行里有人被改过（弱版本闸）。不能当成普通失败弹一句就完：
      // 人手上还留着刚填的东西，得给一条出路。再点一次「保存」仍然会 409
      // （令牌没变），所以唯一的出路是回读最新、丢失格之后重新对一遍。
      if (err.status === 409) {
        const yes = await confirmDanger({
          title: '保存被挡下了',
          body: err.message + '。这次整批没有写入。重新读一遍会把最新的读进来，'
                + '你现在填的内容会丢——需要对着变化重新对一遍。',
          okText: '重新读一遍',
        });
        if (yes) window.dispatchEvent(new CustomEvent('app:refresh'));
        return;
      }
      // 走统一出口（`run` 只认抛出的错）：这一页原来不许直接 alert，
      // 全站错误出口只有 util.js 那一处——守卫用例会红。
      await run('保存', () => { throw err; });
      return;
    } finally {
      saveBtn.disabled = false;
    }
    if (!ok) return;      // 上面每一条失败分支都已经说过话，这里只负责成功那一路
    saveStateChip.hidden = false;
    setTimeout(() => { saveStateChip.hidden = true; }, 2000);
    try {
      // 回读一份最新：**新令牌必须跟着刷新**。不刷新的话，这一页从此带着一个
      // 过期令牌，用户接着改第二次就必然撞 409——一次成功之后整页再也存不进去，
      // 那比没有闸还糟。
      const fresh = await api.providers();
      rowsCache = fresh;
      cache.providers = fresh;
      fresh.forEach(paintRow);
      bindValidationActions(root);
    } catch { /* 回读失败不影响保存本身 */ }
  });

  function paintRow(r) {
    const tr = root.querySelector(`tr.slot[data-id="${r.id}"]`);
    if (!tr) return;
    tr.querySelector('.mc-health').innerHTML = healthCell(r.health);
    tr.querySelector('.mc-validation').innerHTML = validationCell(r);
  }

  async function reloadRows() {
    const fresh = await api.providers();
    rowsCache = fresh; cache.providers = fresh;
    fresh.forEach(paintRow);
    bindValidationActions(root);
    return fresh;
  }

  function summaryHtml(v) {
    const kinds = ['env_check', 'mock', 'real_call', 'target_machine', 'artifact_review'];
    const rows = kinds.map((k) => {
      const item = v?.[k] || {};
      const at = item.at ? String(item.at).slice(0, 16) : '—';
      const job = item.job_id ? ` · 作业 #${item.job_id}` : '';
      return `<div class="kv"><dt>${escapeHtml(VAL_KIND_LABEL[k])}</dt>
        <dd>${stateChip(item.state || 'never')}
          <span class="t-dim">${escapeHtml(at)}${escapeHtml(job)}</span>
          ${item.detail ? `<span class="t-desc">${escapeHtml(item.detail)}</span>` : ''}</dd></div>`;
    }).join('');
    const ready = v?.production_ready
      ? '<span class="chip green">生产可用</span>'
      : '<span class="chip gray">未达生产可用</span>';
    const target = v?.target_machine_ready ? '<span class="chip green">目标机已验证</span>' : '';
    return `<div class="kv-grid">${rows}</div>
      <p class="modal-meta">${ready} ${target}
        <span class="hint">生产可用＝环境齐＋真实调用过＋产物验收通过，由服务端判定；界面不自己拼。</span></p>`;
  }

  function valListHtml(records) {
    if (!records.length) return '<p class="hint">这一轨还没有验证记录。</p>';
    return `<div class="table-wrap"><table><thead><tr>
      <th>出处</th><th>结果</th><th>作业</th><th>说明</th><th>登记人</th><th>时间</th>
      </tr></thead><tbody>${records.map((r) => `<tr>
        <td><span class="chip plain">${escapeHtml(r.kind_label || VAL_KIND_LABEL[r.kind] || r.kind)}</span></td>
        <td>${stateChip(r.state)}</td>
        <td>${r.job_id ? `#${r.job_id}` : '—'}</td>
        <td><span class="t-desc">${escapeHtml(r.detail || '')}</span></td>
        <td>${escapeHtml(r.created_by_name || '—')}</td>
        <td class="t-dim">${escapeHtml(String(r.created_at || '').slice(0, 16))}</td>
      </tr>`).join('')}</tbody></table></div>`;
  }

  const valModal = root.querySelector('#val-modal');
  let valSlot = null;

  async function openValModal(slot) {
    valSlot = slot;
    valModal.hidden = false;
    root.querySelector('#val-title').textContent =
      `验证记录 · ${slot.capability_label} · ${slot.backend_label}`;
    root.querySelector('#val-summary').innerHTML = summaryHtml(slot.validation);
    root.querySelector('#val-job').value = '';
    root.querySelector('#val-kind').value = 'real_call';
    root.querySelector('#val-detail').value = '';
    root.querySelector('#val-paid').checked = false;
    root.querySelector('#val-error').textContent = '';
    syncPassedRow();
    root.querySelector('#val-list').innerHTML = '<p class="hint">正在读验证历史……</p>';
    try {
      const { items: all } = await api.providerValidations(100);
      root.querySelector('#val-list').innerHTML = valListHtml(all.filter((r) =>
        r.capability === slot.capability && r.backend === slot.backend
        && r.provider === slot.provider));
    } catch (err) {
      root.querySelector('#val-list').innerHTML =
        `<p class="t-bad">验证历史读不到：${escapeHtml(err.message)}</p>`;
    }
    root.querySelector('#val-job').focus();
  }

  function syncPassedRow() {
    const isReview = root.querySelector('#val-kind').value === 'artifact_review';
    root.querySelector('#val-passed-label').hidden = !isReview;
    root.querySelector('#val-passed').hidden = !isReview;
  }

  async function submitValidation() {
    const errEl = root.querySelector('#val-error');
    errEl.textContent = '';
    if (!valSlot) return;
    const jobId = Number(root.querySelector('#val-job').value);
    if (!jobId) { errEl.textContent = '填一条已成功的作业号。'; return; }
    const kind = root.querySelector('#val-kind').value;
    const body = {
      capability: valSlot.capability, backend: valSlot.backend,
      provider: valSlot.provider, model: valSlot.model,
      kind, job_id: jobId,
      paid_confirmed: root.querySelector('#val-paid').checked,
      detail: root.querySelector('#val-detail').value.trim(),
    };
    if (kind === 'artifact_review') body.passed = root.querySelector('#val-passed').value === 'pass';
    const btn = root.querySelector('#val-submit');
    btn.disabled = true;
    try {
      await api.validateJob(jobId, body);
      const fresh = await reloadRows();
      valSlot = fresh.find((r) => r.id === valSlot.id) || valSlot;
      root.querySelector('#val-summary').innerHTML = summaryHtml(valSlot.validation);
      const { items: all } = await api.providerValidations(100);
      root.querySelector('#val-list').innerHTML = valListHtml(all.filter((r) =>
        r.capability === valSlot.capability && r.backend === valSlot.backend
        && r.provider === valSlot.provider));
      root.querySelector('#val-job').value = '';
      root.querySelector('#val-detail').value = '';
    } catch (err) {
      errEl.textContent = err.message || '登记失败';
    } finally {
      btn.disabled = false;
    }
  }

  // 重画验证列后要重绑，所以用 onclick 赋值而不是 addEventListener（不叠监听器）
  function bindValidationActions(scope) {
    scope.querySelectorAll('[data-env-check]').forEach((btn) => {
      btn.onclick = async () => {
        const slot = rowsCache.find((r) => String(r.id) === btn.dataset.envCheck);
        if (!slot) return;
        btn.disabled = true;
        const ok = await run('环境检查', () => api.validateProvider({
          capability: slot.capability, backend: slot.backend,
          provider: slot.provider, kind: 'env_check',
        }));
        btn.disabled = false;
        if (ok) await reloadRows().catch(() => {});
      };
    });
    scope.querySelectorAll('[data-val-history]').forEach((btn) => {
      btn.onclick = () => {
        const slot = rowsCache.find((r) => String(r.id) === btn.dataset.valHistory);
        if (slot) openValModal(slot);
      };
    });
  }

  bindValidationActions(root);
  root.querySelector('#val-close').addEventListener('click', () => { valModal.hidden = true; });
  root.querySelector('#val-submit').addEventListener('click', submitValidation);
  root.querySelector('#val-kind').addEventListener('change', syncPassedRow);

  mountEngineInstances(root);
}

/* ================= 工作区 1b 交互 · 引擎实例 ================= */
/**
 * 加、停启用、探测、撤实例。四个动作全部走 `app:refresh` 整页重绘，
 * 不在这里手改 DOM：探测结果来自服务端现查，本地拼一个"就绪"就是在撒谎。
 *
 * 撤实例要二次确认（`confirmDanger`），而且确认文案必须说清它**不动什么**——
 * 这一格最容易被理解成"把 Draw Things 删了"。
 */
export function mountEngineInstances(root) {
  const repaint = () => window.dispatchEvent(new CustomEvent('app:refresh'));
  const addBtn = root.querySelector('#eng-add');
  addBtn?.addEventListener('click', async () => {
    const body = {
      engine_code: root.querySelector('#eng-code').value,
      endpoint: root.querySelector('#eng-endpoint').value.trim(),
      display_name: root.querySelector('#eng-name').value.trim(),
      shared_secret_env: root.querySelector('#eng-secret').value.trim(),
      note: root.querySelector('#eng-note').value.trim(),
    };
    if (!body.engine_code) { alertNotify('先选一个引擎：清单里现在没有可选的一档。'); return; }
    if (!body.endpoint) { alertNotify('端点不能空——这一行的全部意义就是「这一档引擎在哪个地址」。'); return; }
    addBtn.disabled = true;
    let out = null;
    const ok = await run('加实例', async () => { out = await api.createEngine(body); });
    addBtn.disabled = false;
    if (!ok) return;
    // 新建时顺手探过一次；连不上不回 4xx（登记一个"还没启动的服务"是合法动作），
    // 那句 warning 必须让人看到，否则人以为加完就通了。
    if (out && out.warning) alertNotify(out.warning);
    repaint();
  });

  root.querySelectorAll('[data-act-probe]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      let out = null;
      const ok = await run('探测', async () => { out = await api.probeEngine(btn.dataset.actProbe); });
      btn.disabled = false;
      if (!ok || !out) return;
      alertNotify(out.state === 'ok'
        ? `连上了：${out.model || '（服务没报模型名）'} @ ${out.endpoint}`
        : `没连上：${out.detail}`);
      repaint();
    });
  });

  // 看历史：这一台最近几次探测。为什么不塞进卡面——20 条历史塞进一行会把
  // 那张表撑成三层楼；而「这台是不是三天两头掉线」是要停下来看的判断，
  // 顺手扫一眼能过的场合（上次什么时候探的、结果如何）卡面上那一行已经够了。
  root.querySelectorAll('[data-act-probes]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      let data = null;
      const ok = await run('读探测历史', async () => {
        data = await api.engineProbes(btn.dataset.actProbes);
      });
      btn.disabled = false;
      if (ok && data) openProbeHistory(data);
    });
  });

  root.querySelectorAll('[data-act-toggle]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const on = btn.dataset.to === '1';
      btn.disabled = true;
      const ok = await run(on ? '启用' : '停用',
        () => api.patchEngine(btn.dataset.actToggle, { enabled: on }));
      btn.disabled = false;
      if (ok) repaint();
    });
  });

  root.querySelectorAll('[data-act-uninst]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.actUninst;
      const yes = await confirmDanger({
        title: '撤这一条实例登记',
        body: '只删端点这一行：应用、权重与历史作业一律不动，旧作业仍按当时冻结的快照可查。'
          + '要换机器就撤了再登记——端点改不了，它连同引擎 code 是这一行的键。',
        okText: '撤实例',
        danger: true,
      });
      if (!yes) return;
      const ok = await run('撤实例', () => api.removeEngine(id));
      if (ok) repaint();
    });
  });
}
