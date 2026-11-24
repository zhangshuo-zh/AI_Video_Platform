"""适配器规格。修改规格时在对应能力模块维护。"""
from __future__ import annotations
from .types import Adapter, ModelSpec, Param, Price


# ---------- 文本：本地（2026-11-06 本机实测）----------
# 这是平台**第一条本地文本腿**。在它之前 text 族三条全是 backend="cloud"，
# 也就是说"本地优先"这句话在文本这一类根本没有可走的路——
# 《智能体管理中心》说明书 §3.1 要的正是这一档，取证见
# 《文档/开发/智能体管理/00-接入前现状审计.md》§4。
#
# 为什么它是"服务型"而不是子进程型：Ollama 自己是一个常驻 HTTP 服务，
# 端点住在 `engine_instance`（0040），与 draw_things 同一族。
# 为什么它不进 `_service_engine_gate` 那一族：Draw Things 同一时刻只有
# 一个"当前模型"，而 Ollama 的模型名是**每次请求里带的**，
# 不存在"引擎自己选着另一档"这件事——核对一个不存在的概念只会得到假话。
# 详见《文档/开发/引擎接入/00-接入前现状审计.md》§5b 同一条分族纪律。
_OLLAMA = Adapter(
    code="ollama",
    label="Ollama（本机模型服务）",
    capability="text",
    backend="local",
    source="本机 Ollama 0.35.1 实测（2026-11-06）：GET /api/version 回版本号，"
           "GET /api/tags 回 models[] 且带 capabilities 数组；"
           "POST /api/chat 的 tools 与 format 两条都跑通过真调用（见 note）",
    aliases=("ollama-api",),
    params_source="目标机实测的 /api/chat 请求字段（num_predict／temperature／think）",
    home_env="",            # 端点在 engine_instance 表里，不写死 127.0.0.1:11434
    note="密钥不适用：本机服务，平台不发凭据。"
         "实测两条硬结论——① 带 tools 的调用能真的返回 tool_calls（qwen3:8b，16.3 秒）；"
         "② 结构化输出必须把思考关掉：think 缺省时 300 个 token 全花在推理上、"
         "content 是空的，加 think=false 后同样内容 1.9 秒返回合法 JSON。"
         "所以本档默认 think=false，要思考链是显式改参数，不是平台替他选。",
    models=(
        ModelSpec(
            code="qwen3:8b",
            label="Qwen3 8B（本机 Q4_K_M）",
            note="本机 /api/tags 实测在册的唯一一档：5,225,388,164 字节、"
                 "8.2B、Q4_K_M、上下文 40,960，capabilities=[completion, tools, thinking]。"
                 "换一档模型要在清单里另登记一行并重新实测，"
                 "不能拿这一行的参数表去跑没测过的档。",
            # 结构化输出：`runners._ollama()` 真的会往请求里放 `format`（JSON schema），
            # `llm_http_run.py` 再把 `format` 与强制的 `think=false` 一起发下去，
            # 回执里记 `has_format`。2026-11-06 目标机实测：同样内容 think=false
            # 时 1.9 秒回合法 JSON。平台里唯一有实证的"结构化输出"就是这一档。
            structured_output=True,
            params=(
                Param("max_tokens", "int", 2048, 1, 32768,
                      note="下发成 options.num_predict。上限按上下文 40,960 的"
                           "保守值登记，不是官方规格"),
                Param("temperature", "float", None, 0, 2,
                      note="不填＝服务端默认。实测 0 时结构化输出稳定"),
                Param("think", "bool", False,
                      note="关掉思考。这条默认值是量出来的：开着思考时"
                           "format(JSON schema) 会返回空 content"),
            ),
        ),
    ),
)


# ---------- 文本 / 云端（W10 核验官方 schema 后登记） ----------
# 文本是本机唯一没有可用路线的能力（规格 02 §8.2 原文："没有合格的本地文本路线"）。
# DeepSeek 是国内官方 API、OpenAI 兼容、密钥名现场已存在，因此只登记这一家；
# 模型名与价格按 2026-11-01 官方文档核过，旧名 deepseek-chat/reasoner 已下架，
# 不再出现在清单里。v4-pro 的价格这次没有核到同一口径，宁可不登记。
_DEEPSEEK = Adapter(
    code="deepseek",
    label="DeepSeek（官方 API）",
    capability="text",
    backend="cloud",
    aliases=("deepseek-api",),
    key_env="DEEPSEEK_API_KEY",
    source="DeepSeek 官方文档（2026-11-01 核验）：https://api-docs.deepseek.com/",
    params_source="Chat Completions 官方参数表（max_tokens / temperature / thinking）",
    note="OpenAI 兼容端点；密钥只从环境变量名读取，平台不存值。",
    models=(
        ModelSpec(
            code="deepseek-flash",
            label="DeepSeek Flash",
            note="官方当前模型名。价格分忙闲时与输入/输出两段，界面只预填"
                 "输出忙时单价作为上限；实际用量由回执写进 resources。",
            params=(
                Param("max_tokens", "int", 2048, 1, 393216,
                      note="输出上限；文本作业按需调大"),
                Param("temperature", "float", None, 0, 2,
                      note="不填＝供应商默认"),
                Param("thinking", "bool", False,
                      note="开＝deepseek 的 thinking.type=enabled"),
            ),
            prices=(
                Price(
                    label="输出 token（忙时上限）",
                    unit="million_token",
                    currency="CNY",
                    amount_micros=8_000_000,
                    default_quantity=0.002,
                    source="DeepSeek 官方定价（2026-11-01）：输出 4 元/百万 token（闲时）、"
                           "8 元（忙时）；输入缓存命中 0.02/0.04、未命中 1/2",
                    note="按 2,000 token 预填；这是上限估算，不是最终结算价。",
                ),
            ),
        ),
    ),
)


