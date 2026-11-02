import { store } from '../store.js';
import { api } from '../api.js';
import { bindModalDismiss, escapeHtml, run } from '../util.js';
import { setParam } from '../navstate.js';
import { closeDetail } from '../router.js';

/**
 * 资产详情。由 assets.js 在地址带 `a<id>` 时调起来——它不是一个独立模块，
 * 所以不往 views/index.js 里注册，也不进侧栏。
 *
 * 四条口径：
 * - 基本信息一次整体提交（PUT），同一个字段不在两处各给一个编辑入口。
 * - 全局资产人人可读，只有管理员可改可删；界面上的按钮按这个显隐，
 *   真正的门在服务端（access.ensure_asset）。
 * - 关联分镜只读＋跳转，不在这里改镜头。
 * - 被引用的资产不给删。这里不藏这个事实：删除弹窗直接把引用数摆出来。
 */
let cache = null;          // 本次渲染拿到的资产与引用清单，mount 里复用，不再跑第二趟
let refs = [];

const split = (v) => v.split(/[,，]/).map((x) => x.trim()).filter(Boolean);

async function load(id) {
  let asset = null;
  try {
    asset = await api.asset(id);
  } catch (err) {
    return { error: err.message || '读不到这条资产', status: err.status };
  }
  let shots = [];
  try { shots = await api.assetShots(id); } catch { shots = []; }
  return { asset, shots };
}

