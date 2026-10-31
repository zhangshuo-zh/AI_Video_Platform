import { MODULES } from '../modules.js';
import { store } from '../store.js';

/**
 * 首页 = 模块地图 + 九实体说明 + 本机状态。
 * 本机状态里只放后端真报回来的数字；worker / 队列还没接，就老实写"未接入"。
 */
export default {
  async render() {
    const cards = MODULES.filter((m) => m.id !== 'overview' && !m.utility).map((m) => `
        <div class="mod" data-go="${m.id}">
          <h3>${m.label}</h3>
          <p>${m.desc}</p>
          <div class="ents">${m.ents}</div>
        </div>`).join('');

    // 归档的项目不计进"在产"，但也不藏起来：单独一行摆出来，
    // 否则首页那个数字会让人以为项目被删了
    const archivedN = store.projects.length - store.activeProjects.length;

    return `
    <section class="view on">
      <div class="page-head">
        <h1>模块地图</h1>
        <p>平台只编排，不生成；生成全靠适配器调现有脚本</p>
        <div class="right"><span class="chip plain">已接后端：项目 / 类型</span></div>
      </div>

      <div class="mods">
        ${cards}
        <div class="mod static">
          <h3>技术栈</h3>
          <p>FastAPI + SQLite WAL + 静态前端（ES Module，无构建链）。适配器一律 CLI 子进程。</p>
          <div class="ents">/api 只返回 JSON</div>
        </div>
      </div>

      <div class="grid g-2" style="margin-top:14px">
        <div class="card">
          <div class="card-head"><h2>九实体怎么连</h2><span class="sub">主干管属于谁，支线管挂在哪</span></div>
          <div class="card-body">
            <div class="lbl">主干 · 从属关系（越往下越细）</div>
            <ul class="etree">
              <li><b>Project</b><span>项目，一切从这里开始</span>
                <ul><li><b>Episode</b><span>创作单元，一集或一卷</span>
                  <ul><li><b>Doc</b><span>文档，小说／剧本／分镜</span>
                    <ul><li><b>Shot</b><span>一条镜头，带景别与台词</span>
                      <ul><li><b>Job</b><span>一次生成，记成本和产物</span></li></ul>
                    </li></ul>
                  </li></ul>
                </li></ul>
              </li>
            </ul>

            <div class="rels">
              <div class="lbl">支线 · 引用与挂载</div>
              <div class="rel"><b>Shot</b><i>引用</i><b>Asset</b><span>角色／场景／物品</span></div>
              <div class="rel"><b>Review</b><i>挂在</i><b>Doc 或 Job</b><span>校验与人签</span></div>
              <div class="rel"><b>Knowledge</b><i>属于</i><b>Project</b><span>设定与证据快照</span></div>
              <div class="rel"><b>Task</b><i>可挂</i><b>任意实体</b><span>指派与人签</span></div>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="card-head"><h2>本机状态</h2><span class="sub">M5 Ultra 96GB · 1.2TB/s</span></div>
          <div class="card-body stack" style="gap:10px">
            <div class="row" style="justify-content:space-between">
              <span class="note">在产项目</span>
              <span class="num" style="font-size:12px">${store.activeProjects.length}</span>
            </div>
            ${archivedN ? `<div class="row" style="justify-content:space-between">
              <span class="note">已归档项目</span>
              <span class="num" style="font-size:12px">${archivedN}</span>
            </div>` : ''}
            <div class="row" style="justify-content:space-between">
              <span class="note">生成 worker</span>
              <span class="chip gray">未接入</span>
            </div>
            <div class="row" style="justify-content:space-between">
              <span class="note">队列</span>
              <span class="chip gray">未接入</span>
            </div>
            <div class="row" style="justify-content:space-between">
              <span class="note">受限模型（H3 音画同生）</span>
              <span class="chip plain">未启用</span>
            </div>
            <hr class="sep">
            <p class="note">同时最多 1 个本地作业，其余排队。云端 API 可并发提交、串行落库。</p>
          </div>
        </div>
      </div>
    </section>`;
  },

  /** 卡片点击走事件，不直接 import router，避免循环依赖 */
  mount(root) {
    root.querySelectorAll('[data-go]').forEach((el) => {
      el.addEventListener('click', () => {
        window.dispatchEvent(new CustomEvent('app:go', { detail: el.dataset.go }));
      });
    });
  },
};
