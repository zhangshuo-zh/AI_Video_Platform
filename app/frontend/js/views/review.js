/** 审核中心：还没接后端，先给空状态。签字只在这一页落。 */
export default {
  async render() {
    return `
    <section class="view on">
      <div class="page-head">
        <h1>审核中心</h1><p>校验器 gate + 人工签字</p>
      </div>
      <div class="card">
        <div class="empty">
          <div class="empty-t">还没有待审项</div>
          <p>校验器 gate 和人工签字都挂在文档或作业上；有内容提交后才会出现在这里。
             签字动作只在这一页做，创作中心和生成中心只显示状态、不放签字按钮。</p>
          <button class="btn">导入校验规则</button>
        </div>
      </div>
    </section>`;
  },
};
