import adapters

from models import CAPABILITIES
from repositories.providers import providers as provider_repo

from .errors import NoRoute


def resolve_provider(capability: str, execution_mode: str | None = None,
                     *, confirmed_cloud: bool = False) -> dict:
    """按能力解析执行方案：默认轨，或调用方显式指定的那一轨。

    三条不肯让步的规则：
    1. 不传 execution_mode 时只认 is_default 那一条；它没配好就报错，不补位。
    2. 视频的云端路线必须同时满足"显式指定 + 人已确认付费"，
       默认轨永远走不到云端（保存配置时也不许把云端设成默认）。
    3. provider 没有登记 adapter 也报错——没有参数白名单就生成命令，
       等于让模型名去猜运行时，规格 01-4.8.2 明令禁止。
    """
    if capability not in CAPABILITIES:
        raise NoRoute(f"不认识的能力：{capability}")

    if execution_mode:
        if execution_mode not in ("local", "cloud"):
            raise NoRoute(f"执行方式只有本地/云端两种：{execution_mode}")
        row = provider_repo.slot(capability, execution_mode)
        explicit = True
    else:
        row = next((r for r in provider_repo.defaults()
                    if r["capability"] == capability), None)
        explicit = False

    if row is None:
        raise NoRoute(f"{CAPABILITIES[capability]}没有配置默认后端")
    if not explicit and row["backend"] == "cloud" and capability == "video":
        raise NoRoute("云端视频只能在镜头里逐条选择，不能作为默认")
    if capability == "video" and row["backend"] == "cloud" and not confirmed_cloud:
        raise NoRoute("云端视频要逐镜头勾选并确认按供应商计费")
    if not row["provider"].strip() or not row["model"].strip():
        raise NoRoute(f"{CAPABILITIES[capability]}的"
                      f"{'本地' if row['backend'] == 'local' else '云端'}档还没填 provider/模型")
    if not row["enabled"]:
        raise NoRoute(f"{CAPABILITIES[capability]}的"
                      f"{'本地' if row['backend'] == 'local' else '云端'}档没有启用")

    adapter = adapters.find(capability, row["backend"], row["provider"])
    if adapter is None:
        raise NoRoute(f"provider「{row['provider']}」没有登记适配器，平台不会猜它的参数")

    return {"row": row, "adapter": adapter, "capability": capability,
            "backend": row["backend"], "provider": row["provider"],
            "model": row["model"], "explicit": explicit}
