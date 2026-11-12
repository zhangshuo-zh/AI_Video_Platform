/**
 * 模型中心（v0.99，0035）——**入口文件**，只做编排，不写渲染与交互。
 *
 * 四个工作区：
 *   0 模型仓库   清单 x 登记 的联表：这台机器上到底有哪些档、各是什么状态
 *   1 引擎管理   能力轨十行（从系统设置搬来，一行功能没减）＋ 按引擎分组的健康
 *   2 调用策略   九个任务场景 -> 必需能力 / 建议轨 / 偏好 / 备用轨
 *   3 运行监控   队列、worker、成本、最近失败——全部现读，不复制 Job 的事实
 *
 * 三条贯穿全模块的边界，都是为了让「模型中心」不长出第二个执行系统：
 *
 * 1. **规格不入库**。参数范围、计价、出处一律来自 GET /api/providers/manifest
 *    （后端 adapters.py）。这一页没有「新建模型规格」这回事，只有「登记一个实例」。
 * 2. **选路不在这里**。默认轨的唯一来源是能力轨那一列打勾（provider_config.is_default），
 *    策略页的备用轨只是「可改选」的提示——后端 resolve_provider() 不补位、不兜底。
 * 3. **状态不自己拼**。健康、验证、生产可用、队列计数全部读服务端算好的字段。
 *
 * 2026-11-08 拆分（原 2277 行 → 5 份）：
 *   shared.js     缓存、常量、跨子页纯函数
 *   repo.js       0 模型仓库
 *   engines.js    1 引擎管理 ＋ 1b 引擎实例
 *   policies.js   2 调用策略
 *   monitor.js    3 运行监控
 */
import { api } from '../api.js';
import { store } from '../store.js';
import {
  bindModalDismiss, bindSubChips, closeModal, escapeHtml, run, subChipRow,
} from '../util.js';
import { getSub, setSub } from '../navstate.js';
import {
  SUBS, SUB_NEEDS, cache, MODALS, overviewCard, setPendingFilter, viewFilter, alertNotify,
} from './models/shared.js';
import { pageRepo, mountRepo, openForm } from './models/repo.js';
import { pageEngines, mountEngines, mountEngineInstances,
  openProbeResults } from './models/engines.js';
import { pagePolicies, mountPolicies } from './models/policies.js';
import { pageMonitor, mountMonitor } from './models/monitor.js';

