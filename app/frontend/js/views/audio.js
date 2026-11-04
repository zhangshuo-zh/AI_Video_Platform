/**
 * 音频中心（W15）：台词、旁白、音乐、口型、音效的素材台账。
 *
 * 这里不建第二条队列：每条素材按「送进队列」后仍落一条 Job，
 * 由生成中心那一个 worker 跑；这一页只负责关联到集与镜头、把产物路径收回。
 * 真实 VoxCPM2 / ACE-Step 运行时不在本机时，提交会明确说缺什么，不假装出音频。
 */
import { api } from '../api.js';
import { getSub, setSub } from '../navstate.js';
import { store } from '../store.js';
import { bindSubChips, confirmDanger, escapeHtml, errorBox, run, subChipRow } from '../util.js';
import { jobLabel } from '../validation.js';

const TABS = [
  { key: 'voice', label: '配音', kinds: ['dialogue', 'narration'],
    hint: '台词与旁白。台词挂到镜头，旁白可以只挂到集。' },
  { key: 'music', label: '音乐', kinds: ['music'],
    hint: '一集可以有多条音乐；按用途写在文本里，生成时整段送进队列。' },
  { key: 'viseme', label: '口型', kinds: ['dialogue'],
    hint: '口型是台词的后处理；Rhubarb 适配器尚未接线，这一版只记录素材与占用空间。' },
  { key: 'sfx', label: '音效', kinds: ['sfx'],
    hint: '环境音与音效还没有可核对的自托管适配器，这一档标为实验态：能登记，不能提交。' },
];
const KIND_LABEL = { dialogue: '台词', narration: '旁白', music: '音乐', sfx: '音效' };
let audioNotice = '';   // 提交失败的原话留在页面上，不弹 alert 挡住下一次操作
// 这句话是在"哪个项目 × 哪个页签"说的。它是模块级变量，视图重渲染不会自动清，
// 不带这个键就会出现：在 A 项目配音页签报的"缺运行时"，切到 B 项目音乐页签还挂着，
// 看上去像那边也失败了。键一对不上就丢掉——同一页签里再点一次别的行为也照旧能看见。
let audioNoticeKey = '';
// 换集时的镜头候选请求序号。下拉里显示哪一集的镜头，必须跟着**最后一次**点击走：
// 连点两集时先发的那条往往后到，旧版本的列表就会盖掉新那一条——用户看到的是
// 「第 2 集」的下拉里装着第 1 集的镜头，提交上去就挂错镜头。
let shotReqId = 0;
// 页签下标只在这里做一次兜底：地址里出现越界的 sub 时按第 0 个渲染，
// 提示条也要落在同一个下标上，否则一边算 3、一边算 0，话刚写上去就被自己清掉。
const currentTab = () => {
  const raw = getSub('audio') ?? 0;
  return TABS[raw] ? raw : 0;
};
const noticeKeyOf = (pid, tab) => `${pid}:${tab}`;
const STATUS = {
  draft: ['草稿', 'gray'], queued: ['排队中', 'blue'],
  ready: ['已就绪', 'green'], failed: ['失败', 'red'],
};

