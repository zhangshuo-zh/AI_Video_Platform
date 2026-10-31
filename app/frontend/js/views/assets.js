import { store } from '../store.js';
import { api } from '../api.js';
import { escapeHtml, run } from '../util.js';
import { getSub, setSub } from '../navstate.js';

/**
 * 资产中心：角色 / 场景 / 物品。
 * 定妆图现在只填路径，等文件上传做了再换成上传。
 */
const KINDS = [
  { code: '', label: '全部' },
  { code: 'character', label: '角色' },
  { code: 'scene', label: '场景' },
  { code: 'prop', label: '物品' },
];

const kindIndex = () => getSub('assets') ?? 0;

export default {
  async render() {
    const project = store.current;
    if (!project) {
      return `<section class="view on"><div class="card"><div class="empty">
        <div class="empty-t">先选一个项目</div>
        <p>资产属于项目，先去项目中心建项目。</p>
      </div></div></section>`;
    }

    const picked = KINDS[kindIndex()];
    let list = [];
    try { list = await api.assets(project.id, picked.code || undefined); } catch { list = []; }

    const chips = KINDS.map((k, i) => `
      <span class="chip ${kindIndex() === i ? 'blue' : 'plain'}" data-kind="${i}"
            style="cursor:pointer">${k.label}</span>`).join('');

    const rows = list.map((a) => `
      <tr data-aid="${a.id}">
        <td class="t-strong">${escapeHtml(a.name)}</td>
        <td><span class="chip plain">${a.kind_label}</span></td>
        <td class="t-dim">${escapeHtml(a.consistency_prompt) || '—'}</td>
        <td>
          <div class="imgs">
            ${a.ref_images.map((p) => `
              <span class="img-wrap">
                <img class="img-thumb" src="/files/${p}" alt="定妆图" data-preview="/files/${p}">
                <button class="img-del" title="删除这张" data-del-img="${a.id}|${p}">×</button>
              </span>`).join('')}
            <label class="img-add ${a.ref_images.length ? '' : 'wide'}" title="上传定妆图">
              ${a.ref_images.length ? '＋' : '上传定妆图'}
              <input type="file" accept="image/png,image/jpeg,image/webp" data-upload="${a.id}" hidden>
            </label>
          </div>
        </td>
        <td class="r"><button class="btn sm danger" data-del-asset="${a.id}">删除</button></td>
      </tr>`).join('');

    const kindOptions = KINDS.filter((k) => k.code)
      .map((k) => `<option value="${k.code}">${k.label}</option>`).join('');

    return `
    <section class="view on">
      <div class="page-head">
        <h1>资产中心</h1>
        <p>${escapeHtml(project.name)} · 定妆图 + 一致性提示词，跨项目复用</p>
        <div class="right">${chips}</div>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>${picked.label === '全部' ? '全部资产' : picked.label}</h2>
          <span class="sub">${list.length} 条</span>
        </div>
        ${list.length ? `
        <table>
          <thead><tr><th>名称</th><th>类型</th><th>一致性提示词</th><th>定妆图</th><th class="r"></th></tr></thead>
          <tbody>${rows}</tbody>
        </table>` : `
        <div class="empty">
          <div class="empty-t">还没有资产</div>
          <p>先在下面登记一个角色或场景。之后生成镜头时按名字引用它。</p>
        </div>`}

        <div class="card-body" style="border-top:1px solid var(--line)">
          <div class="empty-form" style="justify-content:flex-start">
            <select id="asset-kind" class="ctl">${kindOptions}</select>
            <input id="asset-name" placeholder="名称，例如 掌灯人" style="min-width:150px">
            <input id="asset-prompt" placeholder="一致性提示词：外形、配色、禁忌" style="min-width:280px;flex:1">
            <button class="btn primary" id="asset-create">登记</button>
          </div>
          <p class="hint" style="text-align:left;margin-top:8px">
            同一类型里不能重名——重名说明该合并，不是新建。
            定妆图支持 PNG / JPEG / WebP，单张不超过 20MB；iPhone 拍的 HEIC 要先转格式。
          </p>
        </div>
      </div>
    </section>`;
  },

  mount(root) {
    root.querySelectorAll('[data-kind]').forEach((el) => {
      el.addEventListener('click', () => {
        setSub('assets', Number(el.dataset.kind));
        window.dispatchEvent(new CustomEvent('app:refresh'));
      });
    });

    const kindEl = root.querySelector('#asset-kind');
    const nameEl = root.querySelector('#asset-name');
    const promptEl = root.querySelector('#asset-prompt');
    const createBtn = root.querySelector('#asset-create');

    const addAsset = async () => {
      const name = nameEl.value.trim();
      if (!name) { nameEl.focus(); return; }
      createBtn.disabled = true;
      // 409 是重名，后端把原因写在 detail 里，run 会原样透出来
      const ok = await run('登记', () => api.createAsset(store.currentId, {
        kind: kindEl.value,
        name,
        consistency_prompt: promptEl.value.trim(),
      }));
      createBtn.disabled = false;
      if (!ok) return;
      window.dispatchEvent(new CustomEvent('app:refresh'));
    };
    createBtn.addEventListener('click', addAsset);
    nameEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') addAsset(); });

    // 上传定妆图
    root.querySelectorAll('[data-upload]').forEach((input) => {
      input.addEventListener('change', async () => {
        const file = input.files?.[0];
        if (!file) return;
        input.disabled = true;
        const ok = await run('上传', () => api.uploadAssetImage(Number(input.dataset.upload), file));
        input.disabled = false;
        input.value = '';
        if (!ok) return;
        window.dispatchEvent(new CustomEvent('app:refresh'));
      });
    });

    // 点图看大图，点 × 删这张
    root.querySelectorAll('[data-preview]').forEach((img) => {
      img.addEventListener('click', () => window.open(img.dataset.preview, '_blank'));
    });
    root.querySelectorAll('[data-del-img]').forEach((btn) => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const [id, path] = btn.dataset.delImg.split('|');
        if (!confirm('删掉这张定妆图？文件会一起删除。')) return;
        const ok = await run('删除', () => api.deleteAssetImage(Number(id), path));
        if (!ok) return;
        window.dispatchEvent(new CustomEvent('app:refresh'));
      });
    });

    root.querySelectorAll('[data-del-asset]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        if (!confirm('删掉这个资产？已引用的镜头不会被改。')) return;
        await api.deleteAsset(Number(btn.dataset.delAsset));
        window.dispatchEvent(new CustomEvent('app:refresh'));
      });
    });
  },
};
