/**
 * 音频中心：台词、配音素材、音乐条目、口型 viseme 挂接。
 * 这里没有"新建任务"按钮——队列的唯一东家是生成中心（设计稿 §3.2），
 * 音频这边只准备素材，提交动作交过去。
 */
export default {
  async render() {
    return `
    <section class="view on">
      <div class="page-head">
        <h1>音频中心</h1><p>配音 VoxCPM2 · 音乐 ACE-Step · 口型 Rhubarb 分层</p>
      </div>
      <div class="card">
        <div class="empty">
          <div class="empty-t">还没有音频素材</div>
          <p>这一页管台词、配音素材与口型 viseme 的挂接；配音走 VoxCPM2，音乐走 ACE-Step，
             口型按 Rhubarb 分层、只在口型特写保留。</p>
          <p class="hint">作业不在这边排队：素材备好后送进生成中心的队列，
             单 worker 只有一条队，两处各开一个入口迟早分叉。</p>
        </div>
      </div>
    </section>`;
  },
};