/* ================= 数据装载 ================= */
async function loadAll(sub) {
  try {
    // 顶部四数跟着 `GET /api/models` 一起回来（后端 `stats(rows)` 用的就是同一份 rows）。
    // 所以这里不再单独要 `/api/models/stats`：那一次请求会把整张目录连同每一行的
    // 引擎探测重算一遍，而算出来的数字必须与下面这张表逐行对得上。
    // 老地址还在（命令行与验收脚本按它取数），只是这一页不走它了。
    //
    // 引擎实例只在「引擎管理」那一屏取：`GET /api/engines` 会**逐台真发 HTTP 探测**，
    // 四个子页都取等于把没人看的那一屏也探一遍。切到别的屏时它是 null，
    // 那一屏根本不读它。
    // **按子页取**：四个子页各需要的东西不一样，一次全取等于每切一次标签页
    // 都打七个接口（其中三个只有别的子页会读）。桌面端看不出来，本机接口
    // 一慢就能感觉到"点一下标签要等一秒"。
    //
    // 公共底座（两个下拉的取值、状态译名、顶部四数）只有目录一条接口——
    // 它已经带 stats，不再单独要 `/api/models/stats`（那一次会把整张目录重算一遍）。
    //
    // 目录**不出网**（后端 2026-11-07 起）：服务型引擎那一格在列表里是"待现查"，
    // 要现查请按页头的「检查引擎连接」或那一行的「探测」。
    const need = SUB_NEEDS[sub] || SUB_NEEDS[0];
    const [catalog, manifest] = await Promise.all([api.models(), api.providerManifest()]);
    const [providers, policies, worker, validations, engines,
      workerProcess] = await Promise.all([
      need.providers ? api.providers() : null,
      need.policies ? api.modelPolicies() : null,
      need.worker ? api.workerStatus() : null,
      need.validations ? api.providerValidations(50) : null,
      need.engines ? api.engines() : null,
      // 进程控制面：要 system:config 的人才有；403 时降级为 undefined
      // （卡片自己会显示"未运行"，但按钮点了会报权限不足——那是真实情况，
      //  不在前端假装成"没有这个功能"）。
      need.process ? api.workerProcess().catch(() => null) : null,
    ]);
    const workerDaemon = need.process ? await api.workerDaemon().catch(() => null) : null;
    // 兼容两种响应：后端给了 stats 就用它；万一部署的是老版本（没有这个键），
    // 再退回单独取一次——宁可多一次请求，也不能让顶部四数空着。
    const stats = catalog.stats || await api.modelStats();
    // **必须逐字段写回，不能整对象重新赋值。**
    // cache 是 shared.js 里 export let 导出的：ESM 的 import 绑定指向声明本身，
    // 不是快照——这里若写 cache = {...}，只有本模块看到新对象，
    // repo/engines/policies/monitor 拿到的仍是旧的空缓存（拆完第一版就是这样：
    // 表里 0 行、概览四个数全 0，而接口明明回了 7 档）。
    Object.assign(cache, {
              catalog, manifest,
              providers: providers || cache.providers || [],
              policies: policies || cache.policies || { items: [], preference_labels: {}, no_auto_fallback: true },
              worker: worker || cache.worker || {},
              workerProcess: workerProcess || cache.workerProcess || {},
              workerDaemon: workerDaemon || cache.workerDaemon || {},
              // 验证历史是分页读的（一页 + 总量），缓存里存整个对象而不是数组：
              // 只存数组会把总量丢掉，监控页就只能说"这里有 20 条"，说不出"共多少条"。
              validations: validations || cache.validations || { items: [], total: 0 },
              // 整页重载＝回到第一页。翻页是模块内状态，不该跟着刷新一起漂。
              validationPage: validations ? { before: null, pages: 0 } : cache.validationPage,
      stats, engines,
    });
    return { error: '' };
  } catch (err) {
    // 部分失败不整页挡死：登记读不到时至少还能看能力轨。真读不到才给空态。
    //
    // 但**降级必须说出来**：上一版在这里造了一份"全 0"的 stats 悄悄顶上，
    // 顶部四数就变成"已登记 0 / 可用 0 / 引擎 0"，看着像一台干净机器，
    // 而真相是目录接口挂了。现在两条都改：造出来的 stats 带上 caliber
    // （overviewCard 要读它，缺了会抛 TypeError 把整页钉在上一屏），
    // 并且打上 degraded 标记，由概览卡自己挂一条黄条。
    try {
      const [providers, manifest] = await Promise.all([api.providers(), api.providerManifest()]);
      cache.providers = providers; cache.manifest = manifest;
      cache.catalog = { items: [], capabilities: {}, status_labels: {},
                        unintegrated_engines: [] };
      // 这几个 0 是"空位"不是"事实"：口径那一句必须跟着数字一起给，
      // 否则界面只能猜哪一格是降级值。形状与真响应一致（键一个不少），
      // 少一个键就会把 "undefined" 印在顶上。
      // 这几个 0 是"空位"不是"事实"：每格的口径那一句必须跟着数字一起给。
      // 形状与真响应逐键一致——少一个键，概览卡就会把 undefined 加到数字上。
      const caliber = {
        registered_total: '目录接口没读回来，这个 0 不是"没有登记"',
        available_total: '同上：这一格现在是空位，不是真的 0',
        engine_total: '同上：引擎数来自目录，目录没回就是 0',
      };
      caliber.queued = '同上：队列数来自目录，目录没回就是 0';
      cache.stats = {
        registered_total: 0, available_total: 0, engine_total: 0,
        engine_params_known: 0, unintegrated_engines: [],
        needs_attention: 0, running: 0, registered_by_capability: {},
        degraded: err.message || String(err), caliber,
      };
      cache.stats.queued = 0;
      cache.policies = { items: [], preference_labels: {}, no_auto_fallback: true };
      cache.engines = null;
      return { error: '' };
    } catch (e2) {
      return { error: e2.message || err.message };
    }
  }
}


