"""Randomly draft a complete character asset profile using configured text models."""
from __future__ import annotations

from contextvars import ContextVar
import json
import os
import re
import secrets
import time
from threading import BoundedSemaphore, Lock
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag

import chatgpt_plan
import chatgpt_usage
import cloud_run
import engine_launch
import secret_store
from repositories.content import assets
from repositories.engine_instances import engine_instances
from repositories.model_center import model_center
from repositories.providers import providers as provider_repo
from schemas.assets import AssetRandomCharacterIn, AssetRandomCharacterOut

from .asset_vision_transport import _VisionError, _request
from .content_operations import ContentOperationError

_MAX_REQUESTS = BoundedSemaphore(4)
_OLLAMA_LAUNCH_LOCK = Lock()
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ALLOW_AUTO_START: ContextVar[bool] = ContextVar("asset_random_allow_auto_start", default=False)
_OLLAMA_STARTED: ContextVar[bool] = ContextVar("asset_random_ollama_started", default=False)
_TOTAL_SECONDS = 110.0
_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
_DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
_CREDENTIAL = re.compile(r"\b(?:authorization\s*[:=]|bearer\s+|sk-[a-z0-9_-]{12,})", re.I)

_THEMES = (
    "赛博朋克都市", "蒸汽朋克工坊", "新中式仙侠", "星际航行舰队", "现代奇幻学园",
    "架空维多利亚侦探", "废土机械拾荒", "海洋魔导纪元", "近未来赛博偶像", "日式和风妖怪谭",
)
_ROLES = (
    "外冷内热的机关师", "毒舌但护短的情报贩子", "元气满满的新手实习生", "失忆的古代剑灵",
    "喜欢甜食的星舰领航员", "神秘的夜间咖啡店老板", "古灵精怪的炼金学徒", "冷静理性的战术分析师",
)
_ACCENTS = (
    "异色双瞳与金属耳饰", "挑染长发与不对称披风", "随身怀表与护目镜", "精致发簪与刺绣袖口",
    "荧光纹饰外套与露指手套", "双马尾与机械尾巴配饰", "围巾与左臂徽章", "渐变发色与复古背包",
)


def _seed_hint(theme: str, existing_names: list[str]) -> str:
    clean = theme.strip()
    base = clean if clean else f"{secrets.choice(_THEMES)} · {secrets.choice(_ROLES)} · {secrets.choice(_ACCENTS)}"
    avoid = "、".join(existing_names[:20])
    avoid_line = f"\n避免与已有角色重名：{avoid}" if avoid else ""
    return (
        "你是一位专业动漫角色设定师。请根据灵感随机创作一位辨识度极高的原创角色，只输出严格 JSON 对象，不要输出任何额外说明或 Markdown。\n"
        "JSON 字段要求：\n"
        '- "name": 简短好记的角色名（2-12 字）\n'
        '- "summary": 一两句话概括性格、身份与故事感（40-180 字）\n'
        '- "tags": 3-5 个简短标签数组（每个 2-6 字，如 ["赛博朋克", "异瞳", "黑客"]）\n'
        '- "aliases": 1-2 个外号或简称数组\n'
        '- "consistency_prompt": 用于 AI 绘图的跨镜头角色一致性提示词（120-320 字，中文），'
        "必须按顺序包含：画风质感、脸型五官与瞳色、发型与发色、全身服装层次与配色材质、标志性配饰，以及正面/侧面/背面一致的全身比例特征，不要包含一次性动作或背景。\n"
        '- "image_prompt_en": 与 consistency_prompt 对应的纯英文生图提示词（50-140 词，英文逗号分隔短语，供 FLUX/SDXL 等英文生图模型使用，必须包含画风、性别/体型、发色发型、瞳色五官、服装配色材质与配饰，不要包含中文或背景）。\n'
        f"创作灵感种子：{base}{avoid_line}"
    )


def _resolve_secret(env_name: str) -> str:
    try:
        value, _ = secret_store.resolve(env_name)
        return value.strip()
    except (secret_store.SecretStoreUnavailable, InvalidTag, ValueError, TypeError):
        return ""


def _match_ollama_model(models: list[str], explicit_model: str) -> str | None:
    norm = explicit_model.strip()
    if not norm:
        return None
    alt = norm[:-7] if norm.endswith(":latest") else f"{norm}:latest"
    return next((m for m in models if m == norm or m == alt), None)


def _pick_ollama_model(models: list[str], explicit_model: str | None) -> str:
    if explicit_model:
        hit = _match_ollama_model(models, explicit_model)
        if hit:
            return hit
    return models[0]


