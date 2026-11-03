/** 设定中心：还没接后端，先给空状态。 */
export default {
  async render() {
    // 这个按钮以前挂了事件都没挂：点了什么也不发生，比禁用更让人怀疑平台是不是坏了。
    // 现在明说"未开放"，禁用态 + title 给原因；等表/接口落地再一起打开。
    return `
    <section class="view on">
      <div class="page-head">
        <h1>设定中心</h1><p>条目 + 证据快照，项目内与全局两级</p>
        <div class="right"><span class="chip plain">未开放</span></div>
      </div>
      <div class="card">
        <div class="empty">
          <div class="empty-t">还没有设定条目</div>
          <p>世界观条目要挂证据快照：引句逐字取自抓取的文件，不改写、不补主语，校验器会做逐字 diff。</p>
          <p class="hint">这一页目前只有入口：没有表、没有接口、也没有写入路径，先不假装能新建。</p>
          <button class="btn primary" type="button" disabled
                  title="设定中心还没有表与接口，等立项后开放">新建条目</button>
        </div>
      </div>
    </section>`;
  },
};
