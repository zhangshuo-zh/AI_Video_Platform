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
import { enhanceSelect } from '../../pick.js';
import { HEALTH, VAL_BADGE, VAL_KIND_LABEL, alertLabel } from '../../validation.js';
import { REG_SOURCES, unitLabel } from '../../enums.js';
import {
  SUBS, cache, stateChip, healthChip, manifestAxis, adapterOf, manifestProviders,
  sourceLabels, sourceLabel, overviewCard, alertNotify, VAL_STATE, HISTORY_PAGE,
  resolveSeq, bumpResolveSeq, showWorkerHistory, toggleWorkerHistory,
  setupValidationModal,
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
  // 密钥这一行：变量名可改 + **指纹**（2026-11-11）。
  //
  // 指纹解决的是"这台机器上配的是供应商后台里哪一把 key"——人不必去
  // `cat ~/.zshrc`（那等于给 shell）。它只有 sha256 前 8 位与长度，推不回原值。
  const fp = r.secret_fingerprint || null;
  const fpLine = (r.backend === 'cloud' && fp && fp.present)
    ? `<span class="hint" style="display:block;margin-top:2px">
         <code title="sha256 前 8 位，推不回原值">${escapeHtml(fp.preview)}</code>
         · ${fp.length} 字符
         <button class="btn xs ghost" data-secret-test="${escapeHtml(r.provider)}">测试连接</button>
       </span>` : '';
  const keyLine = r.backend === 'cloud'
    ? `<div class="mc-detail-actions" style="align-items:center;flex-wrap:wrap">
         <input class="ctl sm mono" style="max-width:260px" data-f="key_env"
                value="${escapeHtml(r.key_env_custom)}"
                placeholder="${escapeHtml(r.key_env || 'FV_KEY_<PROVIDER>')}">
         <span class="hint">实际读 <code>${escapeHtml(r.key_env || '按 provider 推')}</code> ·
           ${r.has_key ? '<b>已配置</b>' : '未配置'}；值永远只从进程环境读</span>
         ${fpLine}
         ${(r.backend === 'cloud' && (!fp || !fp.present))
           ? `<span class="hint">没有指纹可显示：这个变量在当前进程环境里没有值</span>` : ''}
       </div>`
    : `<span class="hint">本机运行时不需要密钥；权重目录走
         <code>${escapeHtml((adapter && adapter.home_env) || 'FASTVIDEO_<CODE>_HOME')}</code></span>`;
  return `<tr class="mc-detail" data-detail-for="${r.id}">
    <td colspan="6"><div class="form-grid">
      <label class="top">清单</label>
      <span class="hint mc-src">${noteText(adapter, r.provider)}</span>
      <label>允许参数</label><div class="mc-params">${params}</div>
      <label>密钥</label>${keyLine}
      <label>健康说明</label><div class="mc-detail-actions">
        <span class="hint">${escapeHtml(r.health.detail || '没有健康检查记录')}</span></div>
      <label>备注</label><input class="ctl" data-n="notes" value="${escapeHtml(r.notes)}"
        placeholder="为什么选它、计价出处、哪天核过">
    </div></td></tr>`;
}

export function slotProviderOptions(capability, backend, selectedProvider, cachedList) {
  const providers = cachedList || (cache.manifest?.adapters || []).filter((a) => a.capability === capability && a.backend === backend);
  return '<option value="">— 选择引擎 —</option>'
    + (selectedProvider && !providers.some((a) => a.code === selectedProvider)
      ? `<option value="${escapeHtml(selectedProvider)}" selected>${escapeHtml(selectedProvider)}（清单未登记）</option>` : '')
    + providers.map((a) => `<option value="${escapeHtml(a.code)}"${a.code === selectedProvider ? ' selected' : ''}>${escapeHtml(a.label)}</option>`).join('')
    + '<option value="__manual__">手动填写其他引擎…</option>';
}

export function slotParamDisplay(r, adapter) {
  const model = adapter?.models?.find((m) => m.code === r.model);
  const params = model?.params || [];
  const paramCount = params.length;
  const isOpen = slotOpen.has(r.id);

  if (!adapter && !r.provider) {
    return `<div class="mc-param-flow"><span class="mc-flow-hint">选择引擎后配置</span></div>`;
  }

  let pillHtml = '';
  if (paramCount > 0) {
    // 选出第 1 个最具代表性的核心参数，保证在保留的列宽内 100% 完整显示不截断
    const p = params[0];
    let valStr = '';
    if (p.default !== null && p.default !== undefined) {
      valStr = `=${p.default}`;
    } else if (p.min !== undefined && p.max !== undefined) {
      valStr = `:${p.min}—${p.max}`;
    }
    const shortName = p.name.replace(/^max_/, '').replace(/^prompt_/, '');
    const title = [p.name, p.type, valStr, p.note || ''].filter(Boolean).join(' · ');
    pillHtml = `<span class="mc-chip-mini" data-toggle-slot="${r.id}" title="${escapeHtml(title)}"><span class="k">${escapeHtml(shortName)}</span>${valStr ? `<span class="v">${escapeHtml(valStr)}</span>` : ''}</span>`;
  }

  const badgeHtml = `<button type="button" class="btn xs ghost mc-param-badge" data-toggle-slot="${r.id}" title="${paramCount > 0 ? `支持 ${paramCount} 项参数配置，点击展开全部` : '点击查看配置与参数详情'}">${paramCount > 0 ? `⚙ ${paramCount}项 ${isOpen ? '▴' : '▾'}` : `⚙ 详情 ${isOpen ? '▴' : '▾'}`}</button>`;

  if (paramCount === 0 && adapter) {
    return `<div class="mc-param-flow">
      <span class="mc-flow-hint" data-toggle-slot="${r.id}" style="cursor:pointer" title="该模型开箱即用；点击可查看密钥与环境详情">⚡ 开箱即用</span>
      ${badgeHtml}
    </div>`;
  }

  return `<div class="mc-param-flow">
    ${pillHtml}
    ${badgeHtml}
  </div>`;
}

