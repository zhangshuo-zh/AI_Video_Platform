/**
 * 生成中心：唯一队列 + 成本账的唯一出口。
 * 音频中心不另开队列（单 worker 物理上就一条），它只管素材，提交动作落在这里。
 */
export default {
  async render() {
    return `
    <section class="view on">
      <div class="page-head">
        <h1>生成中心</h1><p>唯一队列 · 本地优先 · 云端按镜头勾选</p>
        <div class="right"><button class="btn">参数模板</button></div>
      </div>

      <div class="card">
        <div class="empty">
          <div class="empty-t">队列是空的</div>
          <p>到分镜里挑一条镜头提交作业。本地一次只跑一个，云端可并发提交、串行落库。
             配音与音乐也排这一条队，音频中心不另开队列。</p>
          <button class="btn primary">去分镜选镜头</button>
          <p class="hint">提交前先跑一遍基准清单，速度数字才是真的。</p>
        </div>
      </div>

      <div class="card" style="margin-top:14px">
        <div class="card-head"><h2>成本账</h2><span class="sub">由 Job 汇总，不可手改</span></div>
        <div class="empty">
          <div class="empty-t">还没有花费</div>
          <p>第一条作业跑完，这里就会按适配器和模型自动汇总出账目。项目中心只显示累计数，明细只在这一页。</p>
        </div>
        <div class="card-body" style="border-top:1px solid var(--line);padding:10px 14px">
          <p class="note"><b>口径</b>：Job 记单价 × 量，本表只汇总、不能手改。本地作业记 0 元，但保留机时——它是判断"产能溢出该不该转云端"的依据。</p>
        </div>
      </div>
    </section>`;
  },
};
