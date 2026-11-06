/**
 * Host half of `dsh-session-delete`.
 *
 * The deployment ships no Session delete RPC at all (workspace archive only
 * hides a row), so permanent deletion means removing the persisted Session
 * directory from disk and letting the Host's own listings drop it:
 *
 * - `sessionQuery.listSessions` re-reads `sessionPersistence.list()` on every
 *   `session.list`, so a removed directory disappears from the sidebar after a
 *   Client-side `sessions.refresh()`.
 * - `workspace.json` keeps the stale id; the sidebar skips workspace session
 *   ids that have no `session.list` summary, so no registry surgery is needed
 *   (and none is attempted: another writer may own that file).
 * - The SQLite search index reconciles against persistence on its next query,
 *   deleting the vanished Session's rows by itself.
 *
 * A Session still mounted in the Host (a live agent or an attached store
 * entry) cannot be deleted: the Host would flush it back to disk. Those
 * requests are rejected instead of half-applied.
 */
import { readdir, rm, stat } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

/** Route the Client half POSTs to. Relative on the Client side, rootless here. */
const ROUTE = '/dsh-session-delete/delete'

/** A Session id is a path segment: no separators, no traversal, bounded length. */
const SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/

/** Delete requests carry one id; anything bigger is a malformed caller. */
const MAX_BODY_BYTES = 8 * 1024

export const name = 'dsh-session-delete'

export const inject = ['webServer', 'connection', 'sessions']

/**
 * Mount the fenced delete route.
 * @param ctx - Host plugin context (webServer, connection, sessions).
 */
export function apply(ctx) {
  ctx.effect(() => ctx.webServer.register({
    kind: 'exact',
    path: ROUTE,
    handler: async (req, res) => {
      // Connection owns deployment trust and browser authentication: never
      // infer either from the request itself.
      if (ctx.connection.requestRejection(req) !== undefined) {
        writeJson(res, 403, failure('forbidden', 'forbidden'))
        return
      }
      if (req.method !== 'POST') {
        writeJson(res, 405, failure('method-error', 'method not allowed'))
        return
      }
      try {
        const payload = await readJsonBody(req)
        const sessionId = payload?.sessionId
        if (typeof sessionId !== 'string' || !SESSION_ID.test(sessionId)) {
          writeJson(res, 400, failure('bad-request', '缺少合法的会话 ID'))
          return
        }
        const liveReason = liveSessionReason(ctx, sessionId)
        if (liveReason !== undefined) {
          writeJson(res, 409, failure('session-live', liveReason))
          return
        }
        const removed = await deletePersistedSession(ctx, sessionId)
        if (removed.length === 0) {
          writeJson(res, 404, failure('session-missing', '没有找到该会话的存储文件，它可能已经被删除'))
          return
        }
        ctx.logger?.info?.(`dsh-session-delete: removed ${sessionId}: ${removed.join(', ')}`)
        writeJson(res, 200, { ok: true, value: { sessionId, removed } })
      } catch (error) {
        writeJson(res, error?.status ?? 500, failure(error?.code ?? 'internal', error?.message ?? String(error)))
      }
    },
  }), 'dsh-session-delete.route')
}

/**
 * Why this Session must not be deleted right now, if it must not.
 * @param ctx - Host plugin context.
 * @param sessionId - Candidate Session.
 * @returns A user-facing reason, or undefined when the Session is cold.
 */
function liveSessionReason(ctx, sessionId) {
  const agent = ctx.get('agents')?.get?.(sessionId)
  if (agent?.status === 'running') {
    return '该会话正在运行：请先停止任务，再完全退出并重新打开 DSH 后删除'
  }
  if (agent !== undefined) {
    return '该会话仍在本进程内存中（DSH 没有关闭会话的功能）：请完全退出并重新打开 DSH 后删除，或改用“归档”'
  }
  if (ctx.sessions?.get?.(sessionId) !== undefined) {
    return '该会话仍在本进程内存中（DSH 没有关闭会话的功能）：请完全退出并重新打开 DSH 后删除，或改用“归档”'
  }
  return undefined
}

