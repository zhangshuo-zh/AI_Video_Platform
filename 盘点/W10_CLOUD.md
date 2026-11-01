# W10 云端 adapter：取数留档与真/模拟边界（2026-11-01）

这一包接的是**国内官方 API**：文本 DeepSeek、视频阿里云百炼 Wan 2.7。
所有模型名、参数、价格都按下面这些官方页面在 2026-11-01 核过才进清单；
没有核到同口径的（DeepSeek v4-pro 价格、Wan 的 I2V/首尾帧/音频）不登记。

## 1. 官方来源

| 用途 | 官方页面 |
|---|---|
| DeepSeek 价格 | <https://api-docs.deepseek.com/quick_start/pricing> |
| DeepSeek Chat Completions | <https://api-docs.deepseek.com/api/create-chat-completion> |
| Wan 2.7 文生视频 API | <https://help.aliyun.com/zh/model-studio/text-to-video-api-reference> |
| Wan 2.7 模型页（价格/限流/快照） | <https://help.aliyun.com/zh/model-studio/wan2-7-t2v> |
| 百炼模型调用价格总表 | <https://help.aliyun.com/zh/model-studio/model-pricing> |

DeepSeek 的价格页有两种币种展示；平台统一按**人民币/百万 token**记账：

| 模型 | 输入（缓存命中） | 输入（缓存未命中） | 输出 |
|---|---|---|---|
| `deepseek-flash` 闲时 | 0.02 元/百万 token | 1 元/百万 token | 4 元/百万 token |
| `deepseek-flash` 忙时 | 0.04 元/百万 token | 2 元/百万 token | 8 元/百万 token |

忙时＝北京时间周一至周五 9:00—12:00、14:00—18:00（官方 UTC 口径 01:00—04:00、06:00—10:00），法定节假日除外。界面预填的是**输出忙时 8 元/百万 token**，数量默认 0.002（2,000 token），所以显示约 ¥0.02；这是上限估算，不是结算价。

Wan 2.7 文生视频（华北 2 北京）：

| 分辨率 | 价格 |
|---|---|
| 720P | 0.6 元/秒 |
| 1080P | 1 元/秒 |

官方模型页同时列出快照 `wan2.7-t2v-2026-04-25` 与 `wan2.7-t2v-2026-06-12`；平台用别名 `wan2.7-t2v`。**付费前如果要锁死版本，把 model 换成带日期的快照 ID 即可。**

## 2. 许可与国内可用性

两家都是**国内商业 API**，不下载权重、不在本地跑模型，按官方服务条款付费调用：

- DeepSeek：官方 API，国内直连 `api.deepseek.com`；不需要额外代理或境外聚合。
- 阿里云百炼：官方 API，华北 2（北京）提供服务；用业务空间专属域名，不需要境外通道。

W10 只新增这两家；火山方舟／MiniMax／可灵／Seedance 仍是“只登记名字、未核验”，没有执行接线。境外聚合（OpenRouter 等）没有引入。

## 3. API 契约

### DeepSeek

- `POST https://api.deepseek.com/chat/completions`
- `Authorization: Bearer $DEEPSEEK_API_KEY`
- 请求体：`model`、`messages`、`stream: false`、可选 `max_tokens`（1—393216）、`temperature`（0—2）、`thinking: {"type": "enabled"|"disabled"}`
- 回执：`id`（用作 `provider_request_id`）、`choices[0].message.content`、`usage`（prompt/completion/total tokens 与缓存字段）
- 平台只把正文写进 `.md` 产物；`reasoning_content` 只记字符数，不落正文。

### Wan 2.7

- 创建：`POST {ALIYUN_WAN_BASE_URL}/api/v1/services/aigc/video-generation/video-synthesis`
  - Header：`Authorization: Bearer $DASHSCOPE_API_KEY`、`X-DashScope-Async: enable`
  - Body：`model`、`input.prompt`、`parameters.resolution|ratio|duration|seed`
- 轮询：`GET {ALIYUN_WAN_BASE_URL}/api/v1/tasks/{task_id}`
- 成功回执：`output.video_url`；用量在**响应顶层 `usage`**（`duration`、`ratio`、`SR` 等），不在 `output` 里。
- `task_id` 与 `video_url` 有效 24 小时；平台创建成功后立刻先落一份含 `task_id` 的部分回执，取消/崩溃时还能追。
- 官方 base URL 是业务空间专属域名：`https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com`（新加坡为 `ap-southeast-1`）。平台不猜 Workspace，只认环境变量 `ALIYUN_WAN_BASE_URL`。

## 4. 密钥与脱敏