export async function renderDetail(id) {
  const data = await load(id);
  cache = data;
  refs = data.shots || [];

  if (data.error) {
    return `
    <section class="view on">
      <div class="page-head"><h1>资产详情</h1></div>
      <div class="card"><div class="empty">
        <div class="empty-t">打不开这条资产</div>
        <p>${escapeHtml(data.error)}${data.status === 404 ? '（可能已被删除）' : ''}</p>
        <p><button class="btn" data-back>返回资产列表</button></p>
      </div></div>
    </section>`;
  }

  const a = data.asset;
  const isGlobal = a.project_id === null;
  // 能不能改：全局要管理员，项目要 asset:write（服务端同样判一遍）
  const canEdit = store.can('asset:write') && (!isGlobal || store.isManager);
  const kindOptions = [['character', '角色'], ['scene', '场景'], ['prop', '道具']]
    .map(([v, l]) => `<option value="${v}"${a.kind === v ? ' selected' : ''}>${l}</option>`).join('');

  return `
    <section class="view on">
      <div class="page-head">
        <h1><button class="crumb" data-back>资产中心</button>
          <span class="crumb-sep">›</span> ${escapeHtml(a.name)}</h1>
        <p>${a.kind_label} · ${isGlobal ? '全局资产池（各项目共享引用，不属于单个项目）'
             : '本项目资产'} · ${a.ref_images.length} 张参考图 · 被 ${a.ref_shot_count} 条镜头引用</p>
        <div class="right">
          ${canEdit ? '<button class="btn primary" id="ad-save">保存</button>' : ''}
          ${canEdit ? '<button class="btn danger-solid" id="ad-del">删除</button>' : ''}
        </div>
      </div>

      <div class="ad-grid">
        <div class="stack">
          <div class="card">
            <div class="card-head"><h2>基本信息</h2>
              <span class="sub">名称在同一范围、同一类型里唯一</span></div>
            <div class="card-body">
              <div class="form-grid">
                <label for="ad-kind">资产类型</label>
                <select id="ad-kind" class="ctl"${canEdit ? '' : ' disabled'}>${kindOptions}</select>

                <label for="ad-name">名称</label>
                <input id="ad-name" class="ctl" maxlength="60" value="${escapeHtml(a.name)}"
                       autocomplete="off"${canEdit ? '' : ' disabled'}>

                <label for="ad-summary" class="top">简介</label>
                <textarea id="ad-summary" class="ctl" rows="2" maxlength="500"
                          ${canEdit ? '' : 'disabled'}>${escapeHtml(a.summary)}</textarea>

                <label for="ad-tags">标签</label>
                <input id="ad-tags" class="ctl" value="${escapeHtml((a.tags || []).join(', '))}"
                       placeholder="逗号分隔，最多 10 个"${canEdit ? '' : ' disabled'}>

                <label for="ad-aliases">别名</label>
                <input id="ad-aliases" class="ctl" value="${escapeHtml((a.aliases || []).join(', '))}"
                       placeholder="逗号分隔"${canEdit ? '' : ' disabled'}>

                <label for="ad-prompt" class="top">一致性提示词</label>
                <textarea id="ad-prompt" class="ctl" rows="5" maxlength="1000"
                          placeholder="跨镜头必须长一样的细节"${canEdit ? '' : ' disabled'}
                    >${escapeHtml(a.consistency_prompt)}</textarea>

                <span class="pf-label">归属</span>
                ${isGlobal
                  ? '<span class="t-dim">全局资产池 —— 已属于所有项目，不能收回成单个项目资产'
                    + '（可能正被多个项目的镜头引用，收回会让引用悬空）</span>'
                  : (store.isManager
                      ? `<label class="chk"><input type="checkbox" id="ad-global">
                           <span>升为全局资产（id 不变，现有引用照旧有效）</span></label>`
                      : '<span class="t-dim">本项目资产（升为全局只有管理员能做）</span>')}
              </div>
              <p class="modal-meta">创建于 ${a.created_at}　最后修改 ${a.updated_at}
                ${canEdit ? '' : '　· 你对此资产只读'}</p>
            </div>
          </div>

          <div class="card">
            <div class="card-head"><h2>一致性提示词怎么写</h2></div>
            <div class="card-body">
              <p class="note">
                ${a.kind === 'character'
                  ? '角色：面部与识别特征、发型、服装、体型与总体风格、必须保持不变的细节。'
                  : a.kind === 'scene'
                    ? '场景：空间结构、建筑风格、环境与光照氛围、需要保持一致的固定元素。'
                    : '道具：形状、材质、颜色、尺寸与比例、独特标记。'}
                这段是喂给生成模型的稳定描述，抽取只会给建议值，最终由你改。
              </p>
            </div>
          </div>
        </div>

        <div class="stack">
          <div class="card">
            <div class="card-head"><h2>参考图</h2>
              <span class="sub">第一张是封面</span></div>
            <div class="card-body">
              <div class="ad-imgs">
                ${a.ref_images.map((p, i) => `
                  <figure class="ad-img${i === 0 ? ' cover' : ''}">
                    <img src="/files/${p}" alt="参考图 ${i + 1}" data-preview="/files/${p}">
                    <figcaption>
                      ${i === 0 ? '<span class="chip blue">封面</span>'
                        : (canEdit ? `<button class="btn xs" data-cover="${p}">设为封面</button>` : '')}
                      ${canEdit ? `<button class="btn xs danger" data-rm-img="${p}">删除</button>` : ''}
                    </figcaption>
                  </figure>`).join('')
                  || '<p class="t-dim">还没有参考图。定妆图、侧面、服装都可以传，'
                     + '它们是同一资产的不同视角，不会各自变成一条新资产。</p>'}
              </div>
              ${canEdit ? `<label class="btn ad-upload">上传参考图
                <input type="file" id="ad-upload" accept="image/png,image/jpeg,image/webp" hidden>
              </label>
              <p class="modal-meta">PNG / JPEG / WebP，单张不超过 20MB；iPhone 的 HEIC 请先转格式。
                服务端按文件头判断类型，不看文件名。</p>` : ''}
            </div>
          </div>

          <div class="card">
            <div class="card-head"><h2>被哪些镜头引用</h2>
              <span class="sub">${refs.length} 条 · 只读</span></div>
            <div class="card-body">
              ${refs.length ? refs.map((s) => `
                <div class="ad-ref">
                  <div>
                    <div class="t-strong">${escapeHtml(s.project_name)}
                      <span class="t-dim">第 ${s.episode_seq} 集 · ${escapeHtml(s.episode_title)}</span></div>
                    <p class="t-dim">镜 ${s.seq} · ${escapeHtml(s.summary)}</p>
                  </div>
                  <button class="btn xs" data-goto-shot="${s.episode_id}">去创作中心</button>
                </div>`).join('')
                : '<p class="t-dim">还没有镜头引用它。在创作中心的分镜表里选资产即可引用。</p>'}
            </div>
          </div>
        </div>
      </div>

      <div class="modal" id="ad-del-modal" hidden>
        <div class="modal-box">
          <div class="modal-head">删除资产</div>
          <div class="modal-body">
            <p class="modal-warn">此操作不可撤销，资产本身与它登记过的参考图都会没掉。</p>
            <p>要删除的是「<b>${escapeHtml(a.name)}</b>」${a.ref_images.length
              ? `，含 ${a.ref_images.length} 张参考图` : ''}。</p>
            ${a.ref_shot_count ? `<p class="modal-warn">它正被 ${a.ref_shot_count} 条镜头引用，`
              + '删不掉——先去创作中心把这些镜头的引用去掉。这里不提供"强行删除"，'
              + '悬空的引用会在分镜表里显示成一个没有名字的编号。</p>' : ''}
          </div>
          <div class="modal-actions">
            <button class="btn" id="ad-del-cancel">取消</button>
            <button class="btn danger-solid" id="ad-del-ok"${a.ref_shot_count ? ' disabled' : ''}>
              确认删除</button>
          </div>
        </div>
      </div>
    </section>`;
}

