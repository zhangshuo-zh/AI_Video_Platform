/**
 * 模型中心 · 2 调用策略（九个任务场景 → 必需能力／建议轨／偏好／备用轨）。
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
  sourceLabels, sourceLabel, overviewCard,
  bumpResolveSeq, getResolveSeq,
} from './shared.js';

/* ================= 工作区 2 · 调用策略 ================= */
/**
 * 场景那一格的徽章：**「配不出来」与「待配置」不是一件事**。
 *
 * 前者是清单里这一轴压根没有能产出那项能力的来源（例如 image 轴没有任何一档
 * 登记过图片输入参数，而某个场景要「图生图」）——下一步是写适配器，点界面没用。
 * 后者只是还没人登记、还没人点探测——下一步是去配。
 * 两种都写成"待配置"，人就会对着一个永远点不亮的场景反复点（2026-11-07 审查里
 * 4 个场景正是这样）。判据来自后端（`impossible_required`），界面不自己推。
 */
export function policyStateChip(p) {
  if (p.required_producible === false) {
    const missing = (p.impossible_required || []).join('、');
    return `<span class="chip red" title="这些必需能力在清单里没有任何来源：${
      escapeHtml(missing)}；要让它有解得先写适配器，或者把这一项从「必需」降下来">
      清单缺能力</span>`;
  }
  return p.enabled ? '<span class="chip green">已配置</span>'
                   : '<span class="chip gray">待配置</span>';
}
export function pagePolicies() {
  const items = cache.policies.items;
  const candidatesOf = (capability) => cache.catalog.items
    .filter((i) => i.capability === capability && i.registered)
    .map((i) => `<option value="${escapeHtml(i.provider)}|${escapeHtml(i.model_code)}">
        ${escapeHtml(i.display_name || i.model_label || i.model_code)}</option>`).join('');

  /* 必需能力从只读 chip 放开成勾选：《使用说明书》§5.4 本来就教人
     "把这一项从「必需」降下来"，服务端 save_policies 也一直在写 required_json——
     规则齐了，入口没开，于是 character_variant（清单缺「参考图」来源）
     只能挂一枚红 chip 让人反复配。勾选项来自后端算好的标签全集
     （producible，与派生标签同源），界面不自己发明第四个标签名；
     清单产不出的那一项仍然显示，但划灰并写明"清单里没有任何来源"。 */
  const requiredPicker = (p) => (p.producible || []).map((tag) => {
    const on = p.required.includes(tag);
    const impossible = (p.impossible_required || []).includes(tag);
    return `<label class="pf-check mc-req" title="${escapeHtml(
      impossible ? '清单里没有任何一档能产出它；勾了也不会有候选' : '作为这一场景的必需能力')}">
      <input type="checkbox" data-req="${escapeHtml(tag)}" ${on ? 'checked' : ''}>
      <span ${impossible ? 'style="text-decoration:line-through;opacity:.55"' : ''}>${
        escapeHtml(tag)}${impossible ? '（清单缺来源）' : ''}</span></label>`;
  }).join('') || '<span class="hint">这一轴清单里还没有任何可声明的标签</span>';

  const rows = items.map((p) => {
    const cur = p.slot_provider ? `${p.slot_provider}|${p.slot_model}` : '';
    const fb = p.fallback_provider ? `${p.fallback_provider}|${p.fallback_model}` : '';
    return `<tr class="policy" data-key="${escapeHtml(p.scene_key)}">
      <td><b>${escapeHtml(p.scene_label || p.scene_key)}</b>
        <div class="hint mono">${escapeHtml(p.scene_key)}</div></td>
      <td>${escapeHtml(p.capability_label)}</td>
      <td><div class="param-chips" style="flex-direction:column;align-items:flex-start;max-width:220px">
        ${requiredPicker(p)}</div></td>
      <td><select class="ctl sm" data-f="slot">
          <option value="">— 不指定 —</option>${candidatesOf(p.capability)}</select></td>
      <td><select class="ctl sm" data-f="pref">
          ${Object.entries(cache.policies.preference_labels)
            .map(([k, v]) => `<option value="${k}">${escapeHtml(v)}</option>`).join('')}
        </select></td>
      <td><select class="ctl sm" data-f="fallback">
          <option value="">— 无 —</option>${candidatesOf(p.capability)}</select></td>
      <td class="c"><input type="checkbox" data-f="cloud" ${p.cloud_confirm_required ? 'checked' : ''}></td>
      <td class="c"><input type="checkbox" data-f="on" ${p.enabled ? 'checked' : ''}></td>
      <td>${policyStateChip(p)}
        <div><button class="btn xs" data-resolve="${escapeHtml(p.scene_key)}">看候选</button></div></td>
    </tr>`;
  }).join('');

  return `
  <div class="card">
    <div class="card-head">
      <h2>任务调用策略</h2>
      <span class="sub">场景 → 必需能力 → 建议轨 / 偏好 / 备用轨</span>
      <span class="right"><button class="btn primary" id="save-policies">保存策略</button></span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>任务场景</th><th>能力</th><th>必需能力</th><th>建议轨</th>
          <th>偏好</th><th>备用轨</th><th class="c">要确认计费</th><th class="c">启用</th><th>状态</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="card-body" style="border-top:1px solid var(--line)">
      <p class="modal-err" id="policy-error"></p>
      <p class="note"><b>保存只改配置，不会启动任何任务</b>；
        也不会把建议轨写成默认轨——默认轨的唯一来源仍是「引擎管理」那张能力轨表。</p>
      <p class="note"><b>备用轨不自动顶上</b>（清单里 <code>no_auto_fallback</code> 为
        ${String(cache.policies.no_auto_fallback)}）。备用轨的意义是：默认轨不行时，
        「看候选」能立刻告诉你哪一档能力匹配、可以人工改选，而不是悄悄换一条路把钱花了。</p>
      <p class="note"><b>候选只从登记里出</b>：下拉里能看到的前提是这一档已经在
        「模型仓库」登记过。没登记就不是可选项，避免选了一个跑不起来的东西。</p>
    </div>
  </div>`;
}

