import { api } from './api.js';
import { gotoEntry } from './goto.js';
import { store } from './store.js';

export const SHOT_TASK_GROUPS = {
  assets: '缺素材', review: '待审核', rework: '返工', failed: '失败与待核对',
};

// 本机浏览器内按登录账号、项目与集分隔。存储被禁用时仍可在当前页面往下处理。
const memory = new Map();
function prefix(projectId, episodeId) {
  return `fv-episode-work:v1:${store.user?.id}:${projectId}:${episodeId}:`;
}

function read(key) {
  if (memory.has(key)) return memory.get(key);
  try {
    const value = localStorage.getItem(key);
    return value ? JSON.parse(value) : null;
  } catch {
    return memory.get(key) ?? null;
  }
}

function write(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
    memory.delete(key);
    return true;
  } catch {
    memory.set(key, value);
    return false;
  }
}

export function taskCursor(projectId, episodeId) {
  return read(prefix(projectId, episodeId) + 'cursor');
}

export function taskAtCursor(tasks, cursor, { next = false } = {}) {
  if (!cursor) return null;
  const group = tasks.filter((item) => item.group === cursor.group);
  if (next) return group.find((item) => item.seq > cursor.seq) ?? null;
  return group.find((item) => item.shot_id === cursor.shot_id)
    ?? group.find((item) => item.seq >= cursor.seq)
    ?? group[0] ?? null;
}

export function shotTaskFocus(task) {
  if (task.group === 'assets') return 'assets';
  if (task.replacement_ready) return 'compare';
  return task.can_review ? 'review' : 'job';
}

export function openShotTask(task, projectId, episodeId) {
  if (store.currentId !== projectId) return;
  const work = { projectId, episodeId, group: task.group, seq: task.seq, shot_id: task.shot_id };
  write(prefix(projectId, episodeId) + 'cursor', work);
  gotoEntry({
    module: 'shot', shot_id: task.shot_id, episode_id: episodeId, job_id: task.job_id,
    focus: shotTaskFocus(task),
    work,
  });
}

export function reworkDraftKey(projectId, episodeId, shotId, sourceJobId) {
  return prefix(projectId, episodeId) + `draft:${shotId}:${sourceJobId}`;
}

export function readReworkDraft(projectId, episodeId, shotId, sourceJobId) {
  return read(reworkDraftKey(projectId, episodeId, shotId, sourceJobId));
}

export function writeReworkDraft(key, draft) {
  return write(key, draft);
}

export function episodeDrafts(projectId, episodeId) {
  if (!store.user || !projectId || !episodeId) return [];
  const start = prefix(projectId, episodeId) + 'draft:';
  const keys = new Set([...memory.keys()].filter((key) => key.startsWith(start)));
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key?.startsWith(start)) keys.add(key);
    }
  } catch { /* 浏览器关闭存储时，使用当前页草稿 */ }
  return [...keys].map((key) => ({ key, draft: read(key) }))
    .filter(({ draft }) => draft)
    .sort((a, b) => b.draft.updatedAt - a.draft.updatedAt);
}

export async function reworkPrefill(projectId, episodeId, shotId, jobId) {
  const [source, review] = await Promise.all([api.job(jobId), api.jobReview(jobId)]);
  if (source.project_id !== projectId || source.shot_id !== shotId
      || (source.episode_id != null && source.episode_id !== episodeId)) {
    throw new Error('这条来源作业不属于当前镜头。');
  }
  const sign = review.signatures.find((item) => ['returned', 'rejected'].includes(item.decision));
  if (!sign || review.signatures.some((item) => item.decision === 'approved')) {
    throw new Error('该版本当前没有有效的退回意见。');
  }
  return {
    projectId, episodeId, shotId, capability: source.capability, prompt: source.prompt_snapshot,
    params: source.params, template: source,
    rework: { id: jobId, note: sign.note, inputRefs: source.input_refs },
  };
}
