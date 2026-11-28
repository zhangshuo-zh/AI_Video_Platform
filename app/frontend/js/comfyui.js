import { api } from './api.js';
import { escapeHtml } from './util.js';

const CKPT_STORAGE_KEY = 'avp.comfyui.checkpoint';
const INST_STORAGE_KEY = 'avp.comfyui.instance_id';
const CACHE_TTL_MS = 15_000;

const ckptCache = new Map();
const ckptInflight = new Map();
let instCache = null;
let instCachedAt = 0;
let instInflight = null;

function readStorage(key) {
  try {
    return window.localStorage.getItem(key) || '';
  } catch {
    return '';
  }
}

function writeStorage(key, value) {
  try {
    const val = String(value || '').trim();
    if (val) window.localStorage.setItem(key, val);
    else window.localStorage.removeItem(key);
  } catch { /* ignore storage restrictions */ }
}

export function rememberedCheckpoint() {
  return readStorage(CKPT_STORAGE_KEY);
}

export function rememberCheckpoint(name) {
  writeStorage(CKPT_STORAGE_KEY, name);
}

export function rememberedInstanceId() {
  return readStorage(INST_STORAGE_KEY);
}

export function rememberInstanceId(id) {
  writeStorage(INST_STORAGE_KEY, id);
}

export async function loadComfyInstances({ force = false } = {}) {
  const now = Date.now();
  if (!force && instCache && now - instCachedAt < CACHE_TTL_MS) {
    return instCache;
  }
  if (!force && instInflight) return instInflight;
  instInflight = api.comfyuiInstances()
    .then((rows) => {
      instCache = Array.isArray(rows) ? rows : [];
      instCachedAt = Date.now();
      return instCache;
    })
    .catch(() => [])
    .finally(() => {
      instInflight = null;
    });
  return instInflight;
}

export async function loadComfyCheckpoints({ instanceId = null, force = false } = {}) {
  const key = instanceId ? String(instanceId) : '';
  const now = Date.now();
  const hit = ckptCache.get(key);
  if (!force && hit && now - hit.at < CACHE_TTL_MS) {
    return hit.data;
  }
  if (!force && ckptInflight.has(key)) return ckptInflight.get(key);
  const req = api.comfyuiCheckpoints(instanceId || null)
    .then((res) => {
      const data = {
        ok: Boolean(res?.ok),
        endpoint: res?.endpoint || '',
        display_name: res?.display_name || '',
        checkpoints: Array.isArray(res?.checkpoints) ? res.checkpoints : [],
        recommended: res?.recommended || '',
        error: res?.error || '',
      };
      ckptCache.set(key, { at: Date.now(), data });
      return data;
    })
    .catch((err) => ({
      ok: false,
      endpoint: '',
      display_name: '',
      checkpoints: [],
      recommended: '',
      error: err?.message || '查询 ComfyUI 模型列表失败',
    }))
    .finally(() => {
      ckptInflight.delete(key);
    });
  ckptInflight.set(key, req);
  return req;
}

export function instanceOptionsMarkup(instances, selectedId = '') {
  const rows = Array.isArray(instances) ? instances : [];
  if (!rows.length) {
    return '<option value="">无启用中的 ComfyUI 实例</option>';
  }
  const picked = String(selectedId || '');
  const hasMatch = picked && rows.some((r) => String(r.id) === picked);
  return rows.map((r, idx) => {
    const isSel = hasMatch ? String(r.id) === picked : idx === 0;
    const label = r.display_name ? `${r.display_name} (${r.endpoint})` : r.endpoint;
    return `<option value="${escapeHtml(String(r.id))}"${isSel ? ' selected' : ''}>${escapeHtml(label)}</option>`;
  }).join('');
}

const KONTEXT_MODEL = 'flux1-dev-kontext_fp8_scaled.safetensors';

export function checkpointOptionsMarkup(data, selected = '') {
  const picked = (selected || '').trim();
  const list = Array.isArray(data?.checkpoints) ? [...data.checkpoints] : [];
  if (picked && !list.includes(picked)) {
    list.unshift(picked);
  }
  const autoLabel = data?.recommended
    ? `自动匹配（推荐：${data.recommended}）`
    : (data?.ok === false ? '自动匹配（节点离线，按默认）' : '自动匹配（按节点默认）');
  const head = `<option value=""${picked === '' ? ' selected' : ''}>${escapeHtml(autoLabel)}</option>`;
  const rows = list.map((name) => (
    `<option value="${escapeHtml(name)}"${name === picked ? ' selected' : ''}>${escapeHtml(name)}</option>`
  )).join('');
  const kontextOpt = !list.includes(KONTEXT_MODEL)
    ? `<option value="${KONTEXT_MODEL}"${picked === KONTEXT_MODEL ? ' selected' : ''}>${KONTEXT_MODEL}（L4 FLUX Kontext 一致性模式）</option>`
    : '';
  return head + rows + kontextOpt;
}
