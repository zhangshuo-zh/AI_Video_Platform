/**
 * 成片中心（阶段 D，v0.91）：把已审核的镜头合成一集成片。
 *
 * 三条边界：
 *   * 合成是一条普通 Job（provider=ffmpeg），由生成中心那一个 worker 执行；
 *     这一页只读 Job 的结果，不显示成本、不重跑 FFmpeg。
 *   * 每次合成版本加一，旧版本不覆盖；时间线快照冻结在创建那一刻。
 *   * 页面不轮询：合成中时刷新或重开这一条看最新；worker 推完会自己收尾。
 */
import { api } from '../api.js';
import { store } from '../store.js';
import { currentSubTab, getSub, setParam, setSub } from '../navstate.js';
import {
  bindModalDismiss, bindSubChips, escapeHtml, errorBox, subChipRow,
} from '../util.js';
import { publicStatus } from '../validation.js';
import { gotoEntry } from '../goto.js';


/** 一条阻塞：原话 ＋ 一个能跳到问题所在处的按钮。
 *  单独抽成一个函数而不是三层模板套着写——嵌套模板字面量在这里把解析器绕晕过一次，
 *  可读性也一样：这一行的规则是「有 shot_id 就去那一镜，否则有 module 就去那个模块」。
 */
function blockerRow(b) {
  const locate = b.locate || {};
  const label = locate.shot_id ? '去这一镜' : (locate.module ? '去处理' : '');
  const gotoAttr = label
    ? ' <button class="btn xs" data-goto="' + escapeHtml(JSON.stringify(locate)) + '">'
      + label + '</button>'
    : '';
  return '<li><b>阻塞</b> ' + escapeHtml(b.text || '') + gotoAttr + '</li>';
}

const TABS = ['成片'];
const TONE = {
  draft: 'gray', queued: 'blue', starting: 'blue', running: 'blue',
  succeeded: 'green', failed: 'red', cancelled: 'gray',
  attention: 'amber', job_missing: 'gray',
};
const RES = [
  { label: '720p（1280×720）', width: 1280, height: 720 },
  { label: '1080p（1920×1080）', width: 1920, height: 1080 },
];
const KIND = { dialogue: '台词', narration: '旁白', music: '音乐', sfx: '音效' };

let openId = null;
let notice = '';
let noticeKey = '';

/**
 * 跳到另一个视图。app:go 的 detail 是一个视图 id 字符串（app.js 的
 * goView(e.detail)）；二级标签和详情参数要先写进 navstate，
 * 否则跳过去会落在上一次停留的标签上。
 */
function goto(viewId, sub) {
  setSub(viewId, sub);
  setParam(viewId, undefined);
  window.dispatchEvent(new CustomEvent('app:go', { detail: viewId }));
}

