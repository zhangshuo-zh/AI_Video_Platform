"""定妆图的读取入口。

原来这里是 `app.mount("/files", StaticFiles(...))`——任何人不用登录，
拿到路径就能取图。上传接口的鉴权（`ensure_asset`）就白做了：同一个文件两个门，
一个开着一个关着。现在改成带登录与项目归属判定的路由，规则与读取资产详情一致：
**能看见这个资产，就能看见它的图**。

文件名是服务端生成的 `<资产id>/<uuid>.<ext>`，猜不出来；但"猜不出来"不是访问控制，
链接一旦贴进聊天或日志就等于长期公开，所以这一道还是要过。
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from access import ensure_asset
from config import FILES_DIR
from deps import require_user
from paths import resolve_within

router = APIRouter(prefix="/files", tags=["files"])


@router.get("/{rel:path}")
def get_file(rel: str, user=Depends(require_user)) -> FileResponse:
    """按 `资产可见性` 发图。看不见的项目一律 404，不因为它存在而回 403。"""
    # 写法先自己收住：绝对路径、上跳、只有一段（资产目录本身）都不发。
    # 真正的边界复核在下面交给 paths.resolve_within——它按 root + os.sep 比，
    # 不会把 data/files 匹配给 data/files-evil。
    parts = Path(rel).parts
    if not rel or rel.startswith("/") or ".." in parts or len(parts) < 2:
        raise HTTPException(status_code=404, detail="文件不存在")
    # 第一段就是资产 id：图按资产分目录存，归属可以直接推出来。
    # 先判可见性再看文件在不在——反过来的话，"这个路径存在"本身就成了
    # 给没权限的人的一个探测口（存在回 404 的另一种写法、不存在回 404）
    asset_id = parts[0]
    if not asset_id.isdigit():
        raise HTTPException(status_code=404, detail="文件不存在")
    ensure_asset(int(asset_id), user, "read")

    target = resolve_within(FILES_DIR, rel)
    if target is None or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    media = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "webp": "image/webp"}.get(target.suffix.lstrip(".").lower())
    if media is None:
        # 上传接口只写这三种扩展名；别的要么是手工塞进目录的文件，要么是脏数据
        raise HTTPException(status_code=404, detail="文件不存在")
    # 不缓存：换封面后同一 URL 会被换成新图，no-store 与前端静态资源同一口径
    return FileResponse(target, media_type=media,
                        headers={"Cache-Control": "no-store"})