export function mountDetail(root, id) {
  const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
  const back = () => closeDetail('assets');
  root.querySelectorAll('[data-back]').forEach((el) => el.addEventListener('click', back));

  if (!cache || cache.error) return;
  const a = cache.asset;

  root.querySelectorAll('[data-preview]').forEach((img) => {
    img.addEventListener('click', () => window.open(img.dataset.preview, '_blank'));
  });

  // 跳创作中心并定位到那一集：只留一个"下一次进去看哪一集"的提示，
  // 不越界去改创作中心的状态
  root.querySelectorAll('[data-goto-shot]').forEach((btn) => {
    btn.addEventListener('click', () => {
      setParam('writing', Number(btn.dataset.gotoShot));
      window.dispatchEvent(new CustomEvent('app:go', { detail: 'writing' }));
    });
  });

  const saveBtn = root.querySelector('#ad-save');
  saveBtn?.addEventListener('click', async () => {
    const name = root.querySelector('#ad-name').value.trim();
    if (!name) { root.querySelector('#ad-name').focus(); return; }
    const globalEl = root.querySelector('#ad-global');
    saveBtn.disabled = true;
    const ok = await run('保存', () => api.updateAsset(id, {
      kind: root.querySelector('#ad-kind').value,
      name,
      summary: root.querySelector('#ad-summary').value.trim(),
      consistency_prompt: root.querySelector('#ad-prompt').value.trim(),
      tags: split(root.querySelector('#ad-tags').value),
      aliases: split(root.querySelector('#ad-aliases').value),
      scope: globalEl?.checked ? 'global' : 'project',
    }));
    saveBtn.disabled = false;
    if (ok) refresh();
  });

  // 注意选择器：这个 input 是 id="ad-upload"（藏在 label 里），
  // 老列表页用的是 data-upload，抄过来会绑到一个不存在的节点上、上传静默失效
  root.querySelector('#ad-upload')?.addEventListener('change', async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.disabled = true;
    const ok = await run('上传', () => api.uploadAssetImage(id, file));
    e.target.disabled = false;
    e.target.value = '';
    if (ok) refresh();
  });

  root.querySelectorAll('[data-cover]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const ok = await run('设为封面', () => api.setAssetCover(id, btn.dataset.cover));
      if (ok) refresh();
    });
  });

  root.querySelectorAll('[data-rm-img]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const ok = await run('删除这张图', () => api.deleteAssetImage(id, btn.dataset.rmImg));
      if (ok) refresh();
    });
  });

  const delModal = root.querySelector('#ad-del-modal');
  const delOk = root.querySelector('#ad-del-ok');
  const closeDel = () => { delModal.hidden = true; delOk.disabled = false; };
  root.querySelector('#ad-del')?.addEventListener('click', () => { delModal.hidden = false; });
  root.querySelector('#ad-del-cancel')?.addEventListener('click', closeDel);
  delModal?.addEventListener('click', (e) => { if (e.target === delModal) closeDel(); });
  // Escape 走 util 的那一个实现：绑在 delModal 上只有"焦点已经 Tab 进弹窗"
  // 才收得到 keydown，而打开这个确认框时焦点还留在外面那张卡片上。
  if (delModal) bindModalDismiss(delModal, closeDel);
  delOk?.addEventListener('click', async () => {
    delOk.disabled = true;
    const ok = await run('删除资产', () => api.deleteAsset(id));
    if (!ok) { delOk.disabled = false; return; }
    closeDel();
    back();                      // 删完停在列表，别停在这条已经不存在的详情上
    refresh();
  });
}
