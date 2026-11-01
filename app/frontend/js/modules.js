/**
 * 模块清单：侧栏、命令面板、首页卡片都从这里取，避免三处各写一遍。
 * subs 是二级导航；desc/ents 只给首页卡片用。
 * desc 按设计稿 §3.2 的职责矩阵写：只说这一模块自己写什么，别的一律写成"引用"。
 */
export const MODULES = [
  {
    id: 'overview', label: '模块地图', icon: 'ic-overview', subs: [],
    desc: '', ents: '',
  },
  {
    id: 'projects', label: '项目中心', icon: 'ic-projects',
    // 合并成一页：筛选、切换、增删改都在同一张表上，新建收进弹窗，所以没有二级页了
    subs: [],
    desc: '只管 Project 这一层：建项目、标签、成员、删除。集与分镜、花费都只显示数字。',
    ents: 'Project',
  },
  {
    id: 'assets', label: '资产中心', icon: 'ic-assets',
    // 顺序必须和 views/assets.js 的 KINDS 一字不差——之前这里是"物品／场景"反着写，
    // 于是侧栏高亮"物品"时表格里摆的其实是场景（同一个状态两个真相源的下场）
    subs: ['全部', '角色', '场景', '道具'],
    desc: '角色／场景／道具登记，定妆图落盘，一致性提示词——资产内容只在这里改。',
    ents: 'Asset',
  },
  {
    id: 'writing', label: '创作中心', icon: 'ic-writing',
    subs: ['小说', '剧本', '分镜'],
    desc: '集与三级文档的东家：建集删集、加镜删镜都在这一页；小说剧本正文走文件 + git。',
    ents: 'Episode · Doc · Shot',
  },
  {
    id: 'generate', label: '生成中心', icon: 'ic-generate',
    // W08 起这一页就是"队列 + 成本账 + 详情抽屉 + 参数模板"一整页：
    // 原来的三个二级标签里只有"队列"有内容，另外两个点了什么都不变。
    // 收成一个，参数模板做成页头按钮、产物进详情抽屉，不再留两个死入口。
    subs: ['队列与成本'],
    desc: '唯一队列、模型路由、参数模板、产物落盘，成本账的唯一出口。本地串行单 worker。',
    ents: 'Job · Task',
  },
  {
    id: 'audio', label: '音频中心', icon: 'ic-audio',
    subs: ['配音', '音乐', '口型', '音效'],
    desc: '台词、配音素材、音乐条目、口型 viseme 挂接；作业送生成中心排队，这里不开队列。',
    ents: 'Task',
  },
  {
    id: 'review', label: '审核中心', icon: 'ic-review',
    subs: ['校验结果', '人工签字'],
    desc: '校验器 gate 与人工签字，签字只在这里落，别处只显示状态。',
    ents: 'Review',
  },
  {
    // 业务资料，不是运行时配置——两者曾叫 setting / settings，差一个字母
    id: 'knowledge', label: '设定中心', icon: 'ic-knowledge',
    subs: ['条目', '证据快照'],
    desc: '世界观条目与证据快照引用，抓取内容逐字留档。',
    ents: 'Knowledge',
  },
  {
    // 不是业务模块，是系统级配置；侧栏里单独放在最下面。
    // perms 是"任一即可"的进入门槛：管理员看得到两个页，
    // 只有 user:read 的人也能进来看用户与角色（模型配置那页会自己挡回去）
    id: 'system-config', label: '系统设置', icon: 'ic-system',
    subs: ['模型与密钥', '用户与角色'], utility: true,
    perms: ['system:config', 'user:read'],
    desc: '运行时配置：模型双轨、密钥状态、用户与角色权限。',
    ents: '系统',
  },
];