def _extract_ollama_models(tags: object) -> list[str]:
    if not isinstance(tags, dict):
        return []
    raw_models = tags.get("models")
    if not isinstance(raw_models, list):
        return []
    out: list[str] = []
    for m in raw_models:
        if isinstance(m, dict):
            name = m.get("name")
            if isinstance(name, str):
                out.append(name)
    return out


def _ollama_endpoint_and_model(explicit_model: str | None, deadline: float) -> tuple[str, str]:
    candidates: list[tuple[str, bool]] = []
    online_with_models = False
    for inst in engine_instances.instances():
        if inst["engine_code"] != "ollama" or not inst["enabled"]:
            continue
        endpoint = str(inst["endpoint"] or "").rstrip("/")
        if not endpoint:
            continue
        try:
            parsed = urlsplit(endpoint)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                continue
        except ValueError:
            continue
        is_local = (parsed.hostname or "").lower() in _LOOPBACK_HOSTS
        candidates.append((endpoint, is_local))
        try:
            tags = _request(endpoint + "/api/tags", label="Ollama", deadline=deadline, idle=2.5)
        except _VisionError:
            continue
        models = _extract_ollama_models(tags)
        if models:
            online_with_models = True
            if explicit_model:
                hit = _match_ollama_model(models, explicit_model)
                if hit:
                    return endpoint, hit
                continue
            return endpoint, models[0]

    launch_note = ""
    if _ALLOW_AUTO_START.get():
        local_endpoints = [ep for ep, is_local in candidates if is_local]
        for endpoint in local_endpoints:
            with _OLLAMA_LAUNCH_LOCK:
                result = engine_launch.launch("ollama", endpoint)
            if not result.reachable:
                launch_note = result.message
                continue
            try:
                tags = _request(endpoint + "/api/tags", label="Ollama",
                                deadline=min(deadline, time.monotonic() + 5), idle=3.0)
            except _VisionError:
                continue
            models = _extract_ollama_models(tags)
            if models:
                online_with_models = True
                if result.started:
                    _OLLAMA_STARTED.set(True)
                if explicit_model:
                    hit = _match_ollama_model(models, explicit_model)
                    if hit:
                        return endpoint, hit
                    continue
                return endpoint, models[0]
            launch_note = "已启动本地 Ollama，但尚未拉取任何文本模型"

    if explicit_model and online_with_models:
        raise _VisionError(
            422,
            f"已启用的 Ollama 实例中未找到指定模型「{explicit_model}」，请确认模型名称或先拉取该模型",
        )
    if launch_note:
        raise _VisionError(
            503,
            f"未找到可用的 Ollama 文本模型（自动拉起结果：{launch_note}），请检查模型或切换云端通道",
            unavailable=True,
        )
    raise _VisionError(503, "未找到可用的 Ollama 文本模型，请检查本地实例或切换云端通道", unavailable=True)


def _run_ollama(instruction: str, model: str | None, deadline: float) -> tuple[str, str]:
    endpoint, chosen = _ollama_endpoint_and_model(model, min(deadline, time.monotonic() + 6))
    result = _request(endpoint + "/api/chat", label="Ollama", deadline=deadline, idle=90, body={
        "model": chosen,
        "messages": [{"role": "user", "content": instruction}],
        "format": "json", "think": False, "stream": False,
        "options": {"temperature": 0.9, "num_predict": 2048},
    })
    if not isinstance(result, dict):
        raise _VisionError(502, "Ollama 返回格式无效")
    msg = result.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return (content if isinstance(content, str) else ""), chosen


