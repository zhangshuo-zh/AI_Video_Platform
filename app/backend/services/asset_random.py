"""Randomly draft a complete character asset profile using configured text models."""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from threading import BoundedSemaphore
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag

import chatgpt_plan
import chatgpt_usage
import cloud_run
import secret_store
from repositories.content import assets
from repositories.engine_instances import engine_instances
from repositories.model_center import model_center
from repositories.providers import providers as provider_repo
from schemas.assets import AssetRandomCharacterIn, AssetRandomCharacterOut

from .asset_vision_transport import _VisionError, _request
from .content_operations import ContentOperationError

_MAX_REQUESTS = BoundedSemaphore(4)
_TOTAL_SECONDS = 90.0
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
        f"创作灵感种子：{base}{avoid_line}"
    )


def _resolve_secret(env_name: str) -> str:
    try:
        value, _ = secret_store.resolve(env_name)
        return value.strip()
    except (secret_store.SecretStoreUnavailable, InvalidTag, ValueError, TypeError):
        return ""


def _ollama_endpoint_and_model(explicit_model: str | None, deadline: float) -> tuple[str, str]:
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
        try:
            tags = _request(endpoint + "/api/tags", label="Ollama", deadline=deadline, idle=2.5)
        except _VisionError:
            continue
        models = [m.get("name") for m in (tags.get("models") or []) if isinstance(m, dict) and isinstance(m.get("name"), str)]
        if not models:
            continue
        if explicit_model:
            hit = next((m for m in models if m == explicit_model or m == explicit_model + ":latest"), None)
            if hit:
                return endpoint, hit
        return endpoint, models[0]
    raise _VisionError(503, "未找到可用的 Ollama 文本模型，请检查本地实例或切换云端通道", unavailable=True)


def _run_ollama(instruction: str, model: str | None, deadline: float) -> tuple[str, str]:
    endpoint, chosen = _ollama_endpoint_and_model(model, min(deadline, time.monotonic() + 6))
    result = _request(endpoint + "/api/chat", label="Ollama", deadline=deadline, idle=45, body={
        "model": chosen,
        "messages": [{"role": "user", "content": instruction}],
        "format": "json", "think": False, "stream": False,
        "options": {"temperature": 0.9, "num_predict": 1200},
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
    result = _request(f"{base}/chat/completions", label="DeepSeek", deadline=deadline, idle=45, token=key, body={
        "model": chosen,
        "messages": [{"role": "user", "content": instruction}],
        "temperature": 0.9,
        "max_tokens": 1200,
        "response_format": {"type": "json_object"},
    })
    choices = result.get("choices") if isinstance(result, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    msg = first.get("message") if isinstance(first, dict) else None
    text = msg.get("content") if isinstance(msg, dict) and isinstance(msg.get("content"), str) else ""
    return (text if key not in text else ""), chosen


def _run_gemini(instruction: str, model: str | None, deadline: float) -> tuple[str, str]:
    key = _resolve_secret("GEMINI_API_KEY")
    if not key:
        raise _VisionError(424, "未配置 GEMINI_API_KEY", unavailable=True)
    chosen = model or _DEFAULT_GEMINI_MODEL
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", chosen):
        raise _VisionError(422, "Gemini 模型名称格式无效")
    base = (os.environ.get("GEMINI_BASE_URL") or cloud_run.DEFAULT_GEMINI_BASE).rstrip("/")
    gen_config: dict = {"temperature": 0.9, "maxOutputTokens": 1200, "responseMimeType": "application/json"}
    tcfg = cloud_run.gemini_thinking_config(chosen, thinking=False)
    if tcfg:
        gen_config["thinkingConfig"] = tcfg
    result = _request(
        f"{base}/v1beta/models/{chosen}:generateContent",
        label="Gemini", deadline=deadline, idle=45,
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
                          idle=45, responses=True, token=token, metrics=metrics, body={
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
    raw_tags = obj.get("tags") if isinstance(obj.get("tags"), list) else []
    tags = [str(t).strip()[:20] for t in raw_tags if str(t).strip()][:10]
    raw_aliases = obj.get("aliases") if isinstance(obj.get("aliases"), list) else []
    aliases = [str(a).strip()[:30] for a in raw_aliases if str(a).strip()][:6]
    if not name or not prompt:
        raise _VisionError(502, "模型生成的角色名称或提示词为空，请点击再试一次")
    return {
        "name": name, "summary": summary, "tags": tags,
        "aliases": aliases, "consistency_prompt": prompt,
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
                     payload: AssetRandomCharacterIn) -> AssetRandomCharacterOut:
    if not _MAX_REQUESTS.acquire(blocking=False):
        raise ContentOperationError(503, "当前 AI 生成请求较多，请稍后重试")
    try:
        deadline = time.monotonic() + _TOTAL_SECONDS
        existing = [str(r["name"]) for r in assets.list_visible(project_id, kind="character")
                    if r["name"]]
        instruction = _seed_hint(payload.theme, existing)
        mode = payload.execution_mode
        provider = (payload.provider_code or "").strip().lower()
        model = (payload.model_code or "").strip() or None

        def finish(text_and_model: tuple[str, str], used_provider: str) -> AssetRandomCharacterOut:
            raw, used_model = text_and_model
            parsed = _parse_character_json(raw)
            return AssetRandomCharacterOut(provider=used_provider, model=used_model, **parsed)

        if mode == "local" or provider == "ollama":
            return finish(_run_ollama(instruction, model, deadline), "ollama")

        if mode == "cloud":
            target_p = provider or _pick_cloud_provider()[0]
            target_m = model or _pick_cloud_provider()[1]
            if target_p == "deepseek":
                return finish(_run_deepseek(instruction, target_m, deadline), "deepseek")
            if target_p == "gemini":
                return finish(_run_gemini(instruction, target_m, deadline), "gemini")
            if target_p == "chatgpt":
                return finish(_run_chatgpt(user_id, project_id, instruction, target_m, deadline), "chatgpt")
            raise ContentOperationError(422, f"随机角色暂不支持云端通道「{target_p or '未配置'}」，请切回本地 Ollama、DeepSeek 或 Gemini")

        # auto: 本地 Ollama 优先，其次已配置的云端文本通道
        try:
            return finish(_run_ollama(instruction, model, deadline), "ollama")
        except _VisionError as exc:
            if not exc.unavailable:
                raise
        cloud_p, cloud_m = _pick_cloud_provider()
        if cloud_p == "deepseek" and _resolve_secret("DEEPSEEK_API_KEY"):
            return finish(_run_deepseek(instruction, cloud_m, deadline), "deepseek")
        if cloud_p == "gemini" and _resolve_secret("GEMINI_API_KEY"):
            return finish(_run_gemini(instruction, cloud_m, deadline), "gemini")
        if _resolve_secret("DEEPSEEK_API_KEY"):
            return finish(_run_deepseek(instruction, None, deadline), "deepseek")
        if _resolve_secret("GEMINI_API_KEY"):
            return finish(_run_gemini(instruction, None, deadline), "gemini")
        try:
            return finish(_run_chatgpt(user_id, project_id, instruction, None, deadline), "chatgpt")
        except _VisionError as exc:
            if not exc.unavailable:
                raise
        raise _VisionError(503, "暂无可用的文本模型通道，请先在「模型中心」配置 Ollama 或云端文本模型")
    finally:
        _MAX_REQUESTS.release()