export default {
  async render() {
    const sub = getSub('models') ?? 0;
    this.subIndex = sub;
    if (!store.can('system:config')) {
      return `<section class="view on">
        <div class="page-head"><h1>模型中心</h1><p>${SUBS[sub]}</p>
          <div class="right">${subChipRow(SUBS, sub)}</div></div>
        <div class="card"><div class="empty">
          <div class="empty-t">没有权限</div>
          <p>模型中心只有管理员能打开。要看现在走哪条模型轨，
             生成中心的发起弹窗里就写着；要改，找管理员。</p>
        </div></div></section>`;
    }

    const load = await loadAll(sub);
    if (load.error) {
      return `<section class="view on">
        <div class="page-head"><h1>模型中心</h1><p>${SUBS[sub]}</p></div>
        <div class="card"><div class="empty">
          <div class="empty-t">读不到模型数据</div><p>${escapeHtml(load.error)}</p>
        </div></div></section>`;
    }

    const body = sub === 0 ? pageRepo() : sub === 1 ? pageEngines()
      : sub === 2 ? pagePolicies() : pageMonitor();
    const s = cache.stats;

    return `
    <section class="view on">
      <div class="page-head">
        <h1>模型中心</h1>
        <p>模型登记、引擎真相、调用策略与运行概览；选路与排队仍归生成中心</p>
        <div class="right">
          ${subChipRow(SUBS, sub)}
          <!-- 「查看运行异常」原来在这儿，做的事只是 setSub(3)，等于一颗跳转链接。
               跳转不该占页头一等位置，已挂到概览卡「任务」那格的可点数字上。 -->
          <button class="btn" id="mc-check-all" title="现查一次服务型引擎，再逐条跑环境检查">检查引擎连接</button>
          <button class="btn primary" id="mc-add">登记模型</button>
        </div>
      </div>
      ${overviewCard(s)}
      ${body}
      ${MODALS}
    </section>`;
  },

  mount(root) {
    bindSubChips(root, (i) => {
      setSub('models', i);
      window.dispatchEvent(new CustomEvent('app:refresh'));
    });

    /* ---------- 概览带：四个数点得下去（一处派发，四个子页都在顶部） ----------
       registered／available／attention 的行在模型仓库那张表里：当前就在
       那一屏就就地筛；在别的子页点的就先存意图再切回 sub 0 消费。
       engines／tasks 本表没有对应的行（adapter 轴与队列），跳真能看见
       它们的子页——硬筛一张空表比不筛更坏。 */
    root.querySelectorAll('[data-stlink]').forEach((el) => {
      el.addEventListener('click', (e) => {
        // 「N 个要看一眼」嵌在「可用」格里，两层都带 data-stlink：
        // 不挡冒泡的话，点徽章会连点整格——attention 筛完又立刻被
        // available 覆掉，徽章等于白做。只有**最深的那一层**响应。
        if (e.target.closest('[data-stlink]') !== el) return;
        // 「任务」格里那颗「运行监控 →」按钮自己已经绑了同一条跳转：
        // 它冒泡上来会再派发一次 app:refresh，白跑一趟装载。让按钮自己走。
        if (e.target.closest('#mc-goto-monitor')) return;
        const kind = el.dataset.stlink;
        if (kind === 'engines') {
          setSub('models', 1);
          window.dispatchEvent(new CustomEvent('app:refresh'));
          return;
        }
        if (kind === 'tasks') {
          setSub('models', 3);
          window.dispatchEvent(new CustomEvent('app:refresh'));
          return;
        }
        if ((getSub('models') ?? 0) === 0 && viewFilter) { viewFilter(kind)(); return; }
        // 不在模型仓库那一屏：mountRepo 还没跑，把意图交给下一次装载
        setPendingFilter(kind);
        setSub('models', 0);
        window.dispatchEvent(new CustomEvent('app:refresh'));
      });
    });

    /* ---------- 页头三个入口 ---------- */
    root.querySelector('#mc-add')?.addEventListener('click', () => openForm(null));
    root.querySelector('#mc-goto-monitor')?.addEventListener('click', () => {
      setSub('models', 3); window.dispatchEvent(new CustomEvent('app:refresh'));
    });
    root.querySelector('#mc-check-all')?.addEventListener('click', async () => {
      const filled = cache.providers.filter((r) => r.provider);
      if (!filled.length) { alertNotify('十条能力轨都还没填 provider，没有可检查的对象。', 'warn'); return; }
      const btn = root.querySelector('#mc-check-all');
      btn.disabled = true;
      let engineList = null;
      let cloudRows = [];
      const ok = await run('环境检查', async () => {
        // 先请后端**现查**一次服务型引擎（fresh=1 绕过缓存）：打开页面时那一格是
        // "待现查"，只有显式动作才去连网——这一颗按钮就是那个显式动作。
        // 它只读、不建 Job、不改配置，结论随整张目录一起回来。
        // 现在它还替云端轨各去供应商网关问一次（`cloud` 段，见 cloud_probe.py）：
        // 按钮原来只现查本机，点一下云端一格不动，用起来怪。
        const probeRes = await api.probeModels(true);
        cloudRows = probeRes?.cloud || [];
        // 再取一遍引擎实例列表：`GET /api/engines` 逐台按**它自己的端点**
        // 现查一次（列表档 0.8 秒），这才是"引擎列表通不通"的权威口径。
        // 不用 probeModels 的 probed：那个数组按 engine_code 去重，
        // 同一家登记了两台机器时只看得到第一台，另一台的死活它不知道。
        // （云端段不受这条影响：云端引擎不是实例表里的行，只有这一路有结论。）
        engineList = await api.engines();
        // 逐条走和列表页同一个接口：不发请求、不加载权重，只留痕
        for (const r of filled) {
          await api.validateProvider({ capability: r.capability, backend: r.backend,
                                       provider: r.provider, kind: 'env_check' });
        }
      });
      btn.disabled = false;
      if (ok) {
        window.dispatchEvent(new CustomEvent('app:refresh'));
        // 弹窗放在重绘**之后**：整页重绘会重建 DOM，先弹窗会被下一次装载
        // 的骨架闪一下；重绘完再 append 到 body 的弹窗不受影响。
        openProbeResults(engineList, cloudRows);
      }
    });

    // 关闭统一走 closeModal：它先播 150ms 退场再提交 hidden，
    // 不然关闭是"啪"地消失、和打开那两层动画不对称。
    root.querySelectorAll('[data-close]').forEach((b) => b.addEventListener('click', () => {
      closeModal(root.querySelector('#' + b.dataset.close));
    }));
    root.querySelectorAll('.modal').forEach((m) => {
      m.addEventListener('click', (e) => { if (e.target === m) closeModal(m); });
      bindModalDismiss(m, () => closeModal(m));
    });

    const sub = getSub('models') ?? 0;
    if (sub === 0) mountRepo(root);
    // 引擎管理子页只挂自己的两块。运行时路径与供应商密钥已搬到
    // 「系统设置 · 参数管理」（2026-11-11），挂载点也跟着搬走了——
    // 留在这一页会挂到一个不存在的卡上（querySelector 返回 null，静默跳过），
    // 看起来"没报错"，实际是两块配置都不响应。
    if (sub === 1) { mountEngines(root); mountEngineInstances(root); }
    if (sub === 2) mountPolicies(root);
    if (sub === 3) return mountMonitor(root);
  },
};