export function slotParamBadge(r, adapter) {
  return slotParamDisplay(r, adapter);
}

export function slotRow(r, providerLists) {
  const adapter = adapterOf(cache.manifest, r.capability, r.backend, r.provider);
  const providers = providerLists[`${r.capability}|${r.backend}`];
  const providerOptions = slotProviderOptions(r.capability, r.backend, r.provider, providers);
  const videoCloud = r.capability === 'video' && r.backend === 'cloud';
  const main = `<tr class="slot"
      data-id="${r.id}" data-cap="${r.capability}" data-backend="${r.backend}">
    <td class="t-strong">${r.capability_label}</td>
    <td><span class="chip ${r.backend === 'local' ? 'plain' : 'blue'}">${r.backend_label}</span></td>
    <td><div class="mc-picks">
        <select class="ctl sm" data-f="provider" aria-label="引擎">${providerOptions}</select>
        ${slotModelPick(adapter, r.model)}
        <span class="mc-pick-extra">${slotParamDisplay(r, adapter)}</span>
      </div></td>
    <td>${statusCell(r)}</td>
    <td class="c"><input type="checkbox" data-f="enabled" ${r.enabled ? 'checked' : ''}></td>
    <td class="c"><input type="radio" name="def-${r.capability}" data-f="default"
             ${r.is_default ? 'checked' : ''} ${videoCloud ? 'disabled' : ''}
             ${videoCloud ? 'title="云端视频只能逐镜头显式选择，不能设为默认"' : ''}></td>
  </tr>`;
  return main + (slotOpen.has(r.id) ? slotDetail(r, adapter) : '');
}

function slotModelPick(adapter, model) {
  const models = adapter?.models || [];
  if (!models.length) return `<input class="ctl sm" data-f="model" value="${escapeHtml(model || '')}" placeholder="模型 ID（清单未提供，请手填）" aria-label="模型">`;
  const extra = model && !models.some((m) => m.code === model)
    ? `<option value="${escapeHtml(model)}" selected>${escapeHtml(model)}（清单未登记）</option>` : '';
  return `<select class="ctl sm" data-f="model" aria-label="模型"><option value="">— 选择模型 —</option>${extra}${models.map((m) => `<option value="${escapeHtml(m.code)}"${m.code === model ? ' selected' : ''}>${escapeHtml(m.label)}</option>`).join('')}</select>`;
}

// 启动可能比后端的短等待慢；继续检查这一台，直到连接成功或等候期结束。
// 使用实例探测接口取得真实状态，不能把 started 当成就绪。
async function waitForStartedEngine(id, out, active, waiting) {
  if (!out?.started || out.reachable) return out;
  const deadline = Date.now() + 60000;
  waiting();
  while (active() && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    if (!active()) return out;
    const probe = await api.probeEngine(id);
    if (probe.state === 'ok') return { ...out, reachable: true, message: '服务已连接，就绪。' };
  }
  return { ...out, message: '服务已请求启动，但尚未连接。请检查应用中的 HTTP API Server 是否已开启。' };
}

/* 没填 provider 的轨折成一行灰字。十行里本机只填了三行，
   剩下七行不该和已配置的等重——这是这一屏最大的噪声源。 */
