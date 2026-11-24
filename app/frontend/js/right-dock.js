/**
 * 右侧悬浮控制台：X3-UI 风格系统资源仪表盘 + 产物缓存安全清理 + 实时作业队列。
 * 平时完全隐藏在屏幕右边缘，仅在垂直中间带靠近时滑出，收起后立即停止轮询。
 */
import { api } from './api.js';
import { store } from './store.js';
import { escapeHtml } from './util.js';
import { CAPS } from './enums.js';

const JOB_STATUS = {
  queued: ['排队中', 'amber'],
  running: ['运行中', 'blue'],
  succeeded: ['已完成', 'green'],
  failed: ['失败', 'red'],
  canceled: ['已取消', ''],
};

function fmtBytes(bytes) {
  const n = Number(bytes || 0);
  if (n >= 1024 ** 3) return `${(n / (1024 ** 3)).toFixed(1)} GB`;
  if (n >= 1024 ** 2) return `${Math.round(n / (1024 ** 2))} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

function toneOf(pct, disabled) {
  if (disabled) return 'muted';
  if (pct >= 85) return 'danger';
  if (pct >= 65) return 'warn';
  return 'ok';
}

function renderGauge({ label, pct, sub, disabled = false }) {
  const clamped = Math.max(0, Math.min(100, Number(pct || 0)));
  const circ = 113.1;
  const offset = disabled ? circ : (circ * (1 - clamped / 100)).toFixed(2);
  const tone = toneOf(clamped, disabled);
  const pctText = disabled ? '--' : `${Math.round(clamped)}%`;
  return `<div class="rd-gauge tone-${tone}">`
    + `<div class="rd-ring">`
    + `<svg viewBox="0 0 48 48" aria-hidden="true">`
    + `<circle class="rd-ring-bg" cx="24" cy="24" r="18"/>`
    + `<circle class="rd-ring-fg" cx="24" cy="24" r="18" stroke-dasharray="${circ}" stroke-dashoffset="${offset}"/>`
    + `</svg>`
    + `<span class="rd-ring-val">${pctText}</span>`
    + `</div>`
    + `<div class="rd-g-meta"><div class="rd-g-name">${escapeHtml(label)}</div>`
    + `<div class="rd-g-sub" title="${escapeHtml(sub)}">${escapeHtml(sub)}</div></div>`
    + `</div>`;
}

export function setupRightQueueDock({ isReady, onNavigate }) {
  const edge = document.getElementById('right-dock-edge');
  const dock = document.getElementById('right-dock');
  const gaugesEl = document.getElementById('rd-gauges');
  const cacheEl = document.getElementById('rd-cache');
  const platEl = document.getElementById('rd-sys-plat');
  const listEl = document.getElementById('rd-list');
  const goBtn = document.getElementById('rd-go-gen');
  const tabsEl = document.getElementById('rd-tabs');
  const statsEl = document.getElementById('rd-stats');
  if (!edge || !dock || !listEl || !gaugesEl || !cacheEl) return;

  const TAB_KEY = 'fv.rdTab';
  let activeTab = 'jobs';
  try {
    const saved = localStorage.getItem(TAB_KEY);
    if (saved === 'procs' || saved === 'jobs') activeTab = saved;
  } catch { /* ignore */ }

  let closeTimer = 0;
  let idleTimer = 0;
  let pollTimer = 0;
  let visBound = false;
  let dockHover = false;
  let loadingJobs = false;
  let loadingProcs = false;
  let loadingSys = false;
  let confirmClear = false;
  let clearing = false;
  let clearFeedback = '';
  let lastSys = null;

  const isEnabled = () => window.innerWidth > 860 && isReady();

  // 初始化即填好占位骨架，确保初次从屏幕正中滑出时高度已稳定，不发生上下跳闪
  gaugesEl.innerHTML = ['CPU', '内存', 'Swap', '磁盘']
    .map((label) => renderGauge({ label, pct: 0, sub: '采样中…', disabled: true }))
    .join('');
  cacheEl.innerHTML = `<div class="rd-cache-info"><div class="rd-cache-t">产物缓存</div>`
    + `<div class="rd-cache-s">等待采样…</div></div>`;

  const renderCacheBar = (cache) => {
    if (!cache) {
      cacheEl.innerHTML = '';
      return;
    }
    const totalStr = fmtBytes(cache.total_bytes);
    const reclaimBytes = Number(cache.reclaimable_bytes || 0);
    const reclaimStr = fmtBytes(reclaimBytes);
    const cnt = Number(cache.candidate_count || 0);
    const infoLine = reclaimBytes > 0
      ? `总计 ${totalStr} · 可清 ${reclaimStr}（${cnt} 项）`
      : `总计 ${totalStr} · 暂无冗余废片`;

    let actionHtml = '';
    if (clearFeedback) {
      actionHtml = `<span class="rd-cache-msg">${escapeHtml(clearFeedback)}</span>`;
    } else if (!cache.can_clear) {
      actionHtml = `<span class="rd-cache-ro">只读 · 可汇报管理员</span>`;
    } else if (clearing) {
      actionHtml = `<button type="button" class="btn sm" disabled>清理中…</button>`;
    } else if (confirmClear) {
      actionHtml = `<div class="rd-cache-acts">`
        + `<button type="button" class="btn sm danger" data-gc="confirm">确认清理 ${escapeHtml(reclaimStr)}</button>`
        + `<button type="button" class="btn sm" data-gc="cancel">取消</button>`
        + `</div>`;
    } else {
      actionHtml = `<button type="button" class="btn sm" data-gc="ask"${reclaimBytes <= 0 ? ' disabled' : ''}>清空缓存</button>`;
    }

    cacheEl.innerHTML = `<div class="rd-cache-info">`
      + `<div class="rd-cache-t">产物缓存</div>`
      + `<div class="rd-cache-s">${escapeHtml(infoLine)}</div>`
      + `</div>`
      + `<div class="rd-cache-right">${actionHtml}</div>`;
  };

  const renderResources = (res) => {
    if (!res) return;
    lastSys = res;
    if (platEl) {
      const pName = res.platform === 'Darwin' ? 'macOS' : res.platform;
      platEl.textContent = `${pName} · ${res.cpu?.cores || 1} 核`;
    }
    const fmtPair = (used, total) => {
      const a = fmtBytes(used);
      const b = fmtBytes(total);
      const [va, ua] = a.split(' ');
      const [vb, ub] = b.split(' ');
      return ua === ub ? `${va}/${vb} ${ub}` : `${a}/${b}`;
    };
    const swapTotal = Number(res.swap?.total_bytes || 0);
    const memSub = res.memory?.total_bytes
      ? fmtPair(res.memory.used_bytes, res.memory.total_bytes)
      : '未读取';
    const swapSub = swapTotal > 0
      ? fmtPair(res.swap.used_bytes, swapTotal)
      : '未启用';
    const diskSub = res.disk?.total_bytes
      ? `余 ${fmtBytes(res.disk.free_bytes)}`
      : '未读取';

    gaugesEl.innerHTML = [
      renderGauge({
        label: 'CPU',
        pct: res.cpu?.percent || 0,
        sub: `负载 ${Number(res.cpu?.load_1m || 0).toFixed(1)}`,
      }),
      renderGauge({
        label: '内存',
        pct: res.memory?.percent || 0,
        sub: memSub,
      }),
      renderGauge({
        label: 'Swap',
        pct: res.swap?.percent || 0,
        sub: swapSub,
        disabled: swapTotal <= 0,
      }),
      renderGauge({
        label: '磁盘',
        pct: res.disk?.percent || 0,
        sub: diskSub,
      }),
    ].join('');
    renderCacheBar(res.cache);
  };

  const refreshResources = async () => {
    if (!isEnabled() || loadingSys) return;
    loadingSys = true;
    try {
      const res = await api.systemResources();
      renderResources(res);
    } catch (err) {
      if (!lastSys) {
        if (platEl) platEl.textContent = '监控暂不可用';
        gaugesEl.innerHTML = `<div class="rd-empty">读取硬件状态失败：${escapeHtml(err?.message || '请稍后重试')}</div>`;
      }
    } finally {
      loadingSys = false;
    }
  };

  const renderJobs = (items) => {
    if (!items || !items.length) {
      listEl.innerHTML = '<div class="rd-empty">暂无最近作业记录</div>';
      return;
    }
    listEl.innerHTML = items.map((j) => {
      const [stLabel, stTone] = JOB_STATUS[j.status] || [String(j.status || '未知'), ''];
      const capLabel = CAPS[j.capability] || j.capability || '任务';
      const prompt = (j.prompt_snapshot || '').trim() || `#${j.id} · ${j.model_code || '默认模型'}`;
      return `<button type="button" class="rd-item" data-job-id="${j.id}">`
        + `<div class="rd-item-top">`
        + `<span class="pill ${stTone}">${escapeHtml(stLabel)}</span>`
        + `<span class="rd-cap">${escapeHtml(capLabel)} · #${j.id}</span>`
        + `</div>`
        + `<div class="rd-prompt">${escapeHtml(prompt)}</div>`
        + `</button>`;
    }).join('');
  };

  const refreshJobs = async () => {
    if (!isEnabled() || loadingJobs) return;
    if (activeTab !== 'jobs') return;
    if (!store.can('job:read')) {
      listEl.innerHTML = '<div class="rd-empty">当前账号无作业读取权限</div>';
      return;
    }
    loadingJobs = true;
    try {
      const { items } = await api.jobs({ limit: 6 });
      if (activeTab === 'jobs') renderJobs(items);
    } catch (err) {
      if (activeTab === 'jobs') {
        listEl.innerHTML = `<div class="rd-empty">读取队列失败：${escapeHtml(err.message || '')}</div>`;
      }
    } finally {
      loadingJobs = false;
    }
  };

  const renderProcesses = (w) => {
    if (activeTab !== 'procs') return;
    if (!w) {
      listEl.innerHTML = `<div class="rd-empty">${escapeHtml(store.workerError || '暂未获取到进程状态')}</div>`;
      return;
    }
    const workers = Array.isArray(w.workers) ? w.workers : [];
    const liveList = workers.filter((x) => x.online);
    const liveCount = Number(w.live_count ?? liveList.length);
    const histCount = Number(w.history_count || 0);
    const alerts = Array.isArray(w.alerts) ? w.alerts : [];
    if (statsEl) {
      const pills = [`<span class="pill ${liveCount > 0 ? 'green' : 'red'}">在线 ${liveCount}</span>`];
      if (histCount > 0) pills.push(`<span class="pill">离线记录 ${histCount}</span>`);
      if (alerts.length > 0) pills.push(`<span class="pill red">告警 ${alerts.length}</span>`);
      statsEl.innerHTML = pills.join('');
    }
    const blocks = [];
    alerts.slice(0, 2).forEach((al) => {
      const msg = al.message || al.title || al.code || '队列状态告警';
      blocks.push(`<div class="rd-alert">${escapeHtml(msg)}</div>`);
    });
    if (!liveList.length) {
      blocks.push('<div class="rd-empty">暂无在线 Worker 进程（排队作业不会自动执行）</div>');
    } else {
      liveList.forEach((node) => {
        const pidTxt = node.pid ? `PID ${node.pid}` : (node.worker_id || 'Worker');
        const busyJob = node.current_job_id;
        const badgeCls = busyJob ? 'blue' : 'green';
        const badgeTxt = busyJob ? `运行 #${busyJob}` : '待命中';
        const ageTxt = node.age_seconds != null ? `心跳 ${node.age_seconds}s 前` : '在线';
        const widTxt = node.worker_id && node.pid ? `${node.worker_id} · ${ageTxt}` : ageTxt;
        blocks.push(
          `<button type="button" class="rd-item" data-proc-id="${escapeHtml(node.worker_id || '')}">`
          + `<div class="rd-item-top">`
          + `<span class="pill ${badgeCls}">${escapeHtml(badgeTxt)}</span>`
          + `<span class="rd-cap">${escapeHtml(pidTxt)}</span>`
          + `</div>`
          + `<div class="rd-prompt">${escapeHtml(widTxt)}</div>`
          + `</button>`,
        );
      });
    }
    listEl.innerHTML = blocks.join('');
  };

  const refreshProcesses = async () => {
    if (!isEnabled() || loadingProcs || activeTab !== 'procs') return;
    loadingProcs = true;
    try {
      await store.loadWorker();
      renderProcesses(store.worker);
    } finally {
      loadingProcs = false;
    }
  };

  const syncTabs = () => {
    if (tabsEl) {
      tabsEl.querySelectorAll('[data-rd-tab]').forEach((b) => {
        const on = b.dataset.rdTab === activeTab;
        b.classList.toggle('on', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
      });
    }
    if (goBtn) {
      goBtn.textContent = (activeTab === 'procs' && store.can('system:config')) ? '运行监控' : '生成中心';
    }
    if (activeTab === 'jobs' && statsEl) {
      const q = store.worker?.queue || { queued: 0, running: 0 };
      const busy = (q.queued + q.running) > 0;
      statsEl.innerHTML = busy
        ? `<span class="pill blue">运行中 ${q.running}</span><span class="pill">排队 ${q.queued}</span>`
        : '<span class="pill">队列空闲</span>';
    }
  };

  syncTabs();

  const refreshAll = () => {
    refreshResources();
    if (activeTab === 'procs') refreshProcesses();
    else refreshJobs();
  };

  const onVisibilityChange = () => {
    if (document.hidden || !dock.classList.contains('open')) return;
    refreshAll();
  };

  const stopPoll = () => {
    clearInterval(pollTimer);
    pollTimer = 0;
    if (visBound) {
      document.removeEventListener('visibilitychange', onVisibilityChange);
      visBound = false;
    }
  };

  const closeDock = () => {
    clearTimeout(closeTimer); closeTimer = 0;
    clearTimeout(idleTimer); idleTimer = 0;
    stopPoll();
    confirmClear = false;
    dock.classList.remove('open');
    dockHover = false;
  };

  const bumpIdle = () => {
    clearTimeout(idleTimer);
    if (!dock.classList.contains('open')) return;
    idleTimer = setTimeout(() => {
      if (!clearing) closeDock();
    }, 8000);
  };

  const scheduleClose = (delay = 340) => {
    clearTimeout(closeTimer);
    if (clearing || confirmClear) return;
    closeTimer = setTimeout(() => {
      if (!dockHover && !clearing && !confirmClear) closeDock();
    }, delay);
  };

  const openDock = () => {
    if (!isEnabled()) return;
    clearTimeout(closeTimer); closeTimer = 0;
    const wasOpen = dock.classList.contains('open');
    if (!wasOpen) {
      if (!lastSys) {
        gaugesEl.innerHTML = ['CPU', '内存', 'Swap', '磁盘']
          .map((label) => renderGauge({ label, pct: 0, sub: '采样中…', disabled: true }))
          .join('');
      }
      if (!listEl.children.length) {
        listEl.innerHTML = `<div class="rd-empty">${activeTab === 'procs' ? '正在读取进程状态…' : '正在读取最新队列…'}</div>`;
      }
      syncTabs();
      dock.classList.add('open');
      refreshAll();
      stopPoll();
      pollTimer = setInterval(() => {
        if (!dock.classList.contains('open') || document.hidden) return;
        refreshAll();
      }, 3000);
      if (!visBound) {
        document.addEventListener('visibilitychange', onVisibilityChange);
        visBound = true;
      }
    }
    bumpIdle();
  };

  cacheEl.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-gc]');
    if (!btn || clearing) return;
    const act = btn.dataset.gc;
    bumpIdle();
    if (act === 'ask') {
      confirmClear = true;
      renderCacheBar(lastSys?.cache);
      return;
    }
    if (act === 'cancel') {
      confirmClear = false;
      renderCacheBar(lastSys?.cache);
      return;
    }
    if (act === 'confirm') {
      confirmClear = false;
      clearing = true;
      renderCacheBar(lastSys?.cache);
      try {
        const res = await api.systemGcExecute({});
        clearFeedback = res.reclaimed_bytes > 0
          ? `已释放 ${fmtBytes(res.reclaimed_bytes)}`
          : '没有可删废片';
        await refreshResources();
      } catch (err) {
        clearFeedback = err.message || '清理失败';
      } finally {
        clearing = false;
        renderCacheBar(lastSys?.cache);
        setTimeout(() => {
          clearFeedback = '';
          if (dock.classList.contains('open') && lastSys) renderCacheBar(lastSys.cache);
        }, 3200);
      }
    }
  });

  let lastX = 0;
  let lastDx = 0;

  document.addEventListener('pointermove', (e) => {
    if (!isEnabled()) return;
    const gate = document.getElementById('gate');
    if (gate && !gate.hidden) return;
    lastDx = e.clientX - lastX;
    lastX = e.clientX;
    const docW = document.documentElement.clientWidth || window.innerWidth;
    if (e.clientX >= docW - 24) {
      openDock();
    } else if (dock.classList.contains('open') && !dockHover && e.clientX < docW - 340) {
      scheduleClose(320);
    }
  }, { passive: true });

  // 快速往右甩进原生滚动条或甩出右边缘时（含高速向右冲出预测）同样直接呼出
  document.documentElement.addEventListener('mouseleave', (e) => {
    if (!isEnabled()) return;
    const gate = document.getElementById('gate');
    if (gate && !gate.hidden) return;
    const docW = document.documentElement.clientWidth || window.innerWidth;
    const nearRight = e.clientX >= docW - 60;
    const fastRight = lastDx > 8 && lastX >= docW - 140 && e.clientX >= docW - 140;
    if ((nearRight || fastRight) && e.clientY >= 0 && e.clientY <= window.innerHeight) {
      openDock();
    }
  });

  document.addEventListener('pointerdown', (e) => {
    if (!isEnabled() || !dock.classList.contains('open') || clearing || confirmClear) return;
    if (dock.contains(e.target) || edge.contains(e.target)) return;
    closeDock();
  }, { passive: true });

  edge.addEventListener('pointerenter', () => openDock());
  edge.addEventListener('mouseenter', () => openDock());

  dock.addEventListener('mouseenter', () => {
    dockHover = true;
    clearTimeout(closeTimer); closeTimer = 0;
    bumpIdle();
  });
  dock.addEventListener('mousemove', () => bumpIdle(), { passive: true });
  dock.addEventListener('mouseleave', (e) => {
    dockHover = false;
    if (e.relatedTarget && edge.contains(e.relatedTarget)) return;
    scheduleClose(340);
  });

  const targetView = () => ((activeTab === 'procs' && store.can('system:config')) ? 'models' : 'generate');

  if (tabsEl) {
    tabsEl.addEventListener('click', (e) => {
      const tabBtn = e.target.closest('[data-rd-tab]');
      if (!tabBtn) return;
      const next = tabBtn.dataset.rdTab;
      if (next !== 'jobs' && next !== 'procs') return;
      if (next === activeTab) return;
      activeTab = next;
      try { localStorage.setItem(TAB_KEY, activeTab); } catch { /* ignore */ }
      bumpIdle();
      syncTabs();
      listEl.innerHTML = `<div class="rd-empty">${activeTab === 'procs' ? '正在读取进程状态…' : '正在读取最新队列…'}</div>`;
      if (activeTab === 'procs') refreshProcesses();
      else refreshJobs();
    });
  }

  if (goBtn) {
    goBtn.addEventListener('click', () => {
      const dest = targetView();
      closeDock();
      onNavigate(dest);
    });
  }

  listEl.addEventListener('click', (e) => {
    const item = e.target.closest('.rd-item');
    if (item) {
      const dest = item.dataset.procId && store.can('system:config') ? 'models' : 'generate';
      closeDock();
      onNavigate(dest);
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && dock.classList.contains('open')) {
      closeDock();
    }
  });

  window.addEventListener('resize', () => {
    if (window.innerWidth <= 860) closeDock();
  });
}
