"""图片类型判定：唯一的一份表。

为什么单独一个文件：上传接口原来自己握着 `ALLOWED_TYPES / MAGIC / _looks_like`，
而"从生成产物导入资产图"要的是**反方向**——产物没有客户端声明的 content-type，
只能从文件头倒推它是什么。两处各写一份魔数字节表，迟早一份放行 HEIC、
一份拒收 WebP，而这两条路收的必须是同一批文件。

所以：正向校验（客户端说了类型，拿文件头复核）与反向识别（只有字节，判它是什么）
都从这里出。规则本身一句没改，只是搬了个家。
"""
from __future__ import annotations

# 只收这三种。扩展名由服务端按类型定，不信客户端给的文件名
ALLOWED_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAGIC = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
}


def is_heic(content_type: str, head: bytes) -> bool:
    """iPhone 默认拍 HEIC，浏览器自己也显示不了，提前给人一句人话。"""
    return content_type in ("image/heic", "image/heif") or b"ftypheic" in head or b"ftypheif" in head


def looks_like(content_type: str, head: bytes) -> bool:
    """看文件头。content-type 是客户端嘴上说的，文件头才是它到底是什么。"""
    if content_type == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    magic = MAGIC.get(content_type)
    return bool(magic) and head.startswith(magic)


def sniff(head: bytes) -> str | None:
    """只有字节时倒推扩展名；认不出来返回 None（调用方据此拒绝，不猜）。"""
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(MAGIC["image/png"]):
        return "png"
    if head.startswith(MAGIC["image/jpeg"]):
        return "jpg"
    return None