export function slotCollapsed(r) {
  if (emptyOpen.has(r.id)) return slotRow(r, {});
  return `<tr class="mc-collapsed" data-empty-for="${r.id}">
    <td class="t-strong">${r.capability_label}</td>
    <td><span class="chip gray">${r.backend_label}</span></td>
    <td colspan="4">未配置 · <button class="mc-open" data-open-empty="${r.id}">去配置</button></td>
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
    const backends = [...new Set(list.map((a) => a.backend))];
    const backendChips = backends.map((b) => `<span class="chip ${b === 'local' ? 'plain' : 'blue'}">${b === 'local' ? '本地' : (b === 'cloud' ? '云端' : b)}</span>`).join(' ');
    return `<tr>
      <td class="t-strong">${escapeHtml(list[0].label)}
        <div class="hint mono">${escapeHtml(code)}</div></td>
      <td>${backendChips}</td>
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
      <div style="display:flex;align-items:center">
        <h2>能力轨</h2>
        <span class="help-tip">
          <span class="help-icon">?</span>
          <span class="tip-pop tip-right">
            <span class="tip-title">能力轨核心运行规则</span>
            <div class="tip-desc">五类能力 × 本地/云端十行选型配置；打勾行即系统默认执行轨。</div>
            <ul class="tip-list">
              <li><b>默认轨唯一来源</b>：打勾项为默认执行轨。云端视频只能逐镜头显式另选，不支持设为默认。</li>
              <li><b>绝不自动降级</b>：调用策略的建议轨与备用轨仅作参考；本地执行失败绝不会自动降级转云端，避免意外产生账单。</li>
              <li><b>健康与验证分离</b>：健康表明运行环境与密钥变量是否就绪；验证必须有真实调用或产物验收通过记录方达生产可用。</li>
              <li><b>点击单格展开</b>：点击「能力」单元格可展开查看出处清单、允许参数、密钥变量名及历史校验记录。</li>
            </ul>
          </span>
        </span>
      </div>
      <span class="right">
        <span class="chip plain" id="save-state" hidden>已保存</span>
        <button class="btn sm" id="slots-expand">${slotOpen.size ? '全部收起' : '展开全部细节'}</button>
        <button class="btn sm primary" id="save-providers">保存能力轨</button>
      </span>
    </div>
    <div class="table-wrap">
      <table class="tbl-fixed mc-slot-table">
      <colgroup>
        <col style="width:64px">
        <col style="width:72px">
        <col style="width:580px">
        <col style="width:380px">
        <col style="width:44px">
        <col style="width:44px">
      </colgroup>
      <thead><tr>
        <th>能力</th><th>后端</th>
        <th>选型（Provider / 模型）
          <span class="help-tip" data-tip="切换引擎后模型联动到新引擎第一档；也可选择「手动填写其他引擎…」直通自定义配置；右侧微标签展示首要参数规格">?</span>
        </th>
        <th>状态（健康 · 验证）
          <span class="help-tip" data-tip="健康（绿就绪/黄待查/红缺失）：反映运行环境齐备度；验证：必须具备真实调用或产物验收凭据方达生产可用">?</span>
        </th>
        <th class="c">启用</th><th class="c">默认</th>
      </tr></thead>
      <tbody>${body}</tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <div class="card-head">
      <div style="display:flex;align-items:center">
        <h2>引擎</h2>
        <span class="help-tip">
          <span class="help-icon">?</span>
          <span class="tip-pop tip-right">
            <span class="tip-title">适配器引擎清单说明</span>
            <div class="tip-desc">代码库 <code>adapters.py</code> 适配器总表，属于事实规格层（只读）。</div>
            <ul class="tip-list">
              <li><b>数据来源</b>：代码库 <code>adapters.py</code> 适配器总表，属于事实规格层（只读）。</li>
              <li><b>参数核验状态</b>：标「已核验」代表官方 schema 参数与默认值经代码验证并自动预填；「未核验」仅透传自定义参数。</li>
              <li><b>只接探测引擎</b>：如 ComfyUI 已支持端点探活与本地启动，因工作流契约未定暂无生成中心提交入口。</li>
            </ul>
          </span>
        </span>
      </div>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>引擎</th><th>位置</th><th>能力</th><th class="c">模型档</th>
        <th>参数核验</th><th>连接方式（变量名）</th><th>最近检查</th></tr></thead>
      <tbody>${engineRows}${unintegrated}</tbody>
    </table></div>
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
          现在通不通看引擎实例那枚徽章（每次现查）${escapeHtml((data && data.note) || '')}</p>
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
  // ---- 即时开窗优化（2026-11-14）----
  // 支持两种调用姿势：
  //   1. openProbeResults(null, null)  — 加载态：瞬间弹窗 + 骨架流光屏
  //   2. openProbeResults(payload, cloudRows)  — 原有的数据态
  // 调用方可以拿返回的 handle 在数据就绪后 handle.update(payload, cloudRows)，
  // 弹窗就地平滑替换为真实内容，不闪不抖。
  const loading = payload === null || payload === undefined;

  const inst = payload || {};
  const items = inst.items || [];
  const service = new Set(inst.service_engines || []);
  const cloud = Array.isArray(cloudRows) ? cloudRows : [];
  // `cloud` 为空有两种完全不同的原因，不许用同一句话糊过去：
  //   · 这个后端根本不返回 `cloud` 键（服务没重启到带 cloud_probe 的那一版）——
  //     那是部署问题，必须说出来，否则人以为"云端都好好的、只是没显示"。
  //     真出过一次：前端与磁盘源码都含云端段，跑着的进程还是旧代码，
  //     结果窗安安静静少了一整段，底部却照旧宣称"真去网关发了一次 GET"。
  //   · 返回了空数组——云端一家都没登记，那是正常的"没有可查对象"。
  const cloudMissing = cloudRows === undefined || cloudRows === null || !Array.isArray(cloudRows);

  // 这一整块派生逻辑必须在「启动服务之后重画」时能再跑一遍，所以包成函数：
  // 入参是新的 payload，出参是这一屏要的几段 HTML。
  function derive(inst2, cloudRows2) {
    const items = (inst2 || {}).items || [];
    const service = new Set((inst2 || {}).service_engines || []);
    const cloud = Array.isArray(cloudRows2) ? cloudRows2 : [];
    const cloudMissing = cloudRows2 === undefined || cloudRows2 === null
      || !Array.isArray(cloudRows2);
  const rank = (it) => !it.enabled || it.probe_unsupported ? 2 : it.health_state === 'ok' ? 1 : 0;
  const details = (text) => `<details class="probe-details"><summary>查看详情</summary><div>${escapeHtml(text || '暂无详细信息')}</div></details>`;
  const hasActions = items.some((it) => !it.enabled || it.launch_ready);
  const rowsNow = [...items].sort((a, b) => rank(a) - rank(b)).map((it) => {
    const ok = it.health_state === 'ok';
    const skipped = !it.enabled || it.probe_unsupported;
    const label = !it.enabled ? '已停用' : it.probe_unsupported ? '探测未实现' : ok ? '连接成功' : it.health_state === 'missing' ? '连接失败' : '暂无法检查';
    const tone = skipped ? 'gray' : ok ? 'green' : 'red';
    const reason = /Connection refused/i.test(it.health_detail || '') ? '服务未启动或端口未监听' : ok ? '端点已响应，模型可用性需另行验证' : '请查看详情确认原因';
    return `<tr><td><strong>${escapeHtml(it.display_name || it.engine_label || it.engine_code)}</strong>
      <div class="hint mono probe-endpoint">${escapeHtml(it.endpoint || '未配置端点')}</div></td>
      <td><div class="probe-result-stack"><span class="chip ${tone}">${label}</span><div class="hint">${skipped ? '本轮未检查' : reason}</div>
      ${details([it.health_detail, it.engine_code, it.connection_type, it.note].filter(Boolean).join(' · '))}</div></td>
      ${hasActions ? `<td>${!it.enabled ? `<button class="btn xs" data-probe-enable="${it.id}">启用</button>` : it.launch_ready ? `<button class="btn xs" data-act-launch="${it.id}">启动服务</button>` : '<span class="hint">—</span>'}</td>` : ''}</tr>`;
  }).join('');

  const queried = items.filter((it) => it.enabled && !it.probe_unsupported);
  const okN = queried.filter((it) => it.health_state === 'ok').length;
  const badN = queried.filter((it) => it.health_state === 'missing').length;
  const skipped = items.length - queried.length;

  // 一句结论：人想知道的首先就是"通了没有"，不该让他去数徽章。
  // 说明文字只留一句。原来这里连弹窗底部的注，把「连上≠可用」讲了三遍
  // （2026-11-12 用户反馈「注释、提醒字数太多，简略一些」）。结论本身一句就够，
  // 剩下的细节放进 title，想追的人 hover 得到。
  const unknownN = queried.length - okN - badN;
  const cloudOk = cloud.filter((row) => row.state === 'ok').length;
  const cloudSummary = cloudMissing ? '云端未检查' : cloud.length
    ? `云端 ${cloud.length} 家 · ${cloudOk} 家鉴权通过` : '云端尚未登记';
  const verdict = `<div class="probe-summary"><strong>本地 ${queried.length} 项检查 · ${okN} 项通过 · ${badN + unknownN} 项异常</strong><strong>${cloudSummary}</strong>
    <p class="hint">${skipped ? `另有 ${skipped} 项未检查。` : ''}连接成功仅表示端点响应，不代表模型可用。</p></div>`;
  const body = items.length ? `<h3 class="probe-section">本地引擎</h3><div class="table-wrap"><table class="tbl-fixed probe-table ${hasActions ? '' : 'probe-table-no-actions'}"><thead><tr><th>引擎 / 端点</th><th>检查结果</th>${hasActions ? '<th>操作</th>' : ''}</tr></thead><tbody>${rowsNow}</tbody></table></div>` : '<p class="note">尚未登记引擎实例，请进入引擎管理添加端点。</p>';

  // 云端一段：逐家供应商一行，结论来自 cloud_probe（去供应商网关问一次）。
  // 五档各说各的，chip 用的就是"要不要动、动什么"的词，不互相冒充。
  const CLOUD_CHIP = {
    ok: ['green', '端点可达 · 鉴权通过'],
    auth_rejected: ['red', '路通、钥匙被拒'],
    unreachable: ['red', '连不上'],
    no_key: ['amber', '没配密钥'],
    no_endpoint: ['amber', '无可探端点'],
  };
  const cloudRowsNow = [...cloud].sort((a, b) => Number(a.state === 'ok') - Number(b.state === 'ok')).map((c) => {
    const [tone, word] = CLOUD_CHIP[c.state] || ['amber', c.state || '未知'];
    let endpoint = c.key_env || '未配置端点';
    try { endpoint = new URL(c.url).origin; } catch { /* 无有效地址时显示配置项 */ }
    return `<tr><td><strong>${escapeHtml(c.label || c.provider)}</strong><div class="hint mono probe-endpoint">${escapeHtml(endpoint)}</div></td>
      <td><div class="probe-result-stack"><span class="chip ${tone}">${escapeHtml(word)}</span><div class="hint">${c.state === 'ok' ? '未验证实际生成' : '请检查连接或配置'}</div>${details([c.provider, c.capability, c.url, c.detail].filter(Boolean).join(' · '))}</div></td>
      </tr>`;
  }).join('');

  const cloudVerdict = cloudMissing
    ? '<p class="note"><span class="chip red">没查</span> 后端没返回 <code>cloud</code> 段，请检查服务版本并重启。</p>'
    : '';
  const cloudBody = cloud.length
    ? `<div class="table-wrap"><table class="tbl-fixed probe-table probe-table-no-actions"><thead><tr>
        <th>引擎 / 端点</th><th>检查结果</th>
      </tr></thead><tbody>${cloudRowsNow}</tbody></table></div>`
    : '';
    return { verdict, body, cloudVerdict, cloudBody, cloud };
  }

  const host = document.createElement('div');
  host.className = 'modal probe-results';
  host.innerHTML = `<div class="modal-box wide">
      <div class="modal-head neutral">引擎连接检查</div>
      <div class="modal-body" data-probe-body></div>
      <div class="modal-actions"><span role="status" data-probe-status></span><button class="btn" data-manage-engines>进入引擎管理 →</button><button class="btn" data-recheck>重新检查</button><button class="btn" data-close-probe-res>关闭</button></div>
    </div>`;
  document.body.appendChild(host);
  const close = () => host.remove();

  // ---- 骨架加载态（2026-11-14）----
  // 加载态下先画流光骨架屏，让用户瞬间看到弹窗在干活；
  // 数据到了再用 paintWith 一次性替换为真实内容。
  const skeletonHTML = `<div class="probe-loading">
    <p style="margin:0 0 16px;color:var(--fg2)">正在探测引擎连接与云端网关…</p>
    <div class="skeleton-shimmer" style="height:14px;border-radius:6px;margin-bottom:12px;width:70%"></div>
    <div class="skeleton-shimmer" style="height:44px;border-radius:8px;margin-bottom:10px"></div>
    <div class="skeleton-shimmer" style="height:44px;border-radius:8px;margin-bottom:10px"></div>
    <div class="skeleton-shimmer" style="height:44px;border-radius:8px;margin-bottom:10px"></div>
    <div class="skeleton-shimmer" style="height:14px;border-radius:6px;margin-top:18px;width:45%"></div>
  </div>`;

  // 主体内容抽成函数：启动服务之后要**就地重画这一屏**，不是关掉弹窗。
  // 原来的实现是 alertNotify + 派 app:refresh + close()——close() 直接把节点删了，
  // 而 app:refresh 又重绘整页，人根本看不到这一轮的结果（2026-11-12 用户反馈
  // 「点了启动服务弹窗就直接消失了，不能在原来的弹窗刷新吗」）。
  const footerNote = '';
  // 屏内结论行（启动结果写在这里，重画后仍然保留）
  let noticeLine = '';
  const bodyEl = host.querySelector('[data-probe-body]');
  // paintWith 用**新数据**重画这一屏；不传就用最初那次的数据。
  const paintWith = (inst2, cloudRows2) => {
    const d = derive(inst2 === undefined ? payload : inst2,
                     cloudRows2 === undefined ? cloudRows : cloudRows2);
    bodyEl.innerHTML = d.verdict + d.body
      + (d.cloud.length ? '<h3 class="probe-section">云端引擎</h3>' : '')
      + d.cloudVerdict + d.cloudBody + footerNote
      + (noticeLine ? `<p class="note" data-launch-notice style="margin:10px 0 0">${noticeLine}</p>` : '');
    bodyEl.querySelectorAll('[data-probe-enable]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        btn.textContent = '启用中…';
        const status = host.querySelector('[data-probe-status]');
        let enabled = false;
        try {
          await api.patchEngine(btn.dataset.probeEnable, { enabled: true });
          enabled = true;
          payload = await api.engines();
          if (!host.isConnected) return;
          paint();
          status.textContent = '实例已启用，连接状态已更新';
        } catch (err) {
          status.textContent = enabled
            ? `实例已启用，但状态刷新失败，请重新检查：${err.message}`
            : `启用失败：${err.message}`;
        } finally {
          btn.disabled = false;
          btn.textContent = '启用';
        }
      });
    });
    bindLaunch();
  };
  const paint = () => paintWith(payload, cloudRows);
  // 首次绘制：加载态画骨架，数据态直接画内容
  if (loading) {
    bodyEl.innerHTML = skeletonHTML;
    host.querySelector('[data-probe-status]').textContent = '正在探测…';
    host.querySelector('[data-recheck]').disabled = true;
  } else {
    paint();
  }
  host.addEventListener('click', (e) => {
    if (e.target === host || e.target.closest('[data-close-probe-res]')) close();
  });
  host.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });

  host.querySelector('[data-manage-engines]').addEventListener('click', () => {
    close(); setSub('models', 1);
    window.dispatchEvent(new CustomEvent('app:go', { detail: 'models' }));
  });
  host.querySelector('[data-recheck]').addEventListener('click', async (event) => {
    const btn = event.currentTarget;
    const status = host.querySelector('[data-probe-status]');
    btn.disabled = true; status.textContent = '正在检查…';
    try {
      // 并发化（2026-11-14）：两条出网请求同时发，总耗时 = max(T1, T2) 而非 T1 + T2
      const [result, fresh] = await Promise.all([
        api.probeModels(true),
        api.engines(),
      ]);
      payload = fresh; cloudRows = result?.cloud;
      if (host.isConnected) { paint(); status.textContent = '检查已更新'; }
    } catch (err) { status.textContent = `检查失败：${err.message}`; }
    finally { btn.disabled = false; }
  });

  // 启动按钮的点击要绑在**这个弹窗自己**身上（2026-11-12 修）。
  // 它由这里 append 到 body、不在 #view-root 里，所以 mountEngineInstances
  // 里那次 root.querySelectorAll 永远选不到它——实测：按钮画得出来、
  // 点下去没有任何反应（不报错、也不发请求），比不画更糟。
  //
  // **启动完就地重画这一屏，不关弹窗**。原来这里是
  // `alertNotify(...) + 派 app:refresh + close()`：close() 直接删节点，
  // 而 app:refresh 又重绘整页——人会看到弹窗凭空消失，且这一轮结果
  // 一个字都没读到（用户原话：「弹窗就直接消失了，不能在原来的弹窗刷新吗」）。
  // 现在的顺序是：拉一次最新实例列表 → 用新数据重画 → 结论写在屏内一行。
  async function bindLaunch() {
    for (const btn of host.querySelectorAll('[data-act-launch]')) {
      btn.addEventListener('click', async () => {
        const id = btn.dataset.actLaunch;
        const label = btn.textContent;
        btn.disabled = true;
        btn.textContent = '启动中…';   // 后端最多等 6 秒，必须有可见反馈
        let out = null;
        let err = null;
        try {
          out = await api.launchEngine(id);
          out = await waitForStartedEngine(id, out, () => host.isConnected, () => {
            btn.textContent = '等待连接…';
            host.querySelector('[data-probe-status]').textContent = '应用正在启动，连接成功后自动更新状态…';
          });
        } catch (e) {
          err = e;
        }
        if (!host.isConnected) return;
        btn.disabled = false;
        btn.textContent = label;

        // 屏内结论：把后端那句话原样写到这一屏底部，重画之后仍然保留。
        noticeLine = (err
          ? '启动失败：' + (err && err.message ? err.message : String(err))
          : (out && out.message) || '已请求启动。')
          .replace(/&/g, '&amp;').replace(/</g, '&lt;');

        // 重画要的是**全部实例**的新状态，而 launch 只回这一台，所以再取一次列表。
        try {
          const fresh = await api.engines();
          if (!host.isConnected) return;
          payload = fresh;
          paintWith(fresh);
          host.querySelector('[data-probe-status]').textContent = out?.reachable ? '服务已连接，就绪。' : '';
          window.dispatchEvent(new CustomEvent('app:refresh'));
        } catch {
          paint();     // 取不到新数据就至少把结论行画出来
        }
      });
    }
  }

  // 返回控制句柄：调用方拿到后可以用 update() 从加载态平滑切换到真实内容，
  // 也可以用 isAlive() 判断弹窗是否还在 DOM 里（用户可能提前手动关掉）。
  return {
    /** 用真实数据替换骨架屏（加载态 → 数据态）。 */
    update(newPayload, newCloudRows) {
      payload = newPayload;
      cloudRows = newCloudRows;
      if (!host.isConnected) return;
      paintWith(newPayload, newCloudRows);
      host.querySelector('[data-probe-status]').textContent = '';
      host.querySelector('[data-recheck]').disabled = false;
    },
    /** 弹窗是否仍挂在 DOM 上。 */
    isAlive() { return host.isConnected; },
  };
}

