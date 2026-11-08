/**
 * 模块清单：侧栏、命令面板、首页卡片都从这里取，避免三处各写一遍。
 * subs 是二级导航；desc/ents 只给首页卡片用。
 * desc 按设计稿 §3.2 的职责矩阵写：只说这一模块自己写什么，别的一律写成"引用"。
 *
 * 0037 起每个模块多两个字段：`group` 与 `group_label`（任务书 §11 的分组导航）。
 * 分组**只改侧栏排布**：模块 id、顺序、二级页下标一个都没动，
 * `#assets/1/a57` 这类深链照旧有效——为导航调整而删页面是任务书明确禁止的。
 * 侧栏高亮、命令面板、首页卡片仍读同一份 MODULES，不另建第二张导航表。
 */
export const GROUPS = [
  { key: 'desk', label: '工作台' },
  { key: 'prepare', label: '创作准备' },
  { key: 'produce', label: '生产执行' },
  { key: 'quality', label: '质量与交付' },
  { key: 'platform', label: '平台能力' },
];

const G = Object.fromEntries(GROUPS.map((g) => [g.key, g.label]));

export const MODULES = [
  {
    id: 'overview', label: '模块地图', icon: 'ic-overview', subs: [],
    group: 'desk', group_label: G.desk,
    desc: '', ents: '',
  },
  {
    // 0037：项目生产工作台。它是"看进度、给下一步"的一页，不写业务事实——
    // 提交仍在生成中心、签字仍在审核中心，所以它排在项目中心之前但不是任何表的家。
    // 项目工作台与项目中心原来共用 ic-projects（同一个文件夹图形），
    // 侧栏里两个相邻入口长得一模一样，只能靠读字分辨。
    // 工作台是「一集的进度与下一步」，用带勾选行的看板图形；
    // 文件夹留给项目中心——那才是管 Project 实体本身的一页。
    id: 'production', label: '项目工作台', icon: 'ic-board', subs: [],
    group: 'desk', group_label: G.desk, perms: ['project:read'],
    desc: '一个项目、一集的生产进度与下一步任务：九阶段的数字全部现算，'
      + '点一行跳到真正做那件事的页面。这里不建作业、不签字、不合成。',
    ents: '只读聚合：Episode · Shot · Job · Review · Composition',
  },
  {
    id: 'projects', label: '项目中心', icon: 'ic-projects',
    // 合并成一页：筛选、切换、增删改都在同一张表上，新建收进弹窗，所以没有二级页了
    subs: [],
    group: 'desk', group_label: G.desk,
    desc: '只管 Project 这一层：建项目、标签、成员、删除。集与分镜、花费都只显示数字。',
    ents: 'Project',
  },
  {
    id: 'assets', label: '资产中心', icon: 'ic-assets',
    // v0.98 起二级导航是**工作区**，类型（全部／角色／场景／道具）降为资产库页内的 chip。
    // 和创作中心（小说／剧本／分镜）、产线中心（运行／模板／整季）同一套做法：
    // 侧栏高亮与页内切换必须是同一个值，类型那种"筛选条件"不该占一级导航。
    // 顺序必须与 views/assets.js 的 WORKSPACES 一字不差。
    subs: ['资产库', '资产创作', '文本提取'],
    group: 'prepare', group_label: G.prepare,
    desc: '角色／场景／道具登记，定妆图落盘，一致性提示词——资产内容只在这里改。'
      + '文字设定与图像候选也在这里发起，但排队、成本与产物仍归生成中心。',
    ents: 'Asset · AssetVariant',
  },
  {
    id: 'writing', label: '创作中心', icon: 'ic-writing',
    subs: ['小说', '剧本', '分镜'],
    group: 'prepare', group_label: G.prepare,
    desc: '集与三级文档的东家：建集删集、加镜删镜都在这一页；小说剧本正文走文件 + git。',
    ents: 'Episode · Doc · Shot',
  },
  {
    // 0037：单镜头生产工作台。`hidden: true`＝不进侧栏、不进命令面板、不进首页卡片，
    // 但它是真页面，深链 `#shot/a123` 直接可达——从分镜表点「工作台」进来。
    // 为什么不挂成创作中心的第四个子页：`writing` 的地址参数已经被文档详情占了
    // （`#writing/0/a12` 是第 12 号文档），再拿同一个参数当镜头 id 就是两个含义。
    id: 'shot', label: '镜头工作台', icon: 'ic-writing', subs: [], hidden: true,
    group: 'prepare', group_label: G.prepare, perms: ['project:read'],
    desc: '一镜的生产档案：设定、引用、关键帧、视频、音频、预览、审核、能不能进合成。',
    ents: 'Shot · Job · Review',
  },
  {
    id: 'generate', label: '生成中心', icon: 'ic-generate',
    // W08 起这一页就是"队列 + 成本账 + 详情抽屉 + 参数模板"一整页：
    // 原来的三个二级标签里只有"队列"有内容，另外两个点了什么都不变。
    // 收成一个，参数模板做成页头按钮、产物进详情抽屉，不再留两个死入口。
    subs: ['队列与成本'],
    group: 'produce', group_label: G.produce,
    desc: '唯一队列、模型路由、参数模板、产物落盘，成本账的唯一出口。本地串行单 worker。',
    ents: 'Job · Task',
  },
  {
    // v0.90 阶段 C：编排层，排在生成中心之后——它读的是生成中心的事实。
    // perms 是进入门槛：job:read 就够了（viewer 也能看进度，签字与发起另按点卡）
    id: 'pipeline', label: '产线中心', icon: 'ic-pipeline',
    subs: ['运行', '模板', '整季'],
    perms: ['job:read'],
    group: 'produce', group_label: G.produce,
    desc: '单集编排与整季排产：模板、运行、节点、事件、批次与 worker 状态。干活仍交给生成中心那一个队列，签字仍归审核中心。',
    ents: 'PipelineRun · PipelineNode · SeasonRun',
  },
  {
    // v0.91 阶段 D：成片是产线的下游，读者先看"这一集什么时候能出片"。
    // 它同样只读 Job 的事实：视频、sha256、时长都来自那条合成作业。
    id: 'composition', label: '成片中心', icon: 'ic-composition',
    subs: ['成片'],
    perms: ['job:read'],
    group: 'quality', group_label: G.quality,
    desc: '把已审核的镜头按顺序合成一集：时间线快照、成片版本、sha256 与回放。合成走同一个 Job 队列。',
    ents: 'Composition',
  },
  {
    id: 'audio', label: '音频中心', icon: 'ic-audio',
    subs: ['配音', '音乐', '口型', '音效'],
    group: 'produce', group_label: G.produce,
    desc: '台词、配音素材、音乐条目、口型 viseme 挂接；作业送生成中心排队，这里不开队列。',
    ents: 'Task',
  },
  {
    id: 'review', label: '审核中心', icon: 'ic-review',
    // 二级页与页内分页一一对应（0025 起多了产物审核）：侧栏高亮和页面看到的
    // 必须是同一个值，所以这里的顺序要与 views/review.js 的 TABS 一字不差
    // 0041 起多了第三栏「动作审批」：智能体要执行的写操作在这里签。
    // 顺序仍须与 views/review.js 的 TABS 一字不差（router 按下标钳位）。
    subs: ['文档审核', '产物审核', '动作审批'],
    group: 'quality', group_label: G.quality,
    desc: '校验器 gate 与人工签字，产物验收也在这里签；签字只在这里落，别处只显示状态。',
    ents: 'Review',
  },
  {
    // 业务资料，不是运行时配置——两者曾叫 setting / settings，差一个字母
    id: 'knowledge', label: '设定中心', icon: 'ic-knowledge',
    subs: ['条目', '证据快照'],
    group: 'prepare', group_label: G.prepare,
    desc: '世界观条目与证据快照引用，抓取内容逐字留档。',
    ents: 'Knowledge',
  },
  {
    // v0.99 从系统设置里拆出来的一级菜单。放在 utility 分组、系统设置之前：
    // 它和系统设置一样不是业务模块，但管的是"模型与引擎"这件具体的事。
    // subs 的顺序必须与 views/models.js 的 SUBS 一字不差（router 会按这个下标钳位）。
    // perms 沿用 system:config——不新增权限点，18 个权限点保持原样。
    id: 'models', label: '模型中心', icon: 'ic-models',
    subs: ['模型仓库', '引擎管理', '调用策略', '运行监控'], utility: true,
    perms: ['system:config'],
    group: 'platform', group_label: G.platform,
    desc: '模型登记、引擎健康、任务调用策略与运行概览的统一入口。'
      + '规格仍以适配器清单为唯一来源；选路与排队仍归生成中心那一个队列，这里不开第二条路。',
    ents: 'ModelRegistration · ModelPolicy',
  },
  {
    // 智能体中心（0041）。它管的是"谁替人做决定、做到哪一步了"，
    // 不管模型是什么、能不能跑——那半归上面那个「模型中心」，
    // 这一页对模型只做引用＋跳转，一个字段都不复制。
    // 取证与边界写在《文档/开发/智能体管理/00-接入前现状审计.md》§7。
    //
    // 入口权限刻意用 project:read 而不是 system:config：运行是属于项目的，
    // 制作角色要能发起、要看自己那批待办；配置那一档（本地／云端两张卡、
    // 工具白名单）在页内按 store.can('system:config') 决定显不显示。
    id: 'agents', label: '智能体中心', icon: 'ic-agents',
    subs: ['总览', '智能体配置', '任务路由', '运行与审批'], utility: true,
    perms: ['project:read'],
    group: 'platform', group_label: G.platform,
    desc: '智能体配置、任务路由、运行记录与待审批动作的统一入口。'
      + '它只负责规划与调用白名单工具；排队、成本、产物仍归生成中心，签字仍归审核中心。',
    ents: 'AgentRun · AgentStep · AgentEvent（配置与模型一律引用模型中心，不另存一份）',
  },
  {
    // 不是业务模块，是系统级配置；侧栏里单独放在最下面。
    // v0.99 模型那一页搬去「模型中心」；v0.111 剩下的「用户与角色」再拆成两页——
    // 加人是偶尔一次，调角色权限是一次长时间对照，挤在一页时滚到矩阵就看不见用户表。
    // subs 的顺序必须与 views/system-config.js 的 SUBS 一字不差（router 按下标钳位）。
    // perms 仍带 system:config：万一有人粘进旧地址，也要能落到"这一页已经搬走"的说明上
    id: 'system-config', label: '系统设置', icon: 'ic-system',
    subs: ['用户管理', '角色管理'], utility: true,
    perms: ['system:config', 'user:read'],
    group: 'platform', group_label: G.platform,
    desc: '账号与权限：用户管理（建人、挂角色、口令、删除）与角色管理'
      + '（角色概览与 18 个权限点矩阵）。模型与引擎的配置已搬到「模型中心」。',
    ents: '系统',
  },
];