/**
 * Remove every persisted artifact of one cold Session.
 * @param ctx - Host plugin context.
 * @param sessionId - Validated Session id.
 * @returns Absolute paths actually removed.
 */
async function deletePersistedSession(ctx, sessionId) {
  const removed = []
  for (const root of sessionRoots(ctx)) {
    let projects
    try {
      projects = await readdir(root, { withFileTypes: true })
    } catch {
      continue
    }
    for (const project of projects) {
      // Sessions live one level down, grouped by their project directory.
      if (!project.isDirectory()) continue
      const dir = join(root, project.name, sessionId)
      try {
        if (!(await stat(dir)).isDirectory()) continue
      } catch {
        continue
      }
      await rm(dir, { recursive: true, force: true })
      removed.push(dir)
    }
  }
  for (const cacheRoot of projectionCacheRoots(ctx)) {
    for (const suffix of ['.json', '.json.tmp']) {
      const file = join(cacheRoot, `${sessionId}${suffix}`)
      try {
        if (!(await stat(file)).isFile()) continue
      } catch {
        // No cached projection for this Session under this home.
        continue
      }
      try {
        await rm(file, { force: true })
        removed.push(file)
      } catch {
        // An unwritable cache entry never blocks the delete itself.
      }
    }
  }
  return removed
}

/**
 * Candidate Session storage roots, most authoritative first.
 * @param ctx - Host plugin context.
 * @returns Absolute directories, de-duplicated.
 */
function sessionRoots(ctx) {
  const roots = []
  const declared = ctx.get('sessionPersistence')?.root
  if (typeof declared === 'string' && declared !== '') roots.push(declared)
  for (const home of dshHomes(ctx)) roots.push(join(home, 'sessions'))
  return [...new Set(roots)]
}

/**
 * Candidate projection-cache directories for the same homes.
 * @param ctx - Host plugin context.
 * @returns Absolute directories, de-duplicated.
 */
function projectionCacheRoots(ctx) {
  const roots = []
  const declared = ctx.get('sessionPersistence')?.root
  if (typeof declared === 'string' && declared !== '') roots.push(join(dirname(declared), 'storages', 'session_projcache', 'sessions'))
  for (const home of dshHomes(ctx)) roots.push(join(home, 'storages', 'session_projcache', 'sessions'))
  return [...new Set(roots)]
}

/**
 * Candidate DSH homes: the deployment's own first, then the documented default.
 * @param ctx - Host plugin context.
 * @returns Absolute directories.
 */
function dshHomes(ctx) {
  const homes = []
  const env = process.env.DSH_HOME
  if (typeof env === 'string' && env !== '') homes.push(env)
  const profileHome = ctx.profileContext?.home ?? ctx.get('profileContext')?.home
  if (typeof profileHome === 'string' && profileHome !== '') {
    // `<home>/profiles/<name>` -> `<home>`; a home without that shape is used as-is.
    const parent = dirname(profileHome)
    homes.push(dirname(parent) === parent ? profileHome : dirname(parent))
  }
  homes.push(join(homedir(), '.dsh'))
  return [...new Set(homes)]
}

/**
 * Read one bounded JSON request body.
 * @param req - Incoming request stream.
 * @returns The parsed object (undefined when the body is blank).
 */
async function readJsonBody(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > MAX_BODY_BYTES) {
      const error = new Error('请求体过大')
      error.status = 413
      error.code = 'payload-too-large'
      throw error
    }
    chunks.push(chunk)
  }
  if (size === 0) return undefined
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'))
  } catch {
    const error = new Error('请求体不是合法 JSON')
    error.status = 400
    error.code = 'bad-request'
    throw error
  }
}

/**
 * Build the shared failure envelope.
 * @param code - Machine-readable error code.
 * @param message - User-facing message.
 * @returns `{ ok: false, error }`.
 */
function failure(code, message) {
  return { ok: false, error: { code, message } }
}

/**
 * Write one JSON response and end it.
 * @param res - Server response.
 * @param status - HTTP status.
 * @param payload - JSON-serializable body.
 */
function writeJson(res, status, payload) {
  const body = JSON.stringify(payload)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(body),
    'cache-control': 'no-store',
  })
  res.end(body)
}
