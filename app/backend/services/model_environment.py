"""模型中心运行时路径与密钥配置业务；不依赖 HTTP 框架。"""

import os
import adapters
import cloud_probe
from models import (RuntimePathIn, RuntimePathItem, RuntimePathsOut,
                    SecretItem, SecretProbeOut, SecretsOut,
                    SecretValueIn, SecretValueOut, SecretValuesOut)
from repositories.model_center import model_center
from repositories import secret_value

from .model_configuration import ModelOperationError


def list_runtime_paths() -> RuntimePathsOut:
    """本地运行时路径：每一项三份值 + 探测状态。

    `state` 用 `runtime` 的真探测（文件系统检查，不加载权重、不发网络请求），
    三档 ok／missing／unknown 与 `adapters.health` 共用一套词：
      * ok      —— 配了且东西真的在
      * missing —— 配了但路径不在（人要做的事：改路径或把东西搬过去）
      * unknown —— 没配（人要做的事：配一个）
    把 missing 说成 unknown 会让人以为"还没配"，而去配一个其实配错的地方。
    """
    import runtime_paths
    items = []
    for it in runtime_paths.describe():
        state, detail = _runtime_state(it["env_name"], it["effective"])
        items.append(RuntimePathItem(**it, state=state, detail=detail))
    return RuntimePathsOut(
        items=items,
        worker_needs_restart=False,
        note="环境变量优先于页面；被环境占住的那几项页面改不动。"
             "改完路径后 worker 需要重启才会用新值。")


def _runtime_state(env_name: str, effective: str | None) -> tuple[str, str]:
    """把"这个值到底能不能用"问出来。返回 (state, detail)。

    只用文件系统检查，与 `runtime.py` 同一档纪律。认不出属于哪个引擎的变量
    （理论上不会有，白名单是固定的）就回 unknown，不猜。
    """
    if not effective:
        return "unknown", "还没配置"
    from pathlib import Path
    p = Path(effective).expanduser()
    # `*_BIN` 是可执行文件，其余是目录——判据不同，用名字区分而不是猜
    if env_name.endswith("_BIN"):
        if p.is_file():
            return "ok", str(p)
        return "missing", f"{p} 不是一个存在的文件"
    if p.is_dir():
        return "ok", str(p)
    if p.exists():
        return "missing", f"{p} 存在但不是目录"
    return "missing", f"{p} 不存在"


def save_runtime_path(env_name: str, payload: RuntimePathIn, user) -> RuntimePathsOut:
    """保存一项。空字符串 = 清除覆盖（回到跟随环境/默认）。

    **不在这里重启 worker**：重启会 `_terminate` 正在跑的子进程，让作业记成
    `worker_stopped`。那是人该做的决定（页面上有独立的重启按钮），
    不能因为"顺手保存一下路径"就把人家跑着的视频掐掉。

    环境变量占住的那一项允许写库（写进去将来环境没了就用得上），但会如实
    告诉调用方"当前不生效"——`locked=True` 就是那句话。
    """
    import runtime_paths
    from repositories import runtime_path

    try:
        saved = runtime_path.save(env_name, payload.path,
                                  updated_by=user.get("id") if isinstance(user, dict) else None)
    except ValueError as exc:
        # 不在白名单（含任何密钥类变量名）：415 太怪，用 422 表达"这个字段不接受"
        raise ModelOperationError(status_code=422, detail=str(exc)) from exc
    # 顺手记一笔变更痕（0044 那张表只追加）。subject 取值受它自己的 CHECK 限制，
    # 这里借 `model_policy` 之外没有更合适的档，所以**不硬塞**——配置变更本就有
    # `updated_at`/`updated_by` 两列可查，为一条路径去改 0044 的 CHECK 不划算。
    runtime_paths.apply_one(env_name, saved)

    items = []
    for it in runtime_paths.describe():
        state, detail = _runtime_state(it["env_name"], it["effective"])
        items.append(RuntimePathItem(**it, state=state, detail=detail))
    return RuntimePathsOut(
        items=items,
        worker_needs_restart=True,
        note="已保存。**正在跑的 worker 还在用旧值**——它拿的是启动那一刻的环境快照，"
             "要重启 worker 才会用新路径。重启会中断当前作业（可重试）。")


def _cloud_key_items() -> list[dict]:
    """所有云端能力登记过的 (provider, key_env)，按 provider 去重。

    为什么从**登记表**取而不是把 `adapters.ADAPTERS` 全列一遍：清单里有
    kling／seedance／jimeng 这类"已声明但没接"的档，把它们的空 key 摆一屏
    会让人以为平台支持那些供应商。登记过的才是真的在用。
    """
    seen: set[str] = set()
    out: list[dict] = []
    for row in model_center.registrations():
        if row["backend"] != "cloud":
            continue
        adapter = adapters.find(row["capability"], "cloud", row["provider"])
        if adapter is None or adapter.code in seen:
            continue
        seen.add(adapter.code)
        env_name = adapters.resolved_key_env(adapter.code, adapter.capability, "cloud")
        out.append({"env_name": env_name or "", "provider": adapter.code,
                    "label": adapter.label})
    return out