/** 两条探测之间隔了多久。数字都是秒；负值（同一秒内）当 0。 */
export function fmtGap(seconds) {
  const n = Math.max(0, Number(seconds) || 0);
  if (n < 60) return `${n} 秒`;
  if (n < 3600) return `${Math.floor(n / 60)} 分钟`;
  if (n < 86400) return `${Math.floor(n / 3600)} 小时`;
  return `${Math.floor(n / 86400)} 天`;
}

let addInstOpen = false;

export function engineInstStatusCell(it) {
  const badge = it.enabled
    ? (it.probe_unsupported
        ? '<span class="chip amber">探测未实现</span>'
        : healthChip({ state: it.health_state, detail: it.health_detail }))
    : '<span class="chip gray">已停用</span>';

  // 避免模型名称重复打印：如果 health_detail 已经包含 engine_model，则不单列模型行
  let modelLine = '';
  if (it.engine_model && (!it.health_detail || !it.health_detail.includes(it.engine_model))) {
    modelLine = `<div class="inst-model-chip" title="当前加载模型：${escapeHtml(it.engine_model)}"><span class="k">模型</span> <code class="mono">${escapeHtml(it.engine_model)}</code></div>`;
  }

  let detailLine = '';
  if (it.health_detail) {
    const isErr = it.health_state === 'missing' || it.health_state === 'down' || it.health_detail.includes('Errno') || it.health_detail.includes('连不上');
    if (isErr) {
      detailLine = `<div class="inst-err-box" title="${escapeHtml(it.health_detail)}"><span class="warn-dot">⚠</span> ${escapeHtml(it.health_detail)}</div>`;
    } else {
      detailLine = `<div class="hint t-wrap">${escapeHtml(it.health_detail)}</div>`;
    }
  }

  return `<div class="inst-status-wrap">
    <div class="inst-status-head">${badge}</div>
    ${modelLine}
    ${detailLine}
    ${probeHistoryLine(it)}
  </div>`;
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
      <td>
        <div class="inst-endpoint-cell">
          <code class="mono endpoint-text">${escapeHtml(it.endpoint)}</code>
          <button type="button" class="btn-copy-mini" data-copy-endpoint="${escapeHtml(it.endpoint)}" title="复制端点地址">
            <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8">
              <rect x="5" y="5" width="8" height="8" rx="1.5"></rect>
              <path d="M3 11V3a1 1 0 0 1 1-1h8"></path>
            </svg>
          </button>
          ${it.endpoint.startsWith('http://127.0.0.1') || it.endpoint.startsWith('http://localhost')
            ? `<a href="${escapeHtml(it.endpoint)}" target="_blank" rel="noopener noreferrer" class="link-open-mini" title="在浏览器新标签页测试打开此本地端点">↗</a>` : ''}
        </div>
      </td>
      <td><span class="hint">${escapeHtml(it.connection_type)}</span> ${secret}</td>
      <td>${engineInstStatusCell(it)}</td>
      <td class="t-dim">${escapeHtml(it.note || '')}</td>
      <td class="c inst-actions">
        ${it.health_state === 'ok' || !it.launch_ready ? ''
          : `<button class="btn xs btn-launch-engine" data-act-launch="${it.id}"
                     title="${escapeHtml(it.launch_note || '')}">启动服务</button>`}
        <button class="btn xs" data-act-probe="${it.id}">探测</button>
        <button class="btn xs ghost" data-act-probes="${it.id}">看历史</button>
        <button class="btn xs ghost" data-act-toggle="${it.id}" data-to="${it.enabled ? 0 : 1}">${
          it.enabled ? '停用' : '启用'}</button>
        <button class="btn xs danger ghost" data-act-uninst="${it.id}">撤实例</button>
      </td>
    </tr>`;
  }).join('');

  const empty = items.length ? '' : `<tr><td colspan="6"><span class="hint">
    还没有启用中的引擎实例。Draw Things、Ollama、ComfyUI 这类"跑成一台常驻服务"的引擎，
    要先在这里写一条端点（例如 <code>http://127.0.0.1:7860</code>），
    模型仓库那边才查得到它的健康状况。</span></td></tr>`;

  // 无实例时默认展开登记面板，已有实例时收起保持整洁
  if (items.length === 0) addInstOpen = true;

  return `
  <div class="card" id="card-engine-inst">
    <div class="card-head">
      <div style="display:flex;align-items:center">
        <h2>引擎实例</h2>
        <span class="help-tip">
          <span class="help-icon">?</span>
          <span class="tip-pop tip-right">
            <span class="tip-title">引擎实例与端点管理规范</span>
            <div class="tip-desc">记录常驻服务运行的主机与端口（Draw Things、Ollama、ComfyUI 等）。</div>
            <ul class="tip-list">
              <li><b>实例定义</b>：记录常驻服务运行的主机与端口。健康状态每次现查，不落库。</li>
              <li><b>端点不可修改</b>：端点是实例历史作业绑定的核心业务主键；换机器请新建实例再停用旧实例，确保审计留痕准确。</li>
              <li><b>连接协议支持</b>：当前仅开放 <code>local_http</code> 协议探测与调用；gRPC 与 CLI 分支建设中。</li>
              <li><b>现查探活与启动</b>：服务掉线时点击「启动服务」可拉起本地进程并等待连接就绪，点击「探测」可实时探活。</li>
            </ul>
          </span>
        </span>
      </div>
      <span class="right">
        <button class="btn sm ghost" id="btn-toggle-add-inst">${addInstOpen ? '✕ 收起登记面板' : '＋ 登记新实例'}</button>
      </span>
    </div>
    <div class="table-wrap"><table class="tbl-fixed mc-engine-inst-table">
      <colgroup>
        <col style="width:160px">
        <col style="width:240px">
        <col style="width:130px">
        <col style="width:290px">
        <col style="width:160px">
        <col style="width:220px">
      </colgroup>
      <thead><tr><th>实例</th><th>端点</th><th>连接方式（密钥变量）</th>
        <th>状态</th><th>备注</th><th class="c" style="text-align:right">操作</th></tr></thead>
      <tbody>${rows}${empty}</tbody>
    </table></div>

    <div class="mc-add-inst-panel" id="panel-add-inst" style="${addInstOpen ? '' : 'display:none'}">
      <div class="mc-add-inst-head">
        <div class="mc-add-inst-title">
          <span class="badge-dot"></span>
          <span>登记新引擎实例</span>
          <span class="hint">配置常驻服务或自建集群端点</span>
        </div>
        <button type="button" class="btn xs ghost" id="btn-close-add-inst" title="收起登记面板">✕ 收起</button>
      </div>
      <div class="mc-add-inst-grid">
        <div class="field-item">
          <label>目标引擎 <span class="help-tip" data-tip="清单中登记过的服务型引擎优先排列，支持 HTTP 真实握手探活">?</span></label>
          <select class="ctl sm" id="eng-code">${codeOpts || '<option value="">清单里还没有引擎</option>'}</select>
        </div>
        <div class="field-item">
          <label>服务端点 URL <span class="help-tip" data-tip="标准格式：http://主机IP:端口号（例如 http://127.0.0.1:7860），不带额外路径与参数">?</span></label>
          <input class="ctl sm mono" id="eng-endpoint" maxlength="200" placeholder="http://127.0.0.1:7860">
        </div>
        <div class="field-item">
          <label>实例显示名 <span class="help-tip" data-tip="用于多机协作部署时快速识别，留空默认使用引擎英文标识">?</span></label>
          <input class="ctl sm" id="eng-name" maxlength="80" placeholder="例如：M3 Max 本机服务">
        </div>
        <div class="field-item">
          <label>共享密钥变量名 <span class="help-tip" data-tip="操作系统进程环境变量名（如 COMFYUI_SECRET）。密钥值永不存库，仅从环境读取；本机引擎留空即可">?</span></label>
          <input class="ctl sm mono" id="eng-secret" maxlength="60" placeholder="例如：COMFYUI_SECRET">
        </div>
        <div class="field-item span-full">
          <label>备注说明 <span class="help-tip" data-tip="记录此实例的用途、宿主机硬件型号或换机历史归档原因">?</span></label>
          <div class="field-action-row">
            <input class="ctl sm" id="eng-note" maxlength="300" placeholder="备注：这台是谁的、为什么换机、部署在哪台宿主机">
            <button class="btn sm primary" id="eng-add">＋ 确认登记</button>
          </div>
        </div>
      </div>
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
  /* 密钥「测试连接」（2026-11-11）：真出网打一次供应商网关，回答"这把到底能不能用"。
     与「探测」不是一回事：那个问的是引擎端点通不通，这个问的是**密钥被不被接受**
     （401 钥匙问题／402 欠费／429 限流 各有各的处理）。
     用 root 委托而不是逐按钮绑：这一块会随展开/收起重画，逐绑会漏新的那颗。 */
  if (!root.__secretTestBound) {
    root.__secretTestBound = true;
    root.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-secret-test]');
      if (!btn || btn.disabled) return;
      const provider = btn.dataset.secretTest;
      btn.disabled = true;
      const old = btn.textContent;
      btn.textContent = '测试中…';
      try {
        const out = await api.testSecret(provider);
        // 结论按 state 说话，不写"成功/失败"两个字：no_key 与 auth 要人做的事不同
        const msg = {
          ok: `连上了：${out.label} 接受了这把密钥`,
          no_key: `没配密钥（${out.key_env || '变量未定'}）：没有发出请求`,
          no_endpoint: `平台没登记这一家的端点，不编造探测结论`,
        }[out.state] || `被拒绝：${out.detail}`;
        alertNotify(msg);
      } catch (err) {
        alertNotify(`测试没跑成：${err.message}`, 'err');
      } finally {
        btn.disabled = false;
        btn.textContent = old;
      }
    });
  }

  root.querySelectorAll('[data-open-empty]').forEach((b) => b.addEventListener('click', (e) => {
    e.stopPropagation();
    emptyOpen.add(Number(b.dataset.openEmpty));
    slotOpen.add(Number(b.dataset.openEmpty));   // 去配置的人下一步就是要填细节
    repaint();
  }));
  root.querySelectorAll('[data-toggle-slot]').forEach((btn) => btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const id = Number(btn.dataset.toggleSlot);
    if (slotOpen.has(id)) slotOpen.delete(id);
    else slotOpen.add(id);
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
    const refresh = (providerChanged = false) => {
      const provider = tr.querySelector('[data-f="provider"]').value;
      const adapter = adapterOf(cache.manifest, tr.dataset.cap, tr.dataset.backend, provider);
      if (providerChanged) {
        const previous = tr.querySelector('[data-f="model"]');
        // 夜间控件有自绘外壳；一并换掉，避免旧按钮仍显示上一家的模型。
        (previous.closest('.fv-pick') || previous).outerHTML = slotModelPick(adapter, adapter?.models?.[0]?.code || '');
      }
      const model = tr.querySelector('[data-f="model"]').value;
      const extra = tr.querySelector('.mc-pick-extra');
      if (extra) {
        extra.innerHTML = slotParamDisplay({ id: Number(tr.dataset.id), model }, adapter);
        extra.querySelectorAll('[data-toggle-slot]').forEach((el) => el.addEventListener('click', (e) => {
          e.stopPropagation();
          const id = Number(tr.dataset.id);
          if (slotOpen.has(id)) slotOpen.delete(id);
          else slotOpen.add(id);
          repaint();
        }));
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
    tr.addEventListener('change', (e) => {
      if (e.target.matches('[data-f="provider"], [data-f="model"]')) {
        if (e.target.dataset.f === 'provider' && e.target.value === '__manual__') {
          const previous = e.target;
          (previous.closest('.fv-pick') || previous).outerHTML = `
            <div class="mc-manual-provider">
              <input class="ctl sm" data-f="provider" aria-label="引擎" placeholder="自定义 provider 标识">
              <button type="button" class="btn-revert-pick" data-act-revert-provider title="切回下拉选择已有引擎">
                <svg class="revert-icon" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M2.5 7.5A5.5 5.5 0 1 1 8 13.5c-2.4 0-4.5-1.5-5.3-3.8"></path>
                  <polyline points="2.5 3.5 2.5 7.5 6.5 7.5"></polyline>
                </svg>
                <span>切回选择</span>
              </button>
            </div>`;
          tr.querySelector('input[data-f="provider"]').focus();
        }
        refresh(e.target.dataset.f === 'provider');
      }
    });
    tr.addEventListener('click', (e) => {
      const revertBtn = e.target.closest('[data-act-revert-provider]');
      if (revertBtn) {
        const wrap = tr.querySelector('.mc-manual-provider') || revertBtn.closest('.mc-manual-provider');
        if (wrap) {
          const cap = tr.dataset.cap;
          const backend = tr.dataset.backend;
          const opts = slotProviderOptions(cap, backend, '');
          wrap.outerHTML = `<select class="ctl sm artifact-reveal" data-f="provider" aria-label="引擎">${opts}</select>`;
          const newSelect = tr.querySelector('select[data-f="provider"]');
          if (newSelect && document.documentElement.dataset.theme === 'night') {
            enhanceSelect(newSelect);
          }
          refresh(true);
        }
      }
    });
    tr.addEventListener('input', (e) => {
      if (e.target.matches('input[data-f="model"], input[data-f="provider"]')) {
        refresh(e.target.dataset.f === 'provider');
      }
    });
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
          ? (detail.querySelector('[data-f="key_env"]')?.value ?? saved.key_env_custom ?? saved.key_env ?? '')
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

  // 验证弹窗控制器抽到了 shared.js（模型仓库详情也要用它）。引擎页提交后
  // 回读最新十行（顺带重画健康/验证列），把这一槽的新摘要接回弹窗、留着不关。
  const val = setupValidationModal(root, {
    afterSubmit: async (target) => {
      const fresh = await reloadRows();
      const slot = fresh.find((r) => r.id === target.id) || target;
      return { validation: slot.validation, keepOpen: true };
    },
  });

  // 重画验证列后要重绑，所以用 onclick 赋值而不是 addEventListener（不叠监听器）
  function bindValidationActions(scope) {
    scope.querySelectorAll('[data-env-check]').forEach((btn) => {
      btn.onclick = async () => {
        const slot = rowsCache.find((r) => String(r.id) === btn.dataset.envCheck);
        if (!slot) return;
        const ok = await run('环境检查', () => api.validateProvider({
          capability: slot.capability, backend: slot.backend,
          provider: slot.provider, kind: 'env_check',
        }), { btn });
        if (ok) await reloadRows().catch(() => {});
      };
    });
    scope.querySelectorAll('[data-val-history]').forEach((btn) => {
      btn.onclick = () => {
        const slot = rowsCache.find((r) => String(r.id) === btn.dataset.valHistory);
        if (slot) val.open(slot);
      };
    });
  }

  bindValidationActions(root);
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

  const toggleBtn = root.querySelector('#btn-toggle-add-inst');
  const panel = root.querySelector('#panel-add-inst');
  const closeBtn = root.querySelector('#btn-close-add-inst');

  const updatePanelState = (open) => {
    addInstOpen = open;
    if (panel) panel.style.display = open ? 'block' : 'none';
    if (toggleBtn) toggleBtn.textContent = open ? '✕ 收起登记面板' : '＋ 登记新实例';
  };

  toggleBtn?.addEventListener('click', () => updatePanelState(!addInstOpen));
  closeBtn?.addEventListener('click', () => updatePanelState(false));

  const addBtn = root.querySelector('#eng-add');
  addBtn?.addEventListener('click', async () => {
    const body = {
      engine_code: root.querySelector('#eng-code').value,
      endpoint: root.querySelector('#eng-endpoint').value.trim(),
      display_name: root.querySelector('#eng-name').value.trim(),
      shared_secret_env: root.querySelector('#eng-secret').value.trim(),
      note: root.querySelector('#eng-note').value.trim(),
    };
    if (!body.engine_code) { alertNotify('先选一个引擎：清单里现在没有可选的一档。', 'err'); return; }
    if (!body.endpoint) { alertNotify('端点不能空——这一行的全部意义就是「这一档引擎在哪个地址」。', 'err'); return; }
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

  // 启动本机引擎（2026-11-11 加）。只在服务没答话、且这一档有内置配方时才有这颗按钮。
  //
  // 三件事必须分开说清楚，否则这一屏最容易出的谎就是「点了没反应」或「点了就好了」：
  //   1. started 与 reachable 分开——命令发出去了不等于端口答话了；
  //      Draw Things 的 API Server 是应用内开关，应用起来照样连不上。
  //   2. 已经在跑时不重复启动（后端会拦），界面照实说「本来就在跑」。
  //   3. 失败原因原样透出：没装 / 没配方 / 起了没答话，是三种不同的下一步。
  root.querySelectorAll('[data-act-launch]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.actLaunch;
      btn.disabled = true;
      const label = btn.textContent;
      btn.textContent = '启动中…';   // 后端最多等 6 秒，必须有可见反馈
      let out = null;
      const ok = await run('启动服务', async () => {
        out = await api.launchEngine(id);
        out = await waitForStartedEngine(id, out, () => root.isConnected && btn.isConnected, () => {
          btn.textContent = '等待连接…';
        });
      });
      if (!root.isConnected || !btn.isConnected) return;
      btn.textContent = label;
      btn.disabled = false;
      if (!ok || !out) { repaint(); return; }
      // 无论成败都把后端那句话原样给出去：它已经分清了「起来了」与「能用了」。
      alertNotify(out.message, out.reachable ? 'info' : 'warn');
      repaint();
    });
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
      const ok = await run(on ? '启用' : '停用',
        () => api.patchEngine(btn.dataset.actToggle, { enabled: on }), { btn });
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

  root.querySelectorAll('[data-copy-endpoint]').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const text = btn.dataset.copyEndpoint;
      try {
        await navigator.clipboard.writeText(text);
        alertNotify(`已复制端点：${text}`, 'info');
      } catch {
        alertNotify('复制失败，请手动选取复制', 'warn');
      }
    });
  });
}
