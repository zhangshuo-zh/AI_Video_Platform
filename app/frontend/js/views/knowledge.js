/** 设定中心：还没接后端，先给空状态。 */
export default {
  async render() {
    return `
    <section class="view on">
      <div class="page-head">
        <h1>设定中心</h1><p>条目 + 证据快照，项目内与全局两级</p>
      </div>
      <div class="card">
        <div class="empty">
          <div class="empty-t">还没有设定条目</div>
          <p>世界观条目要挂证据快照：引句逐字取自抓取的文件，不改写、不补主语，校验器会做逐字 diff。</p>
          <button class="btn primary">新建条目</button>
        </div>
      </div>
    </section>`;
  },
};
