"""供应商密钥的密文存储：页面可配，但**库被拷走也解不开**（2026-11-11）。

## 这是什么，以及为什么现在才有

`adapters.py` 文件头第 3 条边界原本写死"密钥只认变量名，值永远只从进程环境读"。
那条边界不是为了"密钥不能存"本身，而是为了两件事：**不落明文盘**、**不让密钥
在应用里多出第二份真相**。2026-11-11 用户批准实现 A（新增 `cryptography`、
AES-256-GCM、KEK 走 `PLATFORM_SECRET_KEK`），这条边界随之改为：

    密钥可以存，但必须是密文；KEK 只在环境变量里，绝不入库。

改这一条的理由是"页面能配"的实际价值：否则每加一把 key 都要改 `~/.zshrc`
再重启，而那正是用户说的"不想给同事 shell"要避免的那类操作。

## 三件事必须同时成立，安全性才成立

1. **AEAD 而不是裸加密**。用 AES-GCM：密文自带完整性校验，被改一个比特就解不开
   （`InvalidTag`），不会静默解出一段垃圾明文去发请求。
2. **nonce 每次重新随机**（12 字节）。GCM 在同一个 KEK 下**绝不能复用 nonce**
   ——复用会直接泄漏两段明文的异或值，这是这套方案唯一的硬伤。所以每次 `save()`
   都新取一个，且**不做"加密同一个值就复用旧密文"的优化**（那种优化会引入
   nonce 复用，得不偿失）。
3. **AAD 绑定 `env_name`**。把 `DEEPSEEK_API_KEY` 的密文挪到 `FV_KEY_VOLC` 行上，
   解密会因为 AAD 不匹配而失败。没有这一条，库的写权限就等于"能把任一把密钥
   冒充成另一家"。

## KEK 的处理

`PLATFORM_SECRET_KEK` 是一个 32 字节密钥的 **base64** 表示（`secrets.token_bytes(32)`
再 b64）。它在环境变量里，**不在库里、不在代码里、不在日志里**。

没配 KEK 时本模块的全部功能降级为"不可用"（`available() == False`），
而不是回落到明文或缺省密钥——**缺省密钥等于没有加密**，那比明说"没配"危险得多。
`adapters.key_present()` / `cloud_run` 的读取顺序因此是：
**进程环境 > 本表解密 > 缺失**。环境变量仍然优先，这样运维永远能用一个
`export` 覆盖库里的值（排查"我明明导出了为什么不生效"比反过来好查）。

## 这个设计挡不住什么（写清楚免得被当成万能）

* 能读 `platform.db` **并且**能读进程环境（或 `.zshrc`）的人仍能拿到全部密钥。
  它挡的是"只拿到数据库文件"这一种（备份、`cp` 给人排查、误传）。
* 内存里的明文防不住（Python 字符串不可擦除）。这是所有同类方案的共同限制。
* KEK 泄漏等于全部密钥泄漏——所以它是一个需要单独保管的高价值秘密，
  长度/随机性由 `_kek()` 校验（不是 32 字节就拒绝启动，不静默截断/填充）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import secrets as _secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# `InvalidTag` 是 `unseal()` 会抛给调用方的**公开契约**，所以在这里再导出一次：
# 调用方 `from secret_store import InvalidTag` 比让他们直接 import
# cryptography 的内部路径更稳定（那是实现细节，换实现不该改调用方）。
# 加 `__all__` 而不是留一个未使用的导入，是为了让 ruff 与读者都看得出"这是故意的"。
from cryptography.exceptions import InvalidTag

__all__ = ["KEK_ENV", "SecretStoreUnavailable", "InvalidTag", "available",
           "fingerprint", "seal", "unseal", "peek", "resolve", "present"]

KEK_ENV = "PLATFORM_SECRET_KEK"
_NONCE_BYTES = 12          # GCM 标准长度
_KEY_BYTES = 32            # AES-256


class SecretStoreUnavailable(RuntimeError):
    """没有可用的 KEK（没配、或配错长度）。**不是**"密钥不存在"。"""


def _kek() -> bytes:
    """从环境变量取出 32 字节 KEK。配错就抛，**绝不静默截断或补零**。

    静默容错在这里是灾难性的：一个被截断到 32 字节的短 KEK 看起来"能用"，
    实际熵大幅下降，而没有任何迹象提示人。宁可让这个功能整个不可用。
    """
    raw = (os.environ.get(KEK_ENV) or "").strip()
    if not raw:
        raise SecretStoreUnavailable(
            f"没有配置 {KEK_ENV}：供应商密钥的密文存储不可用。"
            f"生成方法：python3 -c \"import base64,secrets;"
            f"print(base64.b64encode(secrets.token_bytes(32)).decode())\"")
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SecretStoreUnavailable(
            f"{KEK_ENV} 不是合法的 base64：{exc}") from exc
    if len(key) != _KEY_BYTES:
        raise SecretStoreUnavailable(
            f"{KEK_ENV} 解出来是 {len(key)} 字节，必须是 {_KEY_BYTES} 字节（AES-256）。"
            f"**没有做截断或补零**——那样会悄悄降低强度。请重新生成。")
    return key


def available() -> bool:
    """密文存储现在能不能用。界面据此决定显示"可配置"还是"请你配 KEK"。"""
    try:
        _kek()
        return True
    except SecretStoreUnavailable:
        return False


def fingerprint(plain: str) -> str:
    """与 `adapters.key_fingerprint` **同一算法**，两处算出的值可比对。"""
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()[:8].lower()


def seal(env_name: str, plain: str) -> dict:
    """把明文密钥封成可入库的四个字段。每次调用都用**新 nonce**。"""
    if not plain:
        raise ValueError("空密钥不该入库：清除请用 forget()")
    key = _kek()
    nonce = _secrets.token_bytes(_NONCE_BYTES)
    aad = env_name.encode("utf-8")          # 绑定变量名，防密文串门
    ct = AESGCM(key).encrypt(nonce, plain.encode("utf-8"), aad)
    return {"ciphertext": ct, "nonce": nonce, "aad": aad,
            "fingerprint": fingerprint(plain), "length": len(plain)}


def unseal(env_name: str, *, ciphertext: bytes, nonce: bytes,
           aad: bytes | None = None) -> str:
    """解回明文。密文被改、AAD 不匹配、KEK 换了 → 抛 `InvalidTag`。

    **AAD 必须由「行名」现重建，不读库里那一列。**
    这是 2026-11-11 自查抓到的真漏洞：第一版把行里存的 `aad` 直接喂回去，
    于是"把 DeepSeek 的密文挪到 FV_KEY_VOLC 行、aad 列也一起挪"能正常解开
    ——AAD 存了一份跟着密文走的副本，就完全起不到"绑定额度"的作用。
    现在只认传进来的 `env_name`，库里 `aad` 列仅作留档与排查（解开时忽略）。

    `aad` 参数保留但**不再使用**，签名里留着是为了让旧调用点不炸；
    传什么都一样，真正的绑定来自 `env_name`。
    """
    bound = env_name.encode("utf-8")
    return AESGCM(_kek()).decrypt(nonce, ciphertext, bound).decode("utf-8")


def peek(env_name: str) -> dict:
    """不解密也能读的显示信息：指纹、长度、更新时间。

    页面列密钥时走这里——**列表页没有任何理由解密**，能不解就不解。
    """
    from repositories import secret_value
    row = secret_value.get_row(env_name)
    if row is None:
        return {"present": False, "source": "none"}
    out = {"present": True, "fingerprint": row["fingerprint"],
           "length": row["length"], "updated_at": row["updated_at"],
           "updated_by": row["updated_by"]}
    # preview 与 adapters 保持同一条：只露公开家族标记 + 指纹
    import adapters
    out["preview"] = adapters._preview_for_db(
        row["fingerprint"])
    return out


def resolve(env_name: str) -> tuple[str, str]:
    """取一把密钥的**最终值**与来源：`("sk-...", "env"|"db"|"none")`。

    **进程环境永远优先**，与 `runtime._env()` 同一优先级纪律：部署脚本、
    CI、`run.sh` 的 `export` 是显式配置，网页上的值不该悄悄盖掉它。
    反过来"我在网页上设的为什么没用"比"我明明导出了为什么没用"好查得多。

    解密失败**不回落**到"没配"：那是两种不同的故障，混起来会让人去配一把
    其实配好的密钥。这里让异常冒出去，由调用方报"密钥存在但解不开"。
    """
    value = (os.environ.get(env_name) or "").strip()
    if value:
        return value, "env"
    from repositories import secret_value
    row = secret_value.get_row(env_name)
    if row is None:
        return "", "none"
    return unseal(env_name, ciphertext=row["ciphertext"], nonce=row["nonce"],
                  aad=row["aad"]), "db"


def present(env_name: str) -> bool:
    """有没有配（环境或库里）。**不含解密**，所以库里有但解不开也算 True。

    与 `adapters.key_present` 的差别就是这一点：那个只认环境。判断"能不能起
    云端作业"应该用 `resolve()`（它会把解不开变成异常），而不是这个。
    """
    if (os.environ.get(env_name) or "").strip():
        return True
    from repositories import secret_value
    return secret_value.get_row(env_name) is not None
