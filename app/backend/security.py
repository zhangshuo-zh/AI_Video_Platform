"""口令与会话的安全原语。

口令只存 scrypt 的派生值，存成自描述字符串：
    scrypt$16384$8$1$<salt_hex>$<hash_hex>
参数写进串里的好处是以后调强度，老口令仍能验；不用迁移表结构。
"""
import hashlib
import hmac
import secrets

# 强度参数：n=2^14 在本机约 35ms 一次，登录场景够用又不至于卡人
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_LEN,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """任何格式异常都当验证失败，不抛异常，避免把内部细节漏给调用方。"""
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(hash_hex)),
        )
        return hmac.compare_digest(digest, bytes.fromhex(hash_hex))
    except (ValueError, TypeError):
        return False


def new_session_token() -> tuple[str, str]:
    """返回 (明文 token, 入库用的 sha256)。明文只出现在 cookie 里。"""
    token = secrets.token_urlsafe(32)
    return token, hash_session_token(token)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