def list_secrets() -> SecretsOut:
    """云端密钥的**指纹**：能不能确认是哪一把、以及它现在有没有配。

    **只给指纹，不给值。** 指纹 = `sha256(值)` 前 8 位，推不回原值；
    `preview` 只露供应商公开的家族标记（`sk-` 这类）。长度也给——
    复制粘贴时多个换行或少几个字符，人对一下长度就能发现。
    """
    items = []
    for it in _cloud_key_items():
        fp = adapters.key_fingerprint(it["env_name"])
        items.append(SecretItem(
            env_name=it["env_name"], provider=it["provider"], label=it["label"],
            present=fp["present"], length=fp.get("length", 0),
            fingerprint=fp.get("fingerprint", ""), preview=fp.get("preview", "")))
    return SecretsOut(items=items, note=(
        "指纹是 sha256 的前 8 位，用来确认「是哪一把 key」，推不回原值。"
        "**改了密钥后 API 进程立刻看到新值（这一页会变），但 worker 不会**——"
        "它拿的是启动那一刻的环境快照，必须重启 worker 云端作业才会用新 key。"
        "若改的是 ~/.zshrc，要重启 run.sh 才能同时刷新两者。"))


def test_secret(provider: str) -> SecretProbeOut:
    """用**当前进程环境里**的那把 key 发一次最小请求，回答「这把到底能不能用」。

    复用 `cloud_probe.probe_one(fresh=True)`：它已经实现了"没配密钥不发请求"
    （纪律 2）、地址闸（`safenet.check_url`＋不跟随重定向）、错误码归一
    （401/403 钥匙问题、402 欠费、429 限流）。**`fresh=True` 是关键**：
    默认有 15 秒缓存，而人刚 export 完新 key 就来点测试，拿到旧结论会以为没生效。

    注意这是**真出网**的调用（会打供应商网关）。它不做任何生成、不花钱，
    但确实是一次外部请求——所以只对 `system:config` 开放。
    """
    adapter = None
    for cap in ("text", "image", "video", "tts", "music"):
        adapter = adapters.find(cap, "cloud", provider)
        if adapter is not None:
            break
    if adapter is None:
        raise ModelOperationError(status_code=404, detail=f"云端清单里没有「{provider}」这一家")
    out = cloud_probe.probe_one(adapter, fresh=True)
    return SecretProbeOut(
        provider=out["provider"], label=out["label"], state=out["state"],
        detail=out["detail"], key_env=out.get("key_env", ""),
        has_key=bool(out.get("has_key")))


def _secret_out(env_name: str) -> SecretValueOut:
    """一把密钥的**显示态**：不解密，也不含明文。"""
    env_value = (os.environ.get(env_name) or "").strip()
    row = secret_value.get_row(env_name)
    out = SecretValueOut(env_name=env_name)
    if env_value:
        # 环境里的值可以现算指纹（不落库）：这样页面能显示"环境里那把是哪一把"
        fp = adapters.key_fingerprint(env_name)
        out.present = True
        out.source = "env"
        out.fingerprint = fp.get("fingerprint", "")
        out.length = fp.get("length", 0)
        out.preview = fp.get("preview", "")
        out.note = "当前生效的是环境变量；库里的值（若有）不生效"
        return out
    if row is not None:
        out.present = True
        out.source = "db"
        out.fingerprint = row["fingerprint"]
        out.length = int(row["length"] or 0)
        out.preview = adapters._preview_for_db(row["fingerprint"])
        out.updated_at = row["updated_at"]
        out.updated_by = row["updated_by"]
        out.note = str(row["note"] or "")
    return out


def list_secret_values() -> SecretValuesOut:
    """每个云端密钥的显示态：有没有配、值在哪（环境/库）、指纹。

    **一个字符的明文都不返回。** 页面拿指纹去和供应商后台比对就够了。
    """
    import secret_store
    items = [_secret_out(it["env_name"]) for it in _cloud_key_items()]
    return SecretValuesOut(
        items=items,
        kek_ready=secret_store.available(),
        kek_env=secret_store.KEK_ENV,
        note=("密钥以 AES-256-GCM 密文存在库里，主密钥只在环境变量 "
              f"`{secret_store.KEK_ENV}`。**环境变量优先**：同一把密钥若在环境里"
              "也有值，以环境里的为准，库里的不生效。保存后**下一单立即生效**，"
              "不需要重启 worker（读取发生在作业执行时）。"))


def save_secret_value(env_name: str, payload: SecretValueIn, user) -> SecretValueOut:
    """保存一把密钥（**密文落库**）。收明文、存密文、回指纹，不回明文。

    KEK 没配时 409：宁可不收，也不写进一条将来解不开的行。
    """
    import secret_store
    if not secret_store.available():
        raise ModelOperationError(status_code=409, detail=(
            f"没有配置 {secret_store.KEK_ENV}，密钥无法加密，所以没有保存。"
            f"生成方法：python3 -c \"import base64,secrets;"
            f"print(base64.b64encode(secrets.token_bytes(32)).decode())\" "
            f"然后把结果写进环境变量并重启 API。"))
    value = (payload.value or "").strip()
    if not value:
        raise ModelOperationError(status_code=400, detail="密钥不能为空；要清除请用 DELETE")
    try:
        secret_value.save(env_name, value, updated_by=user["id"],
                          note="网页保存")
    except ValueError as exc:
        raise ModelOperationError(status_code=400, detail=str(exc)) from exc
    except secret_store.SecretStoreUnavailable as exc:
        raise ModelOperationError(status_code=409, detail=str(exc)) from exc
    return _secret_out(env_name)


def delete_secret_value(env_name: str) -> SecretValueOut:
    """删掉库里的值（回落到"只认环境变量"）。删一个本来就没有的不算错误。"""
    try:
        secret_value.forget(env_name)
    except ValueError as exc:
        raise ModelOperationError(status_code=400, detail=str(exc)) from exc
    return _secret_out(env_name)
