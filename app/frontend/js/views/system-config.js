import { api } from '../api.js';
import { store } from '../store.js';
import { bindSubChips, escapeHtml, run, subChipRow, bindModalDismiss } from '../util.js';
import { getSub, setSub } from '../navstate.js';
import usersPanel from './system-users.js';

/**
 * 系统设置：两个二级页。
 *   0 模型与密钥 —— 五类能力 × 本地/云端
 *   1 用户与角色 —— 另一份文件（system-users.js），这里只做转发
 *
 * v0.66（W06）起这一页多三样东西，都来自服务端的 adapter 清单：
 *   - 允许参数：只有清单里登记过的档位才显示，清单里没有就明说"不猜"；
 *   - 健康状态：打开页面时现查（运行时目录在不在、密钥变量有没有值）；
 *   - 密钥变量名与备注：存的是变量名，不是值，值永远只从进程环境读。
 * 云端视频的"默认"单选框被禁用：它只能被镜头逐条显式选中。
 */
const SUBS = ['模型与密钥', '用户与角色'];

// 健康四档的显示口径。unknown 用琥珀色而不是红色——"平台查不了"不等于"坏了"。
const HEALTH = {
  ok: ['green', '就绪'],
  missing: ['red', '缺东西'],
  unknown: ['amber', '未检查'],
  unconfigured: ['gray', '未配置'],
  unregistered: ['red', '未登记'],
};

// P0：验证出处与健康是两件事。健康只回答"环境齐不齐"，验证回答"凭什么认为真能产出"。
// 这枚标签按可信度从高到低取第一个 ok：目标机 > 产物验收 > 真实调用 > 模拟 > 环境检查。
// 产物验收比真实调用多一道人眼，所以排更高；环境检查通过永远不能冒充真实调用——
// 那正是外部评审点名的口径事故。
const VAL_BADGE = {
  target_machine: ['green', '目标机已验证'],
  artifact_review: ['green', '产物验收通过'],
  real_call: ['green', '真实调用已验证'],
  mock: ['amber', '仅模拟验证'],
  env_check: ['blue', '仅环境检查'],
  never: ['gray', '未验证'],
};
const VAL_KIND_LABEL = {
  env_check: '环境检查', mock: '模拟', real_call: '真实调用',
  target_machine: '目标机基准', artifact_review: '产物验收',
};
const VAL_STATE = {
  ok: ['green', '通过'], missing: ['red', '缺东西'], unknown: ['amber', '未检查'],
  unconfigured: ['gray', '未配置'], unregistered: ['red', '未登记'],
  failed: ['red', '失败'], never: ['gray', '没有记录'],
};

function badgeOf(v) {
  for (const kind of ['target_machine', 'artifact_review', 'real_call', 'mock', 'env_check']) {
    if ((v?.[kind] || {}).state === 'ok') return kind;
  }
  return 'never';
}

function stateChip(state) {
  const [tone, label] = VAL_STATE[state] || ['gray', state || '未知'];
  return `<span class="chip ${tone}">${escapeHtml(label)}</span>`;
}

const norm = (s) => String(s || '').trim().toLowerCase();

// render 把清单留在模块里，mount 里改 provider/model 时就用同一份，
// 不为了一个下拉建议再发一次请求。
let manifestCache = null;
// 已保存的配置行也留一份：环境检查与验证记录都按"服务端已存的那一轨"走，
// 不拿输入框里还没保存的值去登记，否则验的是 A、记到了 B 头上。
let rowsCache = [];

function adapterOf(manifest, cap, backend, provider) {
  if (!manifest || !provider) return null;
  return (manifest.adapters || []).find((a) => a.capability === cap
    && a.backend === backend
    && (norm(a.code) === norm(provider)
      || (a.aliases || []).some((x) => norm(x) === norm(provider))));
}