def _run_deepseek(instruction: str, model: str | None, deadline: float) -> tuple[str, str]:
    key = _resolve_secret("DEEPSEEK_API_KEY")
    if not key:
        raise _VisionError(424, "未配置 DEEPSEEK_API_KEY", unavailable=True)
    chosen = model or _DEFAULT_DEEPSEEK_MODEL
    base = (os.environ.get("DEEPSEEK_BASE_URL") or cloud_run.DEFAULT_DEEPSEEK_BASE).rstrip("/")
    result = _request(f"{base}/chat/completions", label="DeepSeek", deadline=deadline, idle=60, token=key, body={
        "model": chosen,
        "messages": [{"role": "user", "content": instruction}],
        "temperature": 0.9,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
    })
    choices = result.get("choices") if isinstance(result, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    msg = first.get("message") if isinstance(first, dict) else None
    raw_content = msg.get("content") if isinstance(msg, dict) else None
    text = raw_content if isinstance(raw_content, str) else ""
    return (text if key not in text else ""), chosen


def _run_gemini(instruction: str, model: str | None, deadline: float) -> tuple[str, str]:
    key = _resolve_secret("GEMINI_API_KEY")
    if not key:
        raise _VisionError(424, "未配置 GEMINI_API_KEY", unavailable=True)
    chosen = model or _DEFAULT_GEMINI_MODEL
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", chosen):
        raise _VisionError(422, "Gemini 模型名称格式无效")
    base = (os.environ.get("GEMINI_BASE_URL") or cloud_run.DEFAULT_GEMINI_BASE).rstrip("/")
    gen_config: dict = {"temperature": 0.9, "maxOutputTokens": 2048, "responseMimeType": "application/json"}
    tcfg = cloud_run.gemini_thinking_config(chosen, thinking=False)
    if tcfg:
        gen_config["thinkingConfig"] = tcfg
    result = _request(
        f"{base}/v1beta/models/{chosen}:generateContent",
        label="Gemini", deadline=deadline, idle=60,
        extra_headers={"x-goog-api-key": key},
        body={"contents": [{"role": "user", "parts": [{"text": instruction}]}], "generationConfig": gen_config},
    )
    text, _reason, _ = cloud_run.gemini_extract_text(result)
    return (text if key not in text else ""), chosen


def _run_chatgpt(user_id: int, project_id: int, instruction: str,
                 model: str | None, deadline: float) -> tuple[str, str]:
    try:
        token = chatgpt_plan.access_token(user_id, timeout=15)
    except (OSError, ValueError, TypeError, KeyError):
        raise _VisionError(424, "ChatGPT 授权不可用", unavailable=True) from None
    chosen = model or "gpt-5.1"
    metrics: dict = {}
    started = time.monotonic()
    state = "failed"
    try:
        result = _request(f"{chatgpt_plan.RESOURCE}/responses", label="ChatGPT", deadline=deadline,
                          idle=60, responses=True, token=token, metrics=metrics, body={
            "model": chosen, "store": False, "stream": True,
            "instructions": "根据用户提示创作动漫角色设定，只输出严格 JSON 对象。",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": instruction}]}],
        })
        state = "ready"
        return (result if isinstance(result, str) and token not in result else ""), chosen
    finally:
        chatgpt_usage.record(
            user_id, project_id, chosen, state=state, source="random_character",
            duration_ms=max(0, round((time.monotonic() - started) * 1000)), metrics=metrics,
        )


def _parse_character_json(raw: str) -> dict:
    cleaned = re.sub(r"\A\s*```[\w-]*\s*|\s*```\s*\Z", "", (raw or "").strip())
    if not cleaned:
        raise _VisionError(502, "模型返回内容为空，请重试")
    if _CREDENTIAL.search(cleaned):
        raise _VisionError(502, "模型返回了无法安全使用的内容")
    try:
        obj = json.loads(cleaned)
    except (ValueError, RecursionError):
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise _VisionError(502, "模型未返回有效 JSON 格式的角色设定，请重试") from None
        try:
            obj = json.loads(match.group(0))
        except (ValueError, RecursionError):
            raise _VisionError(502, "模型返回的角色 JSON 无法解析，请重试") from None
    if not isinstance(obj, dict):
        raise _VisionError(502, "模型返回格式无效，请重试")
    name = str(obj.get("name") or "").strip()[:60]
    summary = str(obj.get("summary") or "").strip()[:500]
    prompt = str(obj.get("consistency_prompt") or "").strip()[:1000]
    prompt_en = str(obj.get("image_prompt_en") or "").strip()[:1000]
    maybe_tags = obj.get("tags")
    raw_tags: list = maybe_tags if isinstance(maybe_tags, list) else []
    tags = [str(t).strip()[:20] for t in raw_tags if str(t).strip()][:10]
    maybe_aliases = obj.get("aliases")
    raw_aliases: list = maybe_aliases if isinstance(maybe_aliases, list) else []
    aliases = [str(a).strip()[:30] for a in raw_aliases if str(a).strip()][:6]
    if not name or not prompt:
        raise _VisionError(502, "模型生成的角色名称或提示词为空，请点击再试一次")
    return {
        "name": name, "summary": summary, "tags": tags,
        "aliases": aliases, "consistency_prompt": prompt,
        "image_prompt_en": prompt_en,
    }


def _pick_cloud_provider() -> tuple[str, str | None]:
    slot = provider_repo.slot("text", "cloud")
    if slot is not None and slot["enabled"] and (slot["provider"] or "").strip():
        return str(slot["provider"]).strip(), (slot["model"] or "").strip() or None
    for reg in model_center.registrations():
        if reg["capability"] == "text" and reg["backend"] == "cloud" and reg["enabled"]:
            return str(reg["provider"]).strip(), (reg["model_code"] or "").strip() or None
    return "", None


def random_character(project_id: int, user_id: int,
                     payload: AssetRandomCharacterIn,
                     *, allow_auto_start: bool = False) -> AssetRandomCharacterOut:
    if not _MAX_REQUESTS.acquire(blocking=False):
        raise ContentOperationError(503, "当前 AI 生成请求较多，请稍后重试")
    allow_tok = _ALLOW_AUTO_START.set(allow_auto_start)
    started_tok = _OLLAMA_STARTED.set(False)
    try:
        deadline = time.monotonic() + _TOTAL_SECONDS
        existing = [str(r["name"]) for r in assets.list_visible(project_id, kind="character")
                    if r["name"]]
        instruction = _seed_hint(payload.theme, existing)
        mode = payload.execution_mode
        provider = (payload.provider_code or "").strip().lower()
        model = (payload.model_code or "").strip() or None

        def finish(runner, used_provider: str) -> AssetRandomCharacterOut:
            last_err: _VisionError | None = None
            for attempt in range(2):
                raw, used_model = runner()
                try:
                    parsed = _parse_character_json(raw)
                    return AssetRandomCharacterOut(
                        provider=used_provider, model=used_model,
                        ollama_auto_started=(used_provider == "ollama" and _OLLAMA_STARTED.get()),
                        **parsed,
                    )
                except _VisionError as exc:
                    if exc.status_code != 502 or attempt == 1 or (deadline - time.monotonic()) < 15:
                        raise
                    last_err = exc
            assert last_err is not None
            raise last_err

        if mode == "local" or provider == "ollama":
            return finish(lambda: _run_ollama(instruction, model, deadline), "ollama")

        if mode == "cloud":
            default_p, default_m = _pick_cloud_provider()
            target_p = provider or default_p
            target_m = model or default_m
            if target_p == "deepseek":
                return finish(lambda: _run_deepseek(instruction, target_m, deadline), "deepseek")
            if target_p == "gemini":
                return finish(lambda: _run_gemini(instruction, target_m, deadline), "gemini")
            if target_p == "chatgpt":
                return finish(lambda: _run_chatgpt(user_id, project_id, instruction, target_m, deadline), "chatgpt")
            raise ContentOperationError(422, f"随机角色暂不支持云端通道「{target_p or '未配置'}」，请切回本地 Ollama、DeepSeek 或 Gemini")

        # auto: 本地 Ollama 优先，其次已配置的云端文本通道
        try:
            return finish(lambda: _run_ollama(instruction, model, deadline), "ollama")
        except _VisionError as exc:
            if not exc.unavailable:
                raise
        cloud_p, cloud_m = _pick_cloud_provider()
        if cloud_p == "deepseek" and _resolve_secret("DEEPSEEK_API_KEY"):
            return finish(lambda: _run_deepseek(instruction, cloud_m, deadline), "deepseek")
        if cloud_p == "gemini" and _resolve_secret("GEMINI_API_KEY"):
            return finish(lambda: _run_gemini(instruction, cloud_m, deadline), "gemini")
        if _resolve_secret("DEEPSEEK_API_KEY"):
            return finish(lambda: _run_deepseek(instruction, None, deadline), "deepseek")
        if _resolve_secret("GEMINI_API_KEY"):
            return finish(lambda: _run_gemini(instruction, None, deadline), "gemini")
        try:
            return finish(lambda: _run_chatgpt(user_id, project_id, instruction, None, deadline), "chatgpt")
        except _VisionError as exc:
            if not exc.unavailable:
                raise
        raise _VisionError(503, "暂无可用的文本模型通道，请先在「模型中心」配置 Ollama 或云端文本模型")
    except _VisionError as exc:
        if exc.status_code == 504 and ("看图" in exc.detail or "参考图" in exc.detail):
            raise ContentOperationError(
                504, "生成角色人设超时，本地模型首次加载可能较慢，请稍后重试或切换其他文本模型",
            ) from None
        raise
    finally:
        _ALLOW_AUTO_START.reset(allow_tok)
        _OLLAMA_STARTED.reset(started_tok)
        _MAX_REQUESTS.release()