function fmtDuration(ms) {
  if (!ms) return '—';
  const total = Math.round(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function fmtBytes(n) {
  if (!n) return '—';
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function rowMeta(r) {
  const tone = TONE[r.status] || 'gray';
  return `<tr class="${r.id === openId ? 'sel' : ''}">
    <td><b>第 ${r.episode_seq ?? '—'} 集</b>
      ${r.episode_title && r.episode_title !== `第 ${r.episode_seq} 集`
        ? `<span class="t-desc">${escapeHtml(r.episode_title)}</span>` : ''}</td>
    <td><span class="mono">v${r.version}</span></td>
    <td><span class="chip ${tone}" title="${escapeHtml(r.status_label)}">${escapeHtml(publicStatus(r.job_status || r.status))}</span></td>
    <td class="num">${r.shot_count} 镜${r.audio_count ? ` · ${r.audio_count} 音频` : ''}</td>
    <td class="num">${fmtDuration(r.duration_ms || r.total_duration_ms)}</td>
    <td class="num">${r.width && r.height ? `${r.width}×${r.height}` : '—'}</td>
    <td class="num">${fmtBytes(r.bytes)}</td>
    <td class="num t-dim">${escapeHtml(r.created_at)}</td>
    <td class="r"><button class="btn sm primary" data-open-comp="${r.id}">查看</button></td>
  </tr>`;
}

function shotDuration(s) {
  // 成片按素材实际时长拼接；计划时长只做对照，差得多时两个都显示，
  // 免得看到成片比预想短还以为漏了一镜。
  const actual = s.duration_ms || 0;
  const planned = s.planned_duration_ms || 0;
  if (planned && Math.abs(planned - actual) > 200) {
    return `${fmtDuration(actual)}（分镜计划 ${(planned / 1000).toFixed(1)}s）`;
  }
  return fmtDuration(actual);
}

function detailHtml(d) {
  const tone = TONE[d.status] || 'gray';
  const shots = (d.timeline?.shots || []).map((s) => `<li class="cp-shot">
      <span class="pl-idx">${s.seq}</span>
      <div class="cp-shot-main">
        <div class="pl-title"><b>第 ${s.seq} 镜</b>
          <span class="chip green">已审</span>
          <span class="t-desc">${shotDuration(s)}</span></div>
        <div class="pl-meta">
          <span>${escapeHtml(s.summary || '')}</span>
          <button class="btn xs" data-go-job="${s.video.job_id}">作业 #${s.video.job_id}</button>
        </div>
      </div>
    </li>`).join('');
  const audio = (d.audio || []).map((a) => `<li class="cp-audio">
      <span class="chip plain">${escapeHtml(KIND[a.kind] || a.kind)}</span>
      <span>${escapeHtml(a.speaker || '')}</span>
      <span class="t-desc">从 ${fmtDuration(a.start_ms)} 起</span>
    </li>`).join('');
  const canSubmit = store.can('job:submit');
  return `<div class="card cp-detail">
    <div class="card-head"><h2>成片 v${d.version} · 第 ${d.episode_seq} 集</h2>
      <span class="sub">${escapeHtml(d.episode_title || '')}</span>
      <div class="right">
        <span class="chip ${tone}" title="${escapeHtml(d.status_label)}">${escapeHtml(publicStatus(d.job_status || d.status))}</span>
        ${d.job_id ? `<button class="btn sm" data-go-job="${d.job_id}">去生成中心</button>` : ''}
      </div></div>
    <div class="card-body">
      ${d.error_message ? `<div class="bar bad"><p>${escapeHtml(d.error_message)}</p></div>` : ''}
      ${d.status === 'succeeded' && d.artifact_url ? `
        <video class="cp-video" controls preload="metadata" src="${escapeHtml(d.artifact_url)}"></video>
        <div class="cp-meta">
          <span><b>${d.width}×${d.height}</b> · ${fmtDuration(d.duration_ms)} · ${fmtBytes(d.bytes)}</span>
          <span class="mono" title="sha256">${escapeHtml(d.sha256)}</span>
        </div>
        <p class="hint">合成是一条普通 Job；成本、日志与产物清单在生成中心，签字仍在审核中心。</p>` : `
        <div class="empty"><div class="empty-t">${d.status === 'failed' ? '这一版没合成成功' : '还在合成'}</div>
          <p>${d.status === 'failed'
            ? '原因已经写在上面；把问题修掉后重新发起，会得到下一版，旧版本保留。'
            : 'worker 跑完后刷新这一页即可看到结果。合成不走定时轮询。'}</p>
          ${canSubmit ? '<div class="empty-form"><button class="btn primary" data-new-comp>重新发起一版</button></div>' : ''}
        </div>`}
      <div class="cp-sub">时间线（${d.shot_count} 镜 · ${fmtDuration(d.total_duration_ms)}）</div>
      <ol class="pl-nodes">${shots || '<li class="t-desc">没有镜头</li>'}</ol>
      ${audio ? `<div class="cp-sub">音频（${audio.length}）</div><ul class="cp-audios">${audio}</ul>` : ''}
      ${(d.subtitles || []).length ? `<div class="cp-sub">字幕</div>
        <ul class="cp-audios"><li class="cp-audio">
          <span class="mono">${escapeHtml(d.subtitles[0].path || '')}</span>
          <span class="t-desc">${fmtBytes(d.subtitles[0].bytes || 0)} · 烧进画面</span>
        </li></ul>` : ''}
      <div class="cp-sub">版本与来源</div>
      <p class="cp-src">第 ${d.episode_seq} 集 · v${d.version} · 创建于 ${escapeHtml(d.created_at)}
        ${d.job_id ? ` · 关联作业 #${d.job_id}` : ''} · 输出分辨率 ${d.params?.width}×${d.params?.height} @ ${d.params?.fps}fps</p>
    </div>
  </div>`;
}

function createModal(episodes) {
  const options = episodes.map((e) => `<option value="${e.id}">
      第 ${e.seq} 集 · ${escapeHtml(e.title)}（${e.shot_count} 镜）</option>`).join('');
  return `<div class="modal" id="cp-modal" hidden>
    <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="cp-modal-title">
      <div class="modal-head neutral" id="cp-modal-title">新建成片</div>
      <div class="modal-body">
        <p class="modal-meta">合成前会检查每一镜：视频作业要成功、产物要在盘上、并且<b>已通过审核</b>。
          缺任何一项都会明确拒绝并列出是哪几镜。</p>
        <label class="modal-label" for="cp-ep">集</label>
        <select class="ctl" id="cp-ep">${options}</select>
        <label class="modal-label" for="cp-res">分辨率</label>
        <select class="ctl" id="cp-res">
          ${RES.map((r, i) => `<option value="${i}">${r.label}</option>`).join('')}
        </select>
        <label class="modal-label" for="cp-fps">帧率</label>
        <select class="ctl" id="cp-fps">
          <option value="24">24 fps</option><option value="30">30 fps</option>
        </select>
        <!-- 0037：合成前检查。数字与判据都来自服务端同一份规则（precheck.py），
             这一栏只负责摊开给人看并给跳转，不在界面重算一遍。 -->
        <label class="modal-label">合成前检查</label>
        <div id="cp-pre"><p class="hint">正在检查这一集能不能拼…</p></div>
        <label class="modal-label" for="cp-sub">字幕（可选）</label>
        <div id="cp-srt-wrap" class="cp-srt-wrap">
          <p class="hint">正在读这个项目的字幕文件…</p>
        </div>
        <p class="cp-err" id="cp-modal-err" hidden></p>
      </div>
      <div class="modal-actions">
        <button class="btn" data-cp-cancel>取消</button>
        <button class="btn primary" data-cp-ok>开始合成</button>
      </div>
    </div>
  </div>`;
}

/** 字幕那一栏的界面。
 *
 * 为什么异步渲染：这一栏要同时知道"项目里有哪些字幕"和"这台机器的 FFmpeg
 * 烧不烧得动"，两件事都要问服务端；把它们塞进 render() 会让打开弹窗多等两次请求。
 * 不支持时把选择器禁用并写清原因——留得下选择就会让人提交后才吃 422。
 */
function subtitleHtml(state) {
  const items = state.items || [];
  if (!state.subtitle_supported) {
    const opts = items.map((s) =>
      `<option value="${escapeHtml(s.path)}" disabled>${escapeHtml(s.name)}</option>`).join('');
    return `<select class="ctl" id="cp-sub" disabled>
        <option value="">这台机器的 FFmpeg 烧不了字幕</option>${opts}
      </select>
      <p class="hint cp-srt-hint">${escapeHtml(state.ffmpeg_hint || '这台机器的 FFmpeg 没有 subtitles 滤镜。')}</p>
      ${items.length ? '' : '<p class="hint">传上去的字幕会留在本项目里，装好带 libass 的 FFmpeg 再选。</p>'}`;
  }
  const opts = items.map((s) =>
    `<option value="${escapeHtml(s.path)}">${escapeHtml(s.name)} · `
    + `${fmtBytes(s.bytes)} · ${escapeHtml(s.modified_at)}</option>`).join('');
  return `<select class="ctl" id="cp-sub">
      <option value="">不用字幕</option>${opts}
    </select>
    <div class="row cp-srt-tools">
      <label class="btn xs cp-srt-upload">上传 .srt／.vtt
        <input type="file" id="cp-sub-file" accept=".srt,.vtt" hidden>
      </label>
      ${items.length ? '<button class="btn xs" data-cp-sub-del type="button">删除所选</button>' : ''}
    </div>
    <p class="hint cp-srt-hint">字幕会烧进画面（不是软字幕），并冻结在这一版时间线里。</p>`;
}

export default {
  subIndex: 0,

  async render() {
    const project = store.current;
    if (!project) {
      openId = null; notice = ''; noticeKey = '';
      return `<section class="view on"><div class="card"><div class="empty">
        <div class="empty-t">先选一个项目</div>
        <p>成片按项目与集组织；去项目中心选一个再回来。</p>
      </div></div></section>`;
    }
    const tab = currentSubTab('composition', TABS);
    const key = `${project.id}:${tab}`;
    if (noticeKey !== key) { notice = ''; noticeKey = ''; }
    let list = [];
    let episodes = [];
    let err = null;
    try {
      [list, episodes] = await Promise.all([
        api.compositions(project.id), api.episodes(project.id),
      ]);
    } catch (e) { err = e; }
    const canSubmit = store.can('job:submit');
    const head = `<div class="page-head">
        <h1>成片中心</h1>
        <p>已审镜头按顺序合成一集：时间线快照、版本与 sha256 · ${escapeHtml(project.name)}</p>
        <div class="row" style="gap:6px">${subChipRow(TABS, tab)}</div>
      </div>`;
    const rows = list.map(rowMeta).join('');
    return `<section class="view on">
      ${head}
      ${notice ? `<div class="bar ok"><p>${escapeHtml(notice)}</p></div>` : ''}
      ${err ? errorBox('成片列表', err) : ''}
      <div class="card">
        <div class="card-head"><h2>成片版本</h2>
          <span class="sub">每次合成版本加一，旧版本不覆盖；页面不轮询</span>
          <div class="right">
            ${canSubmit ? '<button class="btn sm primary" data-new-comp>新建成片</button>'
              : '<span class="t-desc">发起合成需要 job:submit</span>'}
          </div></div>
        ${list.length ? `<div class="table-wrap"><table>
          <thead><tr><th>集</th><th>版本</th><th>状态</th><th class="num">时间线</th>
            <th class="num">时长</th><th class="num">分辨率</th><th class="num">大小</th>
            <th class="r">创建</th><th class="r"></th></tr></thead>
          <tbody>${rows}</tbody></table></div>` : `<div class="empty">
          <div class="empty-t">还没有成片</div>
          <p>选一集发起合成：只收已审核的镜头视频，按分镜顺序拼成一条 mp4。
            合成由 worker 执行 FFmpeg，一条成片对应一条普通作业。</p>
          ${canSubmit ? '<div class="empty-form"><button class="btn primary" data-new-comp>新建成片</button></div>' : ''}
        </div>`}
      </div>
      ${openId ? '<div id="cp-detail-host"><div class="card mt-card" ><div class="card-body"><p class="hint">正在读成片…</p></div></div></div>' : ''}
      ${createModal(episodes)}
    </section>`;
  },

  async mount(root) {
    if (!store.current) return;
    const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
    bindSubChips(root, (i) => { setSub('composition', i); refresh(); });

    // 「成片列表」读失败那张卡（:262）上的重试原来点了没反应：`errorBox` 默认画
    // **可点**的 `[data-retry]`，全站没有容器级委托（2026-11-10 逐文件普查确认），
    // 而这一屏只在**详情**那个 catch 里绑过一次（:314），整页级的列表卡没人接。
    // 详情那张是异步画的、此刻还不存在，两处不会重复绑同一个节点。
    root.querySelectorAll('[data-retry]').forEach((btn) =>
      btn.addEventListener('click', refresh));

    root.querySelectorAll('[data-open-comp]').forEach((btn) => btn.addEventListener('click', () => {
      openId = Number(btn.dataset.openComp);
      refresh();
    }));
    root.querySelectorAll('[data-go-job]').forEach((btn) => btn.addEventListener('click', () => {
      goto('generate', 0);
    }));

    const host = root.querySelector('#cp-detail-host');
    if (host && openId) {
      const btn = root.querySelector(`[data-open-comp="${openId}"]`);
      btn?.classList.add('sel');
      try {
        const d = await api.composition(openId);
        if (!host.isConnected) return;
        host.innerHTML = detailHtml(d);
        host.querySelectorAll('[data-go-job]').forEach((b) => b.addEventListener('click', () => {
          goto('generate', 0);
        }));
        host.querySelectorAll('[data-new-comp]').forEach((b) => b.addEventListener('click', openModal));
      } catch (e) {
        if (!host.isConnected) return;
        host.innerHTML = errorBox('成片详情', e);
        host.querySelector('.empty.err [data-retry]')?.addEventListener('click', refresh);
      }
    }

    const modal = root.querySelector('#cp-modal');
    if (!modal) return;
    let idemKey = '';
    let subState = { items: [], subtitle_supported: false, ffmpeg_hint: '' };
    const subWrap = modal.querySelector('#cp-srt-wrap');

    const paintSubs = () => {
      if (!subWrap) return;
      subWrap.innerHTML = subtitleHtml(subState);
      const file = subWrap.querySelector('#cp-sub-file');
      file?.addEventListener('change', async () => {
        const picked = file.files?.[0];
        if (!picked) return;
        const err = modal.querySelector('#cp-modal-err');
        try {
          subState = await api.uploadCompositionSubtitle(store.current.id, picked);
          paintSubs();
          // 上传成功后直接选中刚传的那份：省一次"从列表里认出它"的动作
          const latest = subState.items?.[0];
          const sel = subWrap.querySelector('#cp-sub');
          if (latest && sel) sel.value = latest.path;
          err.hidden = true;
        } catch (e) {
          err.textContent = e.message || '字幕没传上去。';
          err.hidden = false;
        }
      });
      subWrap.querySelector('[data-cp-sub-del]')?.addEventListener('click', async () => {
        const sel = subWrap.querySelector('#cp-sub');
        if (!sel?.value) return;
        const err = modal.querySelector('#cp-modal-err');
        try {
          subState = await api.deleteCompositionSubtitle(store.current.id, sel.value);
          paintSubs();
          err.hidden = true;
        } catch (e) {
          err.textContent = e.message || '没删掉。';
          err.hidden = false;
        }
      });
    };

    // 0037：合成前检查。界面只负责显示服务端给的那三档，不自己判。
    let preState = null;
    const preBox = () => modal.querySelector('#cp-pre');
    const paintPre = async (episodeId) => {
      const box = preBox();
      if (!box) return;
      if (!episodeId) { box.innerHTML = '<p class="hint">先选一集。</p>'; preState = null; return; }
      box.innerHTML = '<p class="hint">正在检查这一集能不能拼…</p>';
      try {
        preState = await api.compositionPrecheck(store.current.id, episodeId);
      } catch (err) {
        preState = null;
        box.innerHTML = `<p class="hint">检查没跑成（${escapeHtml(err.message || '未知错误')}），`
          + '点「开始合成」时服务端照样会把关。</p>';
        return;
      }
      if (!modal.isConnected) return;
      const tone = { 阻塞: 'red', 警告: 'amber', 通过: 'green' }[preState.status] || 'gray';
      const bad = (preState.items || []).filter((i) => i.level === '阻塞');
      const warn = (preState.items || []).filter((i) => i.level === '警告');
      // 不填分辨率会按哪一档出：这句话由服务端算（`timeline.majority_size()`），
      // 弹窗只复述。原来这里什么都没说，人以为留空是"跟随源"，实际写死放大到 1280×720。
      const plan = preState.output_plan;
      const planLine = plan
        ? `<p class="hint">分辨率两栏留空就与素材同尺寸：`
          + `${plan.width}×${plan.height}（${plan.same_as}/${plan.of} 镜是这一档）</p>`
        : '';
      box.innerHTML = `<p><span class="chip ${tone}">${escapeHtml(preState.status)}</span>
          <span class="hint">共 ${preState.total} 项 · 阻塞 ${bad.length} · 警告 ${warn.length}</span></p>
        ${bad.length ? `<ul class="hint">${bad.map(blockerRow).join('')}</ul>` : ''}
        ${warn.length ? `<ul class="hint">${warn.map((w) => `<li>提醒 ${escapeHtml(w.text)}</li>`).join('')}</ul>` : ''}
        ${planLine}`;
    };

    modal.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-goto]');
      if (!btn) return;
      let entry;
      try { entry = JSON.parse(btn.dataset.goto); } catch { return; }
      closeModal();
      gotoEntry(entry);
    });

    const openModal = () => {
      const err = modal.querySelector('#cp-modal-err');
      err.hidden = true;
      idemKey = (crypto.randomUUID?.() ?? `cp-${Date.now()}-${Math.random().toString(36).slice(2)}`);
      modal.hidden = false;
      modal.querySelector('#cp-ep')?.focus();
      if (subWrap) paintSubs();
      paintPre(Number(modal.querySelector('#cp-ep')?.value) || null);
      // 每次都现读：这台机器的 FFmpeg 可能刚被换过，缓存会让界面说瞎话
      api.compositionSubtitles(store.current.id)
        .then((state) => {
          if (!modal.isConnected) return;
          subState = state || subState;
          paintSubs();
        })
        .catch(() => { /* 读不到就当没有：合成本身不依赖字幕，别为此挡住弹窗 */ });
    };
    const closeModal = () => { modal.hidden = true; };
    bindModalDismiss(modal, closeModal);
    modal.querySelector('[data-cp-cancel]').addEventListener('click', closeModal);
    modal.addEventListener('click', (e) => { if (e.target === modal) closeModal(); });
    root.querySelectorAll('[data-new-comp]').forEach((b) => b.addEventListener('click', openModal));
    // 换集要重跑检查：阻塞项是按集算的，留着上一集的结论会指错地方
    modal.querySelector('#cp-ep')?.addEventListener('change', (e) => {
      paintPre(Number(e.target.value) || null);
    });
    modal.querySelector('[data-cp-ok]').addEventListener('click', async () => {
      const err = modal.querySelector('#cp-modal-err');
      const episodeId = Number(modal.querySelector('#cp-ep').value);
      const res = RES[Number(modal.querySelector('#cp-res').value)] || RES[0];
      const fps = Number(modal.querySelector('#cp-fps').value) || 24;
      if (!episodeId) {
        err.textContent = '先选一集。';
        err.hidden = false;
        return;
      }
      // 检查跑过且明确阻塞就不发起：服务端那条路也会拒，但那时只剩一句错误原话，
      // 人已经点过一次"开始合成"了。这里提前一步，界面上还能看见是哪几项。
      if (preState && preState.status === '阻塞') {
        err.textContent = '合成前检查有阻塞项：' + preState.items
          .filter((i) => i.level === '阻塞').map((i) => i.text).join('；');
        err.hidden = false;
        return;
      }
      try {
        const view = await api.createComposition(store.current.id, {
          episode_id: episodeId, width: res.width, height: res.height,
          fps, idempotency_key: idemKey,
          subtitle_path: modal.querySelector('#cp-sub')?.value || null,
        });
        idemKey = '';
        openId = view.id;
        closeModal();
      } catch (e) {
        // 前置检查的原话（哪几镜缺片／未审）留在弹窗里，不让人关了再猜
        err.textContent = e.message || '没发起成功。';
        err.hidden = false;
        return;
      }
      notice = '合成已排进队列，worker 跑完后刷新查看。';
      noticeKey = `${store.current.id}:0`;
      refresh();
    });
  },
};