function paramChips(model) {
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

function paramsCell(adapter, model, provider) {
  if (!provider) return '<span class="hint">选 provider 后按清单显示</span>';
  if (!adapter) return '<span class="hint">未登记这个 provider；参数不猜，运行方式也不猜</span>';
  if (!adapter.params_known) {
    return '<span class="hint">参数未核验，平台不预填（到 W10 接云端 adapter 时按官方 schema 补齐）</span>';
  }
  if (!adapter.models.length) return '<span class="hint">清单里没有模型档</span>';
  if (!model) return '<span class="hint">从「模型」列选一档，或按清单输入</span>';
  const spec = adapter.models.find((m) => m.code === model);
  if (!spec) return '<span class="hint">这个模型 ID 不在清单里；参数不猜</span>';
  const chips = paramChips(spec);
  return chips
    ? `<div class="param-chips">${chips}</div>`
    : '<span class="hint">这一档没有可调参数</span>';
}

function noteText(adapter, provider) {
  if (!provider) return '清单：选 provider 后显示适配器与出处';
  if (!adapter) return '清单：没有登记这个 provider，参数与运行方式不会被猜';
  return `清单：${escapeHtml(adapter.label)} · ${escapeHtml(adapter.source)}`;
}

function healthCell(health) {
  const [tone, label] = HEALTH[health?.state] || ['gray', '未知'];
  const detail = health?.detail || '';
  return `<span class="chip ${tone}" title="${escapeHtml(detail)}">${label}</span>
          <span class="hint">${escapeHtml(detail)}</span>`;
}

// 验证列：一枚出处标签 + 生产可用判定 + 两个常驻可见的入口。
// "生产可用"只认服务端口径（环境齐 + 真实调用过 + 产物验收通过），界面不自己拼这个结论。
function validationCell(r) {
  const v = r.validation || {};
  const kind = badgeOf(v);
  const [tone, label] = VAL_BADGE[kind];
  const tip = ['env_check', 'mock', 'real_call', 'target_machine', 'artifact_review'].map((k) => {
    const item = v[k] || {};
    const st = (VAL_STATE[item.state] || ['', item.state || '没有记录'])[1];
    return `${VAL_KIND_LABEL[k]}：${st}${item.at ? '（' + String(item.at).slice(5, 16) + '）' : ''}`;
  }).join('\n');
  const ready = v.production_ready
    ? '<span class="chip green">生产可用</span>'
    : '<span class="hint">未达生产可用</span>';
  return `<span class="chip ${tone}" title="${escapeHtml(tip)}">${label}</span> ${ready}
    <div class="val-actions">
      <button class="btn xs" data-env-check="${r.id}">环境检查</button>
      <button class="btn xs" data-val-history="${r.id}">验证记录</button>
    </div>`;
}

function keyCell(r) {
  if (r.backend !== 'cloud') return '<span class="hint">本机运行时不需要密钥</span>';
  const env = r.key_env || '';
  return `<input class="ctl sm mono" data-f="key_env" value="${escapeHtml(r.key_env_custom)}"
                 placeholder="${escapeHtml(env || 'FV_KEY_<PROVIDER>')}">
          <span class="hint">实际读 <code>${escapeHtml(env || '按 provider 推')}</code>
            · ${r.has_key ? '<b>已配置</b>' : '未配置'}</span>`;
}

export default {
  async render() {
    if ((getSub('system-config') ?? 0) === 1) return usersPanel.render();
    // 侧栏入口已经按权限隐藏了，但地址可以直接粘进来，所以这里再挡一道
    if (!store.can('system:config')) {
      return `<section class="view on">
        <div class="page-head"><h1>系统设置</h1><p>模型与密钥</p>
          <div class="right">${subChipRow(SUBS, 0)}</div></div>
        <div class="card"><div class="empty">
        <div class="empty-t">没有权限</div>
        <p>系统设置只有管理员能打开；要改模型配置，找管理员。
           右边切到「用户与角色」还能看用户名单（你有 user:read）。</p>
      </div></div></section>`;
    }

    let rows = [];
    let manifest = null;
    let manifestError = '';
    try {
      [rows, manifest] = await Promise.all([api.providers(), api.providerManifest()]);
    } catch (err) {
      // 清单读不到不代表整页不能用：状态与已存配置还在 providers 里。
      try { rows = await api.providers(); } catch (e2) { manifestError = e2.message; }
      if (!rows.length) {
        return `<section class="view on"><div class="card"><div class="empty">
          <div class="empty-t">读不到配置</div><p>${escapeHtml(manifestError || err.message)}</p>
          </div></div></section>`;
      }
      manifestError = err.message;
    }

    const providerLists = {};
    (manifest?.adapters || []).forEach((a) => {
      const key = `${a.capability}|${a.backend}`;
      (providerLists[key] ||= []).push(a);
    });
    manifestCache = manifest;
    rowsCache = rows;

    const body = rows.map((r) => {
      const adapter = adapterOf(manifest, r.capability, r.backend, r.provider);
      const listKey = `${r.capability}|${r.backend}`;
      const providerOptions = (providerLists[listKey] || [])
        .map((a) => `<option value="${escapeHtml(a.code)}">${escapeHtml(a.label)}</option>`).join('');
      const modelOptions = (adapter?.models || [])
        .map((m) => `<option value="${escapeHtml(m.code)}">${escapeHtml(m.label)}</option>`).join('');
      const videoCloud = r.capability === 'video' && r.backend === 'cloud';
      return `
      <tr class="slot" data-id="${r.id}" data-cap="${r.capability}" data-backend="${r.backend}">
        <td class="t-strong">${r.capability_label}</td>
        <td><span class="chip ${r.backend === 'local' ? 'plain' : 'blue'}">${r.backend_label}</span></td>
        <td><input class="ctl sm" data-f="provider" list="prov-${r.id}"
                   value="${escapeHtml(r.provider)}" placeholder="provider 标识">
            <datalist id="prov-${r.id}">${providerOptions}</datalist></td>
        <td><input class="ctl sm" data-f="model" list="model-${r.id}"
                   value="${escapeHtml(r.model)}" placeholder="模型 ID">
            <datalist id="model-${r.id}">${modelOptions}</datalist></td>
        <td class="key-cell">${keyCell(r)}</td>
        <td class="params-cell">${paramsCell(adapter, r.model, r.provider)}</td>
        <td class="health-cell">${healthCell(r.health)}</td>
        <td class="validation-cell">${validationCell(r)}</td>
        <td class="c"><input type="checkbox" data-f="enabled" ${r.enabled ? 'checked' : ''}></td>
        <td class="c"><input type="radio" name="def-${r.capability}" data-f="default"
                 ${r.is_default ? 'checked' : ''} ${videoCloud ? 'disabled' : ''}
                 ${videoCloud ? 'title="云端视频只能逐镜头显式选择，不能设为默认"' : ''}></td>
      </tr>
      <tr class="slot-note" data-note-for="${r.id}">
        <td colspan="10">
          <span class="src">${noteText(adapter, r.provider)}</span>
          <input class="ctl sm" data-n="notes" value="${escapeHtml(r.notes)}"
                 placeholder="备注：为什么选它、计价出处、哪天核过">
        </td>
      </tr>`;
    }).join('');

    return `
    <section class="view on">
      <div class="page-head">
        <h1>系统设置</h1>
        <p>每类能力两条轨；默认轨走打勾那条，云端视频只能逐镜头另选</p>
        <div class="right">
          ${subChipRow(SUBS, 0)}
          <span class="chip plain" id="save-state" hidden>已保存</span>
          <button class="btn primary" id="save-providers">保存</button>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>模型配置</h2>
          <span class="sub">密钥只存变量名，值从进程环境读；健康状态是每次打开现查的</span>
        </div>
        ${manifestError ? `<div class="card-body"><p class="note">
            <b>能力清单读不到</b>：${escapeHtml(manifestError)}。
            参数与模型建议暂时无法显示，配置本身仍可保存。</p></div>` : ''}
        <div class="table-wrap">
          <table>
          <thead><tr>
          <th>能力</th><th>后端</th><th>Provider</th><th>模型</th><th>密钥变量</th>
          <th>允许参数</th><th>健康</th><th>验证</th><th class="c">启用</th><th class="c">默认</th>
          </tr></thead>
          <tbody>${body}</tbody>
          </table>
        </div>
        <div class="card-body" style="border-top:1px solid var(--line)">
          <p class="note">
            <b>怎么配</b>：密钥写进启动脚本或 <code>.env</code> 后重启服务。变量名默认按
            provider 拼（<code>volc</code> → <code>FV_KEY_VOLC</code>），
            DeepSeek 这类已有名字的可以直接填 <code>DEEPSEEK_API_KEY</code>。
            本地运行时用 <code>FASTVIDEO_&lt;CODE&gt;_HOME</code> 指目录，
            平台只检查目录在不在，不加载权重。
          </p>
          <p class="note">
            <b>验证与健康不是一回事</b>：健康只说环境齐不齐；「验证」列说这一档凭什么被认为真能产出。
            环境检查不发请求、不加载权重，永远不能替代真实调用；真实调用、目标机基准与产物验收都要从一条
            <b>已成功</b>的作业登记，假适配器的成功记不成真实调用。真实调用成功只说明链路产出过文件，
            文件好不好要人看过——产物验收通过才算数。「生产可用」＝环境齐＋真实调用过＋产物验收通过，由服务端判定。
          </p>
        </div>
      </div>

      <div class="modal" id="val-modal" hidden>
        <div class="modal-box wide">
          <div class="modal-head neutral" id="val-title">验证记录</div>
          <div class="modal-body">
            <div id="val-summary"></div>
            <h3 class="drawer-h">从作业登记</h3>
            <div class="form-grid">
              <label for="val-job">作业号</label>
              <input id="val-job" class="ctl" type="number" min="1" placeholder="一条已成功的作业 #id">
              <label for="val-kind">出处</label>
              <select id="val-kind" class="ctl">
                <option value="real_call">真实调用</option>
                <option value="target_machine">目标机基准</option>
                <option value="artifact_review">产物验收（人工看片）</option>
                <option value="mock">模拟（假适配器）</option>
              </select>
              <label for="val-passed" id="val-passed-label">验收结论</label>
              <select id="val-passed" class="ctl">
                <option value="pass">通过</option>
                <option value="fail">不通过</option>
              </select>
              <label for="val-detail">说明</label>
              <input id="val-detail" class="ctl" maxlength="500"
                     placeholder="例如 在目标机上跑通 640×352×81，耗时与峰值显存见作业">
              <span class="pf-label"></span>
              <label class="pf-check"><input type="checkbox" id="val-paid"> 云端调用已确认计费</label>
            </div>
            <p class="modal-meta">真实调用、目标机基准与产物验收都读一条已成功的作业；云端调用必须先有计费确认。
              产物验收记的是人看过文件后的结论，通过与否必须明说。环境检查请用列表里的「环境检查」按钮，不走这里。</p>
            <p class="modal-err" id="val-error"></p>
            <h3 class="drawer-h">最近记录</h3>
            <div id="val-list"><p class="hint">正在读验证历史……</p></div>
          </div>
          <div class="modal-actions">
            <button class="btn" id="val-close">关闭</button>
            <button class="btn primary" id="val-submit">登记</button>
          </div>
        </div>
      </div>
    </section>`;
  },

  mount(root) {
    // 切换器要在任何提前 return 之前绑上：无权限那一屏最需要它
    bindSubChips(root, (i) => { setSub('system-config', i); window.dispatchEvent(new CustomEvent('app:refresh')); });
    if ((getSub('system-config') ?? 0) === 1) { usersPanel.mount?.(root); return; }
    const saveBtn = root.querySelector('#save-providers');
    // 这个名字不能叫 stateChip：同作用域下面有个 stateChip() 函数（验证状态徽章），
    // 局部变量一旦同名就会把函数盖掉，验证记录弹窗一开就抛 "not a function"。
    const saveStateChip = root.querySelector('#save-state');
    if (!saveBtn) return;

    // 选默认顺手把"启用"勾上：默认轨没启用等于这一类没有可用路线，
    // 但后端仍按"唯一默认"校验收不到这个错，不如在前端少给人一次困惑。
    root.querySelectorAll('input[data-f="default"]').forEach((radio) => {
      radio.addEventListener('change', () => {
        const tr = radio.closest('tr');
        tr.querySelector('[data-f="enabled"]').checked = true;
      });
    });

    // 改 provider/model 时就地刷新这一行的参数提示与模型建议，
    // 不重绘整页——重绘会把正在输入的框和光标一起弄丢。
    root.querySelectorAll('tr.slot').forEach((tr) => {
      const refresh = () => {
        const provider = tr.querySelector('[data-f="provider"]').value;
        const model = tr.querySelector('[data-f="model"]').value;
        const adapter = adapterOf(manifestCache, tr.dataset.cap, tr.dataset.backend, provider);
        tr.querySelector('.params-cell').innerHTML = paramsCell(adapter, model, provider);
        const src = tr.nextElementSibling?.querySelector('.src');
        if (src) src.innerHTML = noteText(adapter, provider);
        const dl = tr.querySelector(`datalist[id="model-${tr.dataset.id}"]`);
        if (dl) {
          dl.innerHTML = (adapter?.models || [])
            .map((m) => `<option value="${escapeHtml(m.code)}">${escapeHtml(m.label)}</option>`)
            .join('');
        }
        const keyInput = tr.querySelector('[data-f="key_env"]');
        if (keyInput) {
          keyInput.placeholder = adapter?.key_env || 'FV_KEY_<PROVIDER>';
        }
        // 健康是服务端按"已保存的 provider"算的；这一行还没保存时先别摆旧结论，
        // 否则改完 provider 还亮着"未配置"，看着像输入没生效。
        tr.querySelector('.health-cell').innerHTML =
          '<span class="chip gray">待保存</span>'
          + '<span class="hint">保存后按新 provider 重新检查</span>';
      };
      tr.querySelector('[data-f="provider"]').addEventListener('input', refresh);
      tr.querySelector('[data-f="model"]').addEventListener('input', refresh);
    });

    saveBtn.addEventListener('click', async () => {
      const rows = [...root.querySelectorAll('tr.slot')].map((tr) => {
        const note = root.querySelector(`tr[data-note-for="${tr.dataset.id}"] [data-n="notes"]`);
        return {
          id: Number(tr.dataset.id),
          provider: tr.querySelector('[data-f="provider"]').value,
          model: tr.querySelector('[data-f="model"]').value,
          enabled: tr.querySelector('[data-f="enabled"]').checked,
          is_default: tr.querySelector('[data-f="default"]').checked,
          key_env: tr.querySelector('[data-f="key_env"]')?.value || '',
          notes: note?.value || '',
        };
      });

      saveBtn.disabled = true;
      // 后端会明确说哪类能力少了或多了默认，run 会原样透出来
      const ok = await run('保存', () => api.saveProviders(rows));
      saveBtn.disabled = false;
      if (ok) {
        saveStateChip.hidden = false;
        setTimeout(() => { saveStateChip.hidden = true; }, 2000);
        // 保存后把健康列与验证列换成服务端的最新结论；重绘整页会丢焦点，也只更新这两列
        try {
          const fresh = await api.providers();
          rowsCache = fresh;
          fresh.forEach((r) => {
            const tr = root.querySelector(`tr.slot[data-id="${r.id}"]`);
            if (!tr) return;
            tr.querySelector('.health-cell').innerHTML = healthCell(r.health);
            tr.querySelector('.validation-cell').innerHTML = validationCell(r);
          });
          bindValidationActions(root);
        } catch { /* 回读失败不影响保存本身，下一次打开页面照样会查 */ }
      }
    });

    /* ---------- 验证：环境检查 + 验证记录弹窗 ---------- */
    const valModal = root.querySelector('#val-modal');
    let valSlot = null;

    // 只更新某一行的两列，不重绘整页（输入框焦点不能丢）
    function paintRow(r) {
      const tr = root.querySelector(`tr.slot[data-id="${r.id}"]`);
      if (!tr) return;
      tr.querySelector('.health-cell').innerHTML = healthCell(r.health);
      tr.querySelector('.validation-cell').innerHTML = validationCell(r);
    }

    async function reloadRows() {
      const fresh = await api.providers();
      rowsCache = fresh;
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
          <td class="t-desc">${escapeHtml(r.detail || '')}</td>
          <td>${escapeHtml(r.created_by_name || '—')}</td>
          <td class="t-dim">${escapeHtml(String(r.created_at || '').slice(0, 16))}</td>
        </tr>`).join('')}</tbody></table></div>`;
    }

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
        const all = await api.providerValidations(100);
        const mine = all.filter((r) => r.capability === slot.capability
          && r.backend === slot.backend && r.provider === slot.provider);
        root.querySelector('#val-list').innerHTML = valListHtml(mine);
      } catch (err) {
        root.querySelector('#val-list').innerHTML =
          `<p class="t-bad">验证历史读不到：${escapeHtml(err.message)}</p>`;
      }
      root.querySelector('#val-job').focus();
    }

    function closeValModal() { valModal.hidden = true; valSlot = null; }

    // 「验收结论」只在产物验收这一档出现：别的档记的是事实（作业成功），
    // 产物验收记的是人的判断，通过与否必须显式给，不能藏在默认值里。
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
      if (kind === 'artifact_review') {
        body.passed = root.querySelector('#val-passed').value === 'pass';
      }
      const btn = root.querySelector('#val-submit');
      btn.disabled = true;
      try {
        await api.validateJob(jobId, body);
        const fresh = await reloadRows();
        valSlot = fresh.find((r) => r.id === valSlot.id) || valSlot;
        root.querySelector('#val-summary').innerHTML = summaryHtml(valSlot.validation);
        const all = await api.providerValidations(100);
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

    // 环境检查与验证记录按钮在每次重画验证列后都要重绑，所以抽成函数。
    // 用 onclick 赋值而不是 addEventListener：重绑时不会叠出第二个监听。
    function bindValidationActions(scope) {
      scope.querySelectorAll('[data-env-check]').forEach((btn) => {
        btn.onclick = async () => {
          const slot = rowsCache.find((r) => String(r.id) === btn.dataset.envCheck);
          if (!slot) return;
          btn.disabled = true;
          // 环境检查按"已保存的那一轨"走；输入框里没保存的改动不参与
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
    root.querySelector('#val-close').addEventListener('click', closeValModal);
    root.querySelector('#val-submit').addEventListener('click', submitValidation);
    root.querySelector('#val-kind').addEventListener('change', syncPassedRow);
    valModal.addEventListener('click', (e) => { if (e.target === valModal) closeValModal(); });
    bindModalDismiss(valModal, closeValModal);
  },
};