- 平台只存**变量名**：DeepSeek 用 `DEEPSEEK_API_KEY`，Wan 用 `DASHSCOPE_API_KEY`；请求 JSON 里只有 `key_env`，没有值。
- 本机 `DEEPSEEK_API_KEY` 只在交互式 zsh（`~/.zshrc`）里有值。这次 API 与 worker 用 `zsh -lic` 启动，值只存在进程环境；**没有打印、没有写库、没有进日志或响应**。
- 回执 JSON、`run.log`、错误消息都检查过不含密钥串；401 测试故意让 mock 把密钥写进错误消息，`cloud_run._redact()` 会替换成 `***`。
- 本机**没有** `DASHSCOPE_API_KEY`，也没有 Workspace 专属域名，所以 Wan 没有真实付费调用。

## 5. 真调用与模拟的边界

### 真的

- DeepSeek 文本调用，两次（作业 #36、#37）：

| Job | 请求 ID | HTTP | 用量 | 供应商延迟 | 产物 |
|---|---|---|---|---|---|
| #36 | `a38c360a-034f-4939-96cd-2406c84ad153` | 200 | 21 输入 / 21 输出 / 42 total | 1051 ms | 99 字节中文正文 |
| #37 | `0c578bbc-b7d0-40b3-9972-d992ceac7963` | 200 | 21 输入 / 24 输出 / 45 total | 860 ms | 104 字节中文正文 |

两条都是 `pending_reconciliation`，预计 16,000 micros（¥0.016）；实际 token 用量已写进 `resources_json.provider.usage`，但**没有自动改金额**——结算要人工核对，这不在 W10。

- 浏览器：无头 Chrome 真鼠标走完「选中云端 → 预填百万 Token/8 元/¥0.02 → 勾确认 → 提交 → 抽屉显示请求 ID/用量/产物」15 项全过；默认视频轨仍是本地 fastmetal 且不显示计价。补证 1 项：Wan 表单的 `ratio` 渲染成五档下拉（16:9/9:16/1:1/4:3/3:4），720P 预填 0.6 元/秒、5 秒、¥3.00（未提交，不花钱）。
- 测试：`pytest tests -q` → **296 passed**；Wan 的创建→轮询→下载、401/429/缺密钥/缺 base URL 都是本地 `ThreadingHTTPServer` 模拟；另有一条“本地假适配器失败→云端 mock 零请求”的断言。

### 模拟的

- **Wan 没有真实网关调用**：没有 key/Workspace，只验证了请求形状、异步轮询、下载与回执解析。真实网关的字段差异（尤其顶层 `usage` 与 `video_url` 的返回位置）要等有 key 时再跑一次。
- Wan 的「失败任务」「任务超时」「下载中断」只在 mock 覆盖；真实 OSS 签名 URL 的过期与重定向没有实测。
- DeepSeek 的流式、工具调用、JSON mode、v4-pro 都没接。

## 6. 复现方式（不含密钥值）

```bash
# 1) 配置文本云端槽位（管理员）
#    provider=deepseek model=deepseek-flash enabled=1 is_default=0
#    key_env=DEEPSEEK_API_KEY

# 2) 从带 DEEPSEEK_API_KEY 的交互式 shell 启动 API 与 worker
zsh -lic 'cd "/Users/zhangshuo/AI动漫工作流设计文档/平台设计/app" && ./run.sh'
zsh -lic 'cd "/Users/zhangshuo/AI动漫工作流设计文档/平台设计/app" && ./run.sh worker'

# 3) 生成中心提交一条文本作业，选云端、勾确认
#    服务端会起：.venv/bin/python backend/cloud_run.py <job>/outputs/<id>-cloud-request.json
```

Wan 真实调用前还需要：

```bash
export DASHSCOPE_API_KEY=YOUR_API_KEY
export ALIYUN_WAN_BASE_URL=https://YOUR_WORKSPACE_ID.cn-beijing.maas.aliyuncs.com
```

## 7. 仍然开着的口

- DeepSeek v4-pro 价格没有核到同一口径，不登记；要用先补价格页与参数。
- 实际用量不自动结算：`cost_micros` 仍是提交时确认的预计值，`billable_status` 仍待核对。
- Wan 只接 T2V；I2V、首尾帧、音频、`negative_prompt`、`audio_url` 未进清单。
- 取消云端作业只杀本地子进程；供应商侧任务可能继续跑并计费，Wan 的部分回执留了 `task_id` 供人工追。
- 火山方舟／MiniMax／可灵／Seedance／图像与配音云端档仍然只有名字，没有价格与执行接线。
