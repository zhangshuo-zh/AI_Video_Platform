import { escapeHtml } from './util.js';

const STORAGE_PREFIX = 'anim_model_choice_';
const BAD_HEALTH = new Set(['missing', 'unconfigured', 'unregistered']);

export function readRememberedModel(key) {
  if (!key) return '';
  try {
    return window.localStorage.getItem(`${STORAGE_PREFIX}${key}`) || '';
  } catch {
    return '';
  }
}

export function saveRememberedModel(key, val) {
  if (!key || !val) return;
  try {
    window.localStorage.setItem(`${STORAGE_PREFIX}${key}`, val);
  } catch { /* ignore storage restrictions */ }
}

/**
 * 从可用槽位清单里按「引擎 (backend|provider)」去重聚合，方便两级联动控件复用。
 */
export function uniqueEnginesFromOptions(options = []) {
  const map = new Map();
  for (const o of options) {
    const backend = o.backend || 'local';
    const provider = o.provider || '';
    const key = `${backend}|${provider}`;
    const existing = map.get(key);
    const healthy = !BAD_HEALTH.has(o.health_state);
    if (!existing) {
      map.set(key, {
        key,
        backend,
        provider,
        label: o.adapter_label || provider,
        count: 1,
        hasHealthy: healthy,
      });
    } else {
      existing.count += 1;
      if (healthy) existing.hasHealthy = true;
    }
  }
  return [...map.values()];
}

function pickPreferredOption(list, preferredValue = '') {
  if (!list.length) return null;
  if (preferredValue) {
    const exact = list.find((o) => `${o.backend}|${o.provider}|${o.model}` === preferredValue);
    if (exact) return exact;
  }
  return list.find((o) => o.backend === 'local' && !BAD_HEALTH.has(o.health_state)
      && (o.health_state === 'ok' || o.validation_state === 'ok'))
    || list.find((o) => !BAD_HEALTH.has(o.health_state)
      && (o.health_state === 'ok' || o.validation_state === 'ok'))
    || list.find((o) => !BAD_HEALTH.has(o.health_state))
    || list[0];
}

/**
 * 把单个 `backend|provider|model` <select> 升级成「选引擎 -> 选模型」两级联动控件。
 * 原 <select> 的 value、change 事件与表单读取逻辑保持不变，业务代码零改动。
 */
export function attachEngineFilter(modelSel, options = [], cfg = {}) {
  if (!modelSel || !Array.isArray(options) || !options.length) return null;
  if (modelSel.dataset.empAttached === '1') return null;
  modelSel.dataset.empAttached = '1';

  const {
    storageKey = '',
    healthLabel = {},
    engineAriaLabel = '选择引擎',
    wrapClass = '',
  } = cfg;

  const remembered = readRememberedModel(storageKey);
  const initialOption = pickPreferredOption(options, remembered)
    || pickPreferredOption(options, modelSel.value);
  const initialVal = initialOption
    ? `${initialOption.backend}|${initialOption.provider}|${initialOption.model}`
    : modelSel.value;

  const engines = uniqueEnginesFromOptions(options);
  const localEngines = engines.filter((e) => e.backend === 'local');
  const cloudEngines = engines.filter((e) => e.backend === 'cloud');

  const shortEngineLabel = (raw) => String(raw || '')
    .replace(/（(?:官方\s*API|本机模型服务|本地服务|FLUX\.1)）/g, '')
    .trim();

  const engineOptMarkup = (e) => {
    const tag = e.backend === 'cloud' ? '云端' : '本地';
    const name = shortEngineLabel(e.label) || e.provider;
    const suffix = e.hasHealthy ? ` (${e.count})` : ` (${e.count}·不可用)`;
    return `<option value="${escapeHtml(e.key)}">${tag} · ${escapeHtml(name)}${escapeHtml(suffix)}</option>`;
  };

  const engineSel = document.createElement('select');
  const baseCls = modelSel.classList.contains('sm') ? 'ctl sm' : 'ctl';
  engineSel.className = `${baseCls} emp-engine`;
  engineSel.setAttribute('aria-label', engineAriaLabel);
  engineSel.innerHTML = [
    localEngines.length ? `<optgroup label="本地引擎">${localEngines.map(engineOptMarkup).join('')}</optgroup>` : '',
    cloudEngines.length ? `<optgroup label="云端引擎">${cloudEngines.map(engineOptMarkup).join('')}</optgroup>` : '',
  ].join('');

  const initialEngineKey = initialOption
    ? `${initialOption.backend}|${initialOption.provider}`
    : (engines[0]?.key || '');
  if (initialEngineKey) engineSel.value = initialEngineKey;

  const wrap = document.createElement('span');
  wrap.className = `emp-pair${wrapClass ? ` ${wrapClass}` : ''}`;
  const parent = modelSel.parentNode;
  if (!parent) return null;
  parent.insertBefore(wrap, modelSel);
  wrap.appendChild(engineSel);
  wrap.appendChild(modelSel);

  const syncModels = (targetVal = '', dispatchChange = false) => {
    const engineKey = engineSel.value;
    const filtered = options.filter((o) => `${o.backend}|${o.provider}` === engineKey);
    const chosen = pickPreferredOption(filtered, targetVal || readRememberedModel(storageKey));
    const chosenVal = chosen ? `${chosen.backend}|${chosen.provider}|${chosen.model}` : '';
    modelSel.innerHTML = filtered.map((o) => {
      const val = `${o.backend}|${o.provider}|${o.model}`;
      const isBad = BAD_HEALTH.has(o.health_state);
      const warn = isBad
        ? (o.backend === 'local' && o.provider === 'ollama'
          ? '（未启动，将自动拉起）'
          : `（${healthLabel[o.health_state] || '不可用'}）`)
        : (o.validation_state === 'ok' ? '（已验证）' : '');
      return `<option value="${escapeHtml(val)}"${val === chosenVal ? ' selected' : ''}>${escapeHtml(o.model)}${escapeHtml(warn)}</option>`;
    }).join('');
    if (chosenVal) modelSel.value = chosenVal;
    if (dispatchChange) {
      modelSel.dispatchEvent(new Event('change', { bubbles: true }));
    }
  };

  syncModels(initialVal, false);

  engineSel.addEventListener('change', () => {
    syncModels('', true);
  });

  modelSel.addEventListener('change', () => {
    if (storageKey && modelSel.value) saveRememberedModel(storageKey, modelSel.value);
  });

  return { engineSel, modelSel, wrap };
}
