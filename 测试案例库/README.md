# AI 视频生产平台 · 测试案例库

- 版本：v1.1（2026-11-05），对应代码 `e4ba3c6`（接口 158 条、权限点 18 个、迁移 0001—0036、全量单测 781 passed）
- 这一套是给**后续 AI 自动测试**用的：清单说清"测什么、预期是什么"，执行器把清单变成能跑的东西，结果单独落一份文件。

## 文件

| 文件 | 是什么 |
|---|---|
| [清单.md](清单.md) | 107 条案例：ID／案例／关键步骤／预期／层／优先级，另列 22 条本轮不自动跑的 |
| [tools/run_checklist.py](tools/run_checklist.py) | 执行器。按清单逐条发请求、查库、跑 mock 作业，输出 PASS／FAIL／BLOCKED |
| [执行结果.md](执行结果.md) | 2026-11-05 那轮的结果与两条真缺陷的取证 |
| README.md | 本文件：怎么跑、怎么判、怎么扩 |

## 怎么跑

```bash
cd 平台设计
app/backend/.venv/bin/python 测试案例库/tools/run_checklist.py                 # 逐行打印
app/backend/.venv/bin/python 测试案例库/tools/run_checklist.py --json          # 给机器读
app/backend/.venv/bin/python 测试案例库/tools/run_checklist.py --only JOB,REV  # 只跑某域
```

执行器做四件事保证结果可信：

1. **任何项目 import 之前**先把 `FASTVIDEO_DB／FILES／WORKSPACE／JOBS` 指到新建的临时目录，并设 `FASTVIDEO_ALLOW_MOCK=1`。跑完临时目录留在 `/tmp/fvqa-*`，开发库一个字节都不动。
2. 不起端口、不真登录（只有改口令那几条真登录）。会话用 `security.new_session_token()` 现铸，快且不把口令传来传去。
3. 需要跑完作业的案例现场调 `worker.run_once()`，把队列排空后再判状态；媒体产物由 mock 适配器借本机 FFmpeg 生成真 mp4／png，所以成片与产物审核也能真跑。
4. 断言失败就是 FAIL，环境不满足就是 BLOCKED。**没有 `or True`、没有"读不到就当过"**。

## 判定

| 结果 | 含义 |
|---|---|
| PASS | 步骤与断言全过 |
| FAIL | 与代码承诺不符，必须附期望／实际与取证位置 |
| BLOCKED | 前置环境不满足（没有可用默认轨、没有 worker、没有云端档位），不算缺陷也不算通过 |
| ERROR | 执行器自己出错（列名、字段名写错等），修案例不算修产品 |

优先级：P0＝红线（权限绕过、数据丢失、钱算错、签字失效）；P1＝主路径；P2＝边界与文案。

## 案例写法约定

- ID＝`域-两位序号`，域前缀：AUTH／RBAC／PROJ／WRT／AST／JOB／REV／AUD／PIP／CMP／MOD／DSH／SEC，UI 类另列。旧 ID 不复用、不重排。
- 一条案例只验一个承诺。要验"删项目同时清子表但留作业"，就单开一条，别和"删除要确认名"混在一起。
- 拒绝类断言必须同时看**状态码和原话**：同一个 422 可能来自完全不同的规则，只断状态码等于没断。
- 快照类断言（提示词、参数、源版本、计价）一律"改上游再读回来"，不读配置表推断。
- 环境相关的（worker 在线、FFmpeg 有无 libass、云端档位是否配好）走 BLOCKED，不写死期望。
- 清理走 `drop_project()`（删除要 `project:delete`，只有管理员有）；作业与产物按平台设计留在临时库里，整库丢弃即可。

## 已知没覆盖的

清单 §15 那 22 条：真浏览器 12 条（灰态按钮与提示文案、窄屏 chip、冲突并排、告警横条、主题记忆）、云端账单 3 条、真实模型与画质 4 条、时钟型告警 3 条、worker 被 kill 1 条。它们需要环境或人工，不在 PASS 里充数。