export default {
  async render() {
    const project = store.current;
    if (!project) {
      audioNotice = ''; audioNoticeKey = '';
      return `<section class="view on"><div class="card"><div class="empty">
        <div class="empty-t">先选一个项目</div>
        <p>音频素材按项目与集组织；去项目中心选一个再回来。</p>
      </div></div></section>`;
    }
    const tab = currentTab();
    const key = noticeKeyOf(project.id, tab);
    if (audioNoticeKey !== key) { audioNotice = ''; audioNoticeKey = ''; }
    const info = TABS[tab];
    const subRow = subChipRow(TABS.map((t) => t.label), tab);
    let episodes = [];
    let shotsByEp = {};
    let clips = [];
    let err = null;
    try {
      episodes = await api.episodes(project.id);
      const lists = await Promise.all(episodes.map((e) => api.shots(e.id)));
      episodes.forEach((e, i) => { shotsByEp[e.id] = lists[i]; });
      clips = await api.audioClips(project.id);
    } catch (e) { err = e; }
    const mine = clips.filter((c) => info.kinds.includes(c.kind));
    const canWrite = store.can('audio:write') && store.can('job:submit');

    const rows = mine.map((c) => {
      const [label, tone] = STATUS[c.status] || [c.status, 'gray'];
      const where = [c.episode_title ? `第 ${c.episode_seq} 集` : '未挂集',
                     c.shot_id ? `第 ${c.shot_seq} 镜` : null].filter(Boolean).join(' · ');
      const playable = c.path && c.job_id;
      return `<tr>
        <td><b>${escapeHtml(c.speaker || KIND_LABEL[c.kind] || c.kind)}</b>
          <span class="t-desc">${escapeHtml(where)}</span></td>
        <td><div class="audio-text">${escapeHtml(c.text)}</div>
          ${c.notes ? `<span class="t-desc">${escapeHtml(c.notes)}</span>` : ''}</td>
        <td><span class="chip ${tone}">${escapeHtml(label)}</span>
          ${c.job_id ? `<span class="t-desc">作业 #${c.job_id}${c.job_status ? ' · ' + escapeHtml(jobLabel(c.job_status)) : ''}</span>` : ''}
          ${c.job_error ? `<span class="t-desc t-bad">${escapeHtml(c.job_error)}</span>` : ''}</td>
        <td>${tab === 2 ? (c.visemes.length ? `${c.visemes.length} 段` : '<span class="t-dim">未生成</span>')
          : (playable ? `<audio controls preload="none" style="height:30px;max-width:230px"
              src="/api/jobs/${c.job_id}/artifacts/0"></audio>`
            : c.path ? `<span class="t-desc" title="${escapeHtml(c.path)}">${escapeHtml(c.path)}</span>`
            : '<span class="t-dim">—</span>')}</td>
        <td class="r">
          ${canWrite && (c.status === 'draft' || c.status === 'failed')
            ? `<button class="btn sm primary" data-audio-submit="${c.id}">送进队列</button>` : ''}
          ${tab === 2 && c.status === 'ready'
            ? '<button class="btn sm" disabled title="Rhubarb 适配器尚未接线">生成口型</button>' : ''}
          ${canWrite ? `<button class="btn sm danger" data-audio-del="${c.id}">删除</button>` : ''}
        </td>
      </tr>`;
    }).join('');

    const epOptions = episodes.map((e) =>
      `<option value="${e.id}">第 ${e.seq} 集 · ${escapeHtml(e.title)}</option>`).join('');
    const firstEp = episodes[0]?.id;
    const shotOptions = (id) => (shotsByEp[id] || []).map((s) =>
      `<option value="${s.id}">第 ${s.seq} 镜 · ${escapeHtml(s.summary.slice(0, 24))}</option>`).join('');

    return `<section class="view on">
      <div class="page-head">
        <h1>音频中心</h1>
        <p>配音 VoxCPM2 · 音乐 ACE-Step · 口型 Rhubarb 分层</p>
        <div class="right">${subRow}</div>
      </div>
      ${err ? `<div class="card">${errorBox('音频素材', err)}</div>` : ''}
      ${audioNotice ? `<div class="card"><div class="bar bad">${escapeHtml(audioNotice)}</div></div>` : ''}
      <div class="card">
        <div class="card-head"><h2>${info.label}</h2><span class="sub">${info.hint}</span></div>
        ${canWrite && episodes.length ? `
        <div class="card-body audio-new">
          <div class="row" style="gap:8px;flex-wrap:wrap">
            <select id="audio-kind" class="ctl sm" style="width:auto">
              ${info.kinds.map((k) => `<option value="${k}">${KIND_LABEL[k]}</option>`).join('')}
            </select>
            <select id="audio-ep" class="ctl sm" style="width:auto">${epOptions}</select>
            <select id="audio-shot" class="ctl sm" style="width:auto">
              <option value="">不挂镜头</option>${shotOptions(firstEp)}
            </select>
            <input id="audio-speaker" class="ctl sm" maxlength="80"
                   placeholder="说话人（旁白可空）" style="width:150px">
            <input id="audio-text" class="ctl sm" maxlength="4000"
                   placeholder="台词／音乐描述" style="flex:1;min-width:220px">
            <button class="btn sm primary" id="audio-add">登记素材</button>
          </div>
          <p class="hint" style="text-align:left;margin:8px 0 0">
            登记只写素材台账；点「送进队列」才落 Job，成本只从 Job 记。
          </p>
        </div>` : ''}
        ${!episodes.length ? `<div class="empty"><div class="empty-t">还没有集</div>
          <p>先去创作中心建一集，音频才有地方挂。</p></div>`
        : mine.length ? `<div class="table-wrap"><table>
          <thead><tr><th>素材</th><th>文本</th><th>状态</th>
            <th>${tab === 2 ? '口型' : '产物'}</th><th class="r"></th></tr></thead>
          <tbody>${rows}</tbody></table></div>`
        : `<div class="empty"><div class="empty-t">这一类还没有素材</div>
          <p>${canWrite ? '用上面的表单登记第一条。' : '这一页只读；你需要 audio:write 才能登记。'}</p></div>`}
      </div>
    </section>`;
  },

  mount(root) {
    if (!store.current) return;
    const refresh = () => window.dispatchEvent(new CustomEvent('app:refresh'));
    bindSubChips(root, (i) => { setSub('audio', i); refresh(); });

    // 换集时只换镜头候选；不整页重绘，刚写的文本还在输入框里
    root.querySelector('#audio-ep')?.addEventListener('change', async (e) => {
      const wanted = Number(e.target.value);
      const req = ++shotReqId;
      let shots = [];
      try {
        shots = await api.shots(wanted);
      } catch (err) {
        // 读失败不演成"这一集没有镜头"：那会让人以为候选是空的
        const box = root.querySelector('#audio-shot');
        if (!box || req !== shotReqId || !box.isConnected) return;
        box.innerHTML = '<option value="">不挂镜头</option>';
        audioNotice = `这一集的镜头读不出来：${err.message || err}`;
        // 与提交失败同一把 key：换页签/换项目就该收走，留在本页才看得见
        audioNoticeKey = noticeKeyOf(store.currentId, currentTab());
        return;
      }
      const select = root.querySelector('#audio-shot');
      // 页签/项目已经切走，或这条不是最后一次点击的响应 —— 丢掉，别盖掉新页面
      if (!select || !select.isConnected || req !== shotReqId) return;
      select.innerHTML = '<option value="">不挂镜头</option>' + shots.map((s) =>
        `<option value="${s.id}">第 ${s.seq} 镜 · ${escapeHtml(s.summary.slice(0, 24))}</option>`).join('');
    });

    root.querySelector('#audio-add')?.addEventListener('click', async () => {
      const text = root.querySelector('#audio-text')?.value.trim();
      if (!text) { root.querySelector('#audio-text')?.focus(); return; }
      const body = {
        episode_id: Number(root.querySelector('#audio-ep')?.value) || null,
        shot_id: Number(root.querySelector('#audio-shot')?.value) || null,
        kind: root.querySelector('#audio-kind')?.value || 'dialogue',
        speaker: root.querySelector('#audio-speaker')?.value.trim() || '',
        text,
      };
      const ok = await run('登记音频', () => api.createAudioClip(store.currentId, body));
      if (ok) refresh();
    });

    root.querySelectorAll('[data-audio-submit]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        audioNotice = ''; audioNoticeKey = '';
        try {
          await api.submitAudioClip(Number(btn.dataset.audioSubmit));
        } catch (e) {
          // 缺运行时是这一档的常见结果，用页内提示说明缺什么；
          // 弹 alert 会挡住下一次操作，也看不到别的素材状态
          audioNotice = `送进队列失败：${e.message}`;
          audioNoticeKey = noticeKeyOf(store.currentId, currentTab());
          btn.disabled = false;
          refresh();
          return;
        }
        refresh();
      });
    });
    root.querySelectorAll('[data-audio-del]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        if (!(await confirmDanger({ title: '删除音频素材',
                                    body: '只删这一条台账；已提交的作业与产物还在生成中心。',
                                    okText: '删除' }))) return;
        const ok = await run('删除', () =>
          api.deleteAudioClip(Number(btn.dataset.audioDel)));
        if (ok) refresh();
      });
    });
  },
};