export function mountPolicies(root) {
  // 把当前值填进 select：渲染时没带 selected，统一在这里回填，省一遍字符串拼接
  root.querySelectorAll('tr.policy').forEach((tr) => {
    const key = tr.dataset.key;
    const p = cache.policies.items.find((x) => x.scene_key === key);
    if (!p) return;
    tr.querySelector('[data-f="slot"]').value = p.slot_provider ? `${p.slot_provider}|${p.slot_model}` : '';
    tr.querySelector('[data-f="fallback"]').value = p.fallback_provider ? `${p.fallback_provider}|${p.fallback_model}` : '';
    tr.querySelector('[data-f="pref"]').value = p.preference;
  });

  root.querySelector('#save-policies').addEventListener('click', async () => {
    const rows = [...root.querySelectorAll('tr.policy')].map((tr) => {
      const p = cache.policies.items.find((x) => x.scene_key === tr.dataset.key);
      const slot = tr.querySelector('[data-f="slot"]').value.split('|');
      const fb = tr.querySelector('[data-f="fallback"]').value.split('|');
      return {
        scene_key: tr.dataset.key, scene_label: p.scene_label, capability: p.capability,
        // 必需能力读勾选框：那一格从只读 chip 放开成可编辑（§5.4 一直教人
        // "从「必需」降下来"，服务端也早会写 required_json，缺的只是入口）。
        required: [...tr.querySelectorAll('[data-req]:checked')].map((c) => c.dataset.req),
        slot_provider: slot[0] || '', slot_model: slot[1] || '',
        preference: tr.querySelector('[data-f="pref"]').value,
        fallback_provider: fb[0] || '', fallback_model: fb[1] || '',
        resource_limit: p.resource_limit || {},
        cloud_confirm_required: tr.querySelector('[data-f="cloud"]').checked,
        enabled: tr.querySelector('[data-f="on"]').checked,
      };
    });
    const errEl = root.querySelector('#policy-error');
    const btn = root.querySelector('#save-policies');
    errEl.textContent = '';
    btn.disabled = true;
    try {
      await api.saveModelPolicies(rows);
      btn.disabled = false;
      window.dispatchEvent(new CustomEvent('app:refresh'));
    } catch (err) {
      btn.disabled = false;
      // 这里不走全站那个 alert：后端的校验是"一次说清所有问题"，
      // 四五条错误拼成一句长文本塞进系统弹窗，人既读不完也复制不走。
      // 原话留在表下面，改一条就能对着看。
      errEl.textContent = err.message || '保存失败';
    }
  });

  root.querySelectorAll('[data-resolve]').forEach((b) => b.addEventListener('click', async () => {
    const modal = root.querySelector('#mc-resolve');
    const body = root.querySelector('#mc-resolve-body');
    modal.hidden = false;
    body.innerHTML = '<p class="hint">正在按能力匹配……</p>';
    // 请求序号：连点两行时两条候选都会回来，**后到的那条覆盖先到的**——
    // 而"后到"未必是"后点的"（同屏两个请求，一个走缓存一个不走）。
    // 音频中心与产线页的同场景早就加了这道闸（audio.js:157／pipeline.js:790），
    // 这一处是漏的那个。
    const wanted = bumpResolveSeq();
    let out;
    try { out = await api.resolveScene(b.dataset.resolve); } catch (err) {
      if (wanted !== getResolveSeq()) return;
      body.innerHTML = `<p class="t-bad">候选读不到：${escapeHtml(err.message)}</p>`;
      return;
    }
    if (wanted !== getResolveSeq()) return;   // 已经被更新的一次点击接管了
    const cand = out.candidates.map((c) => `<tr>
      <td><b>${escapeHtml(c.label)}</b><div class="hint mono">${escapeHtml(c.provider)}/${escapeHtml(c.model_code)}</div></td>
      <td><span class="chip ${c.role === 'fallback' ? 'blue' : 'plain'}">${c.role === 'fallback' ? '备用轨' : '建议轨'}</span></td>
      <td><span class="chip ${c.status === 'available' ? 'green' : 'amber'}">${escapeHtml(c.status_label)}</span></td>
      <td>${c.needs_cloud_confirm ? '<span class="chip amber">要逐次确认计费</span>' : '<span class="hint">本地</span>'}</td>
    </tr>`).join('');
    body.innerHTML = `
      <p class="modal-meta">${escapeHtml(out.policy_note || '')}</p>
      <h3 class="drawer-h">满足必需能力的候选（${out.candidates.length}）</h3>
      ${out.candidates.length
        ? `<div class="table-wrap"><table><thead><tr><th>模型</th><th>角色</th><th>状态</th><th>云端</th></tr></thead>
            <tbody>${cand}</tbody></table></div>`
        : '<p class="hint">一条都没有。</p>'}
      <h3 class="drawer-h">为什么别的不行（${out.blockers.length}）</h3>
      <ul class="hint">${out.blockers.map((b) => `<li>${escapeHtml(b)}</li>`).join('') || '<li>没有</li>'}</ul>
      <p class="modal-meta">必需能力：${out.required.map(escapeHtml).join('、') || '（没写）'}。
        这一屏只回答"谁可能行"；要真用它，请在发起弹窗里显式选，或在能力轨把它设为默认。</p>`;
  }));
}

/* 页头的"检查引擎连接"用一条轻提示代替弹窗：整批检查是 10 个 POST，
   结果本身已经写进各行的健康列，不需要再弹一层。 */