# ---------- 文本 / 云端 · Google Gemini（2026-11-24 核验并实测） ----------
_GEMINI_PARAMS = (
    Param("max_tokens", "int", 2048, 1, 65536,
          note="映射为 generationConfig.maxOutputTokens"),
    Param("temperature", "float", None, 0, 2,
          note="不填＝供应商默认"),
    Param("thinking", "bool", False,
          note="对 Gemini 4：关＝low、开＝high；对 Gemini 3：关＝minimal、开＝high"),
)

_GEMINI_PRICES = (
    Price(
        label="AI Studio 免费层参考（百万 token）",
        unit="million_token",
        currency="USD",
        amount_micros=0,
        default_quantity=0.002,
        source="Google AI Studio Free Tier：实际计费以供应商控制台账单为准",
        note="免费层默认 0 费率；若绑定付费结算账号，请在控制台核对实际消耗。",
    ),
)

_GEMINI = Adapter(
    code="gemini",
    label="Google Gemini（官方 API）",
    capability="text",
    backend="cloud",
    aliases=("google-gemini", "gemini-api"),
    key_env="GEMINI_API_KEY",
    source="Google Gemini API 文档（2026-11-24 本机实测 /v1beta/models）：https://ai.google.dev/gemini-api/docs",
    params_source="generateContent 官方请求参数表（maxOutputTokens / temperature / thinkingConfig）",
    note="支持文本生成、结构化输出与多模态图像理解；密钥可通过「参数管理 → 供应商密钥」密文配置或环境变量注入。",
    models=(
        ModelSpec(
            code="gemini-4-hydrogen",
            label="Gemini 4 Hydrogen（新一代多模态）",
            structured_output=True,
            vision_inputs=True,
            params=_GEMINI_PARAMS,
            prices=_GEMINI_PRICES,
            note="实测支持免费层 Key 与多模态看图；支持 thinkingLevel=low/medium/high。",
        ),
        ModelSpec(
            code="gemini-4-neon",
            label="Gemini 4 Neon（均衡旗舰）",
            structured_output=True,
            vision_inputs=True,
            params=_GEMINI_PARAMS,
            prices=_GEMINI_PRICES,
            note="Gemini 4 均衡旗舰档；免费层可能报 429 额度受限，需高配额或付费计划。",
        ),
        ModelSpec(
            code="gemini-4-argon",
            label="Gemini 4 Argon（高推理档）",
            structured_output=True,
            vision_inputs=True,
            params=_GEMINI_PARAMS,
            prices=_GEMINI_PRICES,
            note="Gemini 4 强推理档；免费层可能报 429 额度受限，需高配额或付费计划。",
        ),
        ModelSpec(
            code="gemini-4-carbon",
            label="Gemini 4 Carbon（轻量高并发）",
            structured_output=True,
            vision_inputs=True,
            params=_GEMINI_PARAMS,
            prices=_GEMINI_PRICES,
            note="Gemini 4 轻量高并发档；免费层可能报 429 额度受限，需高配额或付费计划。",
        ),
        ModelSpec(
            code="gemini-3-flash-preview",
            label="Gemini 3 Flash Preview（快响稳定兜底）",
            structured_output=True,
            vision_inputs=True,
            params=_GEMINI_PARAMS,
            prices=_GEMINI_PRICES,
            note="实测免费层高可用、低延迟，作为 4 系列高负载或额度受限时的默认稳定兜底。",
        ),
    ),
)


# 火山方舟与 MiniMax 仍只登记名字：没有拿到同一口径的模型名/价格，不猜。
_TEXT_CLOUD = (
    _DEEPSEEK,
    _GEMINI,
    Adapter(code="volc", label="火山方舟", capability="text", backend="cloud",
            aliases=("volces", "ark", "volcengine"),
            key_env="FV_KEY_VOLC", source="设计稿 §7.3（建议供应商）",
            note="一个端点挂多家模型；模型 ID 与参数到核官方文档再填。"),
    Adapter(code="minimax", label="MiniMax", capability="text", backend="cloud",
            key_env="FV_KEY_MINIMAX", source="设计稿 §7.3（候选供应商）",
            note="模型 ID 与参数到核官方文档再填。"),
)
