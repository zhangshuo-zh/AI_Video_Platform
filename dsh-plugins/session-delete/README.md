# dsh-session-delete

Adds a **删除会话** row to the "…" menu of every sidebar Session, deleting that
Session permanently (not the `archive` behaviour, which only hides the row).

## Usage

1. Hover a Session in the left sidebar and click its "…" button.
2. Choose **删除会话**. The row turns red and asks **再次点击确认删除**.
3. Click again. The Session's stored history is removed and the row disappears.

Moving the pointer away (or closing the menu) disarms the confirmation, so a
stray second click cannot delete anything.

## 如何“关闭”会话（为什么有时删不掉）

DSH 目前**没有“关闭会话”这个功能**：在侧边栏切到别的会话、或切到别的页面，只是切换视图，
宿主进程仍然把这个会话的运行时（agent）留在内存里，直到 DSH 进程退出。所以：

- 已在本进程里发过消息的会话 → 宿主仍持有它，禁止删除（否则退出时它会把日志重新写回磁盘）。
- 只是想让它从内存里释放 → 没有对应操作；只能**完全退出 DSH（Cmd+Q）再打开**，
  之后该会话变成“冷”会话（只有磁盘记录、没有运行时），此时“…”→ 删除会话 可以直接删。
- 会话正在跑任务时，先停止任务；停止后仍提示“需重启 DSH 后删除”，就按上一条重启。

行内提示“需重启 DSH 后删除”时，把鼠标停在那一行可以看到完整原因。

## Behaviour and limits

- Deleting is permanent: the Session's log directory and its cached projection
  are removed from disk. The search index drops the Session on its next query.
- A Session that the Host still holds in memory (its agent was used in this
  process) is refused with a message telling you to restart DSH first — otherwise
  the Host would flush it back to disk. Sessions that were only *viewed* (never
  prompted) are cold and delete immediately.
- The sidebar's own "归档" action is untouched; use it when the Session should
  stay on disk but leave the list.

## Layout

| File | Role |
| --- | --- |
| `index.js` | Host half: mounts the fenced `POST /dsh-session-delete/delete` route. |
| `client.js` | Client half: the menu row, two-step confirmation, catalog refresh. |
| `cordis.patch.yml` | Inserts the plugin entry into the profile bundle. |
| `locale/*.json` | Plugin Manager title/description. |
| `icon.svg` | Plugin icon. |

## Notes

The deployment ships no Session-delete RPC (only workspace archive), so the
host half removes the persisted artifacts directly:

```
<dsh-home>/sessions/<project-slug>/<sessionId>/          (recursive)
<dsh-home>/storages/session_projcache/sessions/<id>.json
```

`workspace.json` is left alone: the sidebar skips workspace Session ids that no
longer have a `session.list` summary. Root discovery tries
`sessionPersistence.root`, `$DSH_HOME`, the profile home's parent, and `~/.dsh`.
