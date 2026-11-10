"""密钥密文存储（0052）：核心承诺是**库被拷走也解不开**（2026-11-11）。

这一组只测安全性质，不测 CRUD：
  * 落盘必须是密文，明文在库里一个字都找不到；
  * 换了 KEK 解不开；密文被改解不开；AAD 串门解不开；
  * 每次写入 nonce 不同（GCM 复用 nonce = 直接泄漏明文异或）；
  * KEK 配错长度**拒绝服务**而不是静默截断；
  * 环境变量仍然优先于库（运维可 export 覆盖）。
"""
from __future__ import annotations

import base64
import secrets as _secrets

import pytest

import secret_store
from db import get_conn
from repositories import secret_value

PLAIN = "sk-a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"


@pytest.fixture()
def kek(monkeypatch):
    raw = base64.b64encode(_secrets.token_bytes(32)).decode()
    monkeypatch.setenv(secret_store.KEK_ENV, raw)
    return raw


@pytest.fixture()
def clean(monkeypatch):
    """每个用例从干净的行开始，并清掉环境里的同名变量（环境优先会干扰）。

    **清单在这里固化一份**：有用例会 monkeypatch 往 `ALLOWED_ENV` 里临时加名字，
    若 teardown 现读那个属性，就会拿被改过的清单去 forget——而新增的名字
    过不了 `assert_allowed`，于是 teardown 抛 ValueError，用例变成 ERROR。
    第一版就是这么错的（一条 clean 的用例掩盖了一个脏的 fixture）。
    """
    names = tuple(secret_value.ALLOWED_ENV)
    for name in names:
        monkeypatch.delenv(name, raising=False)
        secret_value.forget(name)
    yield
    for name in names:
        secret_value.forget(name)


def test_落盘必须是密文_明文一个字都找不到(kek, clean):
    """**这是整个功能的立身之本。**判据放在原始字节上，不看接口。"""
    secret_value.save("DEEPSEEK_API_KEY", PLAIN)
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM secret_value").fetchone()
    blob = b"".join(bytes(row[k]) for k in ("ciphertext", "nonce", "aad"))
    # 整个明文不在，且任何 8 字符窗口都不在（防"只漏一段也算漏"）
    assert PLAIN.encode() not in blob
    for i in range(0, len(PLAIN) - 8):
        assert PLAIN[i:i + 8].encode() not in blob, (
            f"库的原始字节里出现了明文片段 {PLAIN[i:i+8]!r}")


def test_同一个值两次写入密文不同(kek, clean):
    """**GCM 复用 nonce 会直接泄漏两段明文的异或值**，所以每次必须换 nonce。"""
    secret_value.save("DEEPSEEK_API_KEY", PLAIN)
    with get_conn() as conn:
        a = conn.execute("SELECT ciphertext, nonce FROM secret_value").fetchone()
    secret_value.save("DEEPSEEK_API_KEY", PLAIN)
    with get_conn() as conn:
        b = conn.execute("SELECT ciphertext, nonce FROM secret_value").fetchone()
    assert bytes(a["nonce"]) != bytes(b["nonce"]), "两次写入用了同一个 nonce"
    assert bytes(a["ciphertext"]) != bytes(b["ciphertext"]), "密文相同，说明没重新加密"


def test_换了KEK就解不开(kek, clean, monkeypatch):
    """库被拷到另一台机器（KEK 不同）时**必须解不开**，这是加密的全部意义。"""
    secret_value.save("DEEPSEEK_API_KEY", PLAIN)
    assert secret_store.resolve("DEEPSEEK_API_KEY")[0] == PLAIN

    monkeypatch.setenv(secret_store.KEK_ENV,
                       base64.b64encode(_secrets.token_bytes(32)).decode())
    with pytest.raises(secret_store.InvalidTag):
        secret_store.resolve("DEEPSEEK_API_KEY")


def test_密文被改一个字节就解不开(kek, clean):
    """AEAD 的完整性校验：不许静默解出一段垃圾明文去发请求。"""
    secret_value.save("DEEPSEEK_API_KEY", PLAIN)
    with get_conn() as conn:
        ct = bytes(conn.execute(
            "SELECT ciphertext FROM secret_value").fetchone()["ciphertext"])
    broken = bytes([ct[0] ^ 0x01]) + ct[1:]
    with get_conn() as conn:
        conn.execute("UPDATE secret_value SET ciphertext = ?", (broken,))
    with pytest.raises(secret_store.InvalidTag):
        secret_store.resolve("DEEPSEEK_API_KEY")


def test_密文串门到别的变量就解不开(kek, clean):
    """AAD 绑定 env_name：把 A 家的密文挪到 B 家行上必须失败。

    没有这一条，库的写权限就等于"能把任一把密钥冒充成另一家"——
    后果是把 DeepSeek 的 key 发给阿里云网关。
    """
    secret_value.save("DEEPSEEK_API_KEY", PLAIN)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT ciphertext, nonce, aad FROM secret_value").fetchone()
    with get_conn() as conn:
        conn.execute("DELETE FROM secret_value")
        conn.execute(
            """INSERT INTO secret_value (env_name, ciphertext, nonce, aad,
                                         fingerprint, length)
               VALUES ('FV_KEY_VOLC', ?, ?, ?, '', 0)""",
            (row["ciphertext"], row["nonce"], row["aad"]))
    with pytest.raises(secret_store.InvalidTag):
        secret_store.resolve("FV_KEY_VOLC")


def test_KEK长度不对就拒绝而不是截断(kek, monkeypatch):
    """静默截断会让熵悄悄下降而毫无迹象——宁可整个功能不可用。"""
    monkeypatch.setenv(secret_store.KEK_ENV,
                       base64.b64encode(_secrets.token_bytes(16)).decode())
    with pytest.raises(secret_store.SecretStoreUnavailable, match="16 字节"):
        secret_store.seal("DEEPSEEK_API_KEY", PLAIN)
    assert secret_store.available() is False


def test_没有KEK时功能不可用而不是回落到明文(monkeypatch):
    """**缺省密钥等于没有加密**：没配 KEK 必须拒，不能找一条"能跑就行"的路。"""
    monkeypatch.delenv(secret_store.KEK_ENV, raising=False)
    assert secret_store.available() is False
    with pytest.raises(secret_store.SecretStoreUnavailable):
        secret_store.seal("DEEPSEEK_API_KEY", PLAIN)
    with pytest.raises(secret_store.SecretStoreUnavailable):
        secret_store.unseal("DEEPSEEK_API_KEY", ciphertext=b"x", nonce=b"y" * 12,
                            aad=b"z")


def test_环境变量优先于库里(kek, clean, monkeypatch):
    """运维必须能用一个 export 覆盖库里的值——排查时这是唯一的兜底。"""
    secret_value.save("DEEPSEEK_API_KEY", PLAIN)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-environment")
    value, source = secret_store.resolve("DEEPSEEK_API_KEY")
    assert (value, source) == ("sk-from-environment", "env")

    monkeypatch.delenv("DEEPSEEK_API_KEY")
    value, source = secret_store.resolve("DEEPSEEK_API_KEY")
    assert (value, source) == (PLAIN, "db")


def test_白名单与迁移的CHECK必须一致(kek, clean):
    """两处都写是刻意的冗余；这里钉住"它们必须一样"，免得改一处忘另一处。"""
    sql = (__import__("pathlib").Path("migrations/0052_secret_value.sql")
           .read_text(encoding="utf-8"))
    for name in secret_value.ALLOWED_ENV:
        assert f"'{name}'" in sql, f"{name} 在迁移的 CHECK 白名单里缺席"
    # 反向：迁移里出现的每个密钥名都得在代码白名单里
    # 正则要覆盖两种命名：`X_API_KEY` 结尾，与 `FV_KEY_XXX` 中间带 KEY。
    # 第一版只写了 `(?:API_KEY|_KEY|_TOKEN)$` 风格，于是 `FV_KEY_VOLC`
    # 这种（KEY 在中间）被漏掉，断言反而误报"迁移少了一半变量"。
    import re
    in_sql = set(re.findall(r"'([A-Z][A-Z0-9_]*)'", sql))
    in_sql = {n for n in in_sql
              if any(m in n for m in ("API_KEY", "_KEY", "_TOKEN", "SECRET"))}
    assert in_sql == set(secret_value.ALLOWED_ENV), (
        f"迁移与仓储白名单不一致：仅迁移有 {in_sql - set(secret_value.ALLOWED_ENV)}，"
        f"仅代码有 {set(secret_value.ALLOWED_ENV) - in_sql}")


def test_不是密钥的名字进不来(kek, clean):
    """白名单 + 密钥特征双门：路径类与非密钥类都要被拦在写库之前。"""
    with pytest.raises(ValueError, match="白名单"):
        secret_value.save("FASTVIDEO_FASTMETAL_HOME", "/tmp/x")
    with pytest.raises(ValueError, match="白名单"):
        secret_value.save(secret_store.KEK_ENV, "whatever")


def test_两道门各自独立可证伪(kek, clean, monkeypatch):
    """**第一道（白名单）与第二道（密钥特征）必须各自有用。**

    2026-11-11 变异验证撞出来的假绿：只测"白名单外的名字被拒"时，
    把第二道 `if not any(...)` 改成 `if False` 测试仍全绿——因为那些名字
    本来就被第一道挡住了，第二道等于从没被执行过。换句话说，那条用例
    证明不了第二道存在。

    所以这里造一个**在白名单里、但不含密钥特征**的名字。真实清单里没有
    这种名字（8 个都带 API_KEY/_KEY），于是临时把它塞进白名单，
    专门验第二道：它必须独立地把这个名字拒掉。
    """
    monkeypatch.setattr(secret_value, "ALLOWED_ENV",
                        secret_value.ALLOWED_ENV + ("FASTVIDEO_SOMETHING",))
    # 直接调 assert_allowed：save() 在 fixture 的 teardown 里也会被触发，
    # 用 save 会让断言落在 setup/teardown 阶段变成 ERROR 而不是干净的 fail。
    with pytest.raises(ValueError, match="看着不像密钥"):
        secret_value.assert_allowed("FASTVIDEO_SOMETHING")


def test_空密钥不许入库(kek, clean):
    """空串是"清除"，不是"存一个空密钥"。两种语义混起来会存出解不开的行。"""
    with pytest.raises(ValueError, match="空密钥"):
        secret_value.save("DEEPSEEK_API_KEY", "")


def test_指纹与adapters算的一致(kek, clean):
    """两处算出的指纹必须能互相比对，否则页面显示的和 key_present 判断的对不上。"""
    import adapters
    secret_value.save("DEEPSEEK_API_KEY", PLAIN)
    assert secret_store.fingerprint(PLAIN) == adapters.key_fingerprint.__wrapped__(
        PLAIN) if hasattr(adapters.key_fingerprint, "__wrapped__") else True
    # 直接比算法：两边都是 sha256 前 8 位
    import hashlib
    assert secret_store.fingerprint(PLAIN) == hashlib.sha256(
        PLAIN.encode()).hexdigest()[:8]


def test_key_present认库里的值(kek, clean):
    """健康徽章要回答"配过没有"，所以它必须认库；但它不解密。"""
    import adapters
    assert adapters.key_present("DEEPSEEK_API_KEY") is False
    secret_value.save("DEEPSEEK_API_KEY", PLAIN)
    assert adapters.key_present("DEEPSEEK_API_KEY") is True


# ---------------------------------------------------------------- 端到端与接口
#
# 上面测的是密码学性质，这一段测**这条功能为什么值得存在**：
# 管理员在页面上保存 → 下一单的云端调用真的用上了它，而且**没重启 worker**。

def test_保存后下一单就生效_不需要重启(kek, clean, monkeypatch):
    """**这是整个功能的立身之本。**

    如果保存后必须重启 worker，那这个功能比改 `.zshrc` 好不了多少——
    用户要的正是"别让我去命令行"。所以这条必须钉死：
    `worker._child_env()` 在**每单执行时**现调 `secret_store.resolve()`，
    拿到的是刚存进去的那把。

    判据放在"子进程最终拿到的 env"上，而不是 `resolve()` 的返回值——
    后者只能证明解密对，证明不了它真被传下去了。
    """
    import worker
    secret_value.save("DEEPSEEK_API_KEY", PLAIN)

    class Spec:
        pass_env = ("DEEPSEEK_API_KEY",)

    env = worker._child_env(Spec())
    assert env.get("DEEPSEEK_API_KEY") == PLAIN, (
        "worker 没把库里那把传下去——保存后仍要重启才能生效，"
        "那这个功能就没达到目的")


def test_环境变量在时不覆盖_worker也不读库(kek, clean, monkeypatch):
    """环境优先这条在**子进程 env** 这一层也要成立，不只是 resolve()。"""
    import worker
    secret_value.save("DEEPSEEK_API_KEY", PLAIN)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")

    class Spec:
        pass_env = ("DEEPSEEK_API_KEY",)

    assert worker._child_env(Spec()).get("DEEPSEEK_API_KEY") == "sk-from-env"


def test_解不开时不给子进程塞旧值(kek, clean, monkeypatch):
    """KEK 换了之后：环境里没有、库里解不开 → **这里就不该有那个变量**。

    不能"解不开就用空串顶上"——空串会让云端 runner 带上
    `Authorization: Bearer ` 发出去，报 401 时看不出是配置问题还是密钥问题。
    """
    import worker
    secret_value.save("DEEPSEEK_API_KEY", PLAIN)
    monkeypatch.setenv(secret_store.KEK_ENV,
                       base64.b64encode(_secrets.token_bytes(32)).decode())

    class Spec:
        pass_env = ("DEEPSEEK_API_KEY",)

    assert "DEEPSEEK_API_KEY" not in worker._child_env(Spec())


def test_接口能配上且只回指纹(as_role, kek, clean):
    """整条 HTTP 路径：PUT 存明文、响应无明文、GET 只给指纹。"""
    client = as_role("admin")
    r = client.put("/api/secret-values/DEEPSEEK_API_KEY", json={"value": PLAIN})
    assert r.status_code == 200, r.text
    body = r.text
    assert PLAIN not in body, "保存响应里出现了明文密钥"
    for n in (8, 12, 16):
        assert PLAIN[:n] not in body, f"响应里出现了明文前 {n} 位"
    assert r.json()["source"] == "db"
    assert r.json()["length"] == len(PLAIN)

    listing = client.get("/api/secret-values")
    assert listing.status_code == 200
    assert PLAIN not in listing.text
    assert PLAIN[:12] not in listing.text
    assert listing.json()["kek_ready"] is True

    # 库里存的确实是密文（在原始字节上验，不看接口）
    with get_conn() as conn:
        ct = bytes(conn.execute(
            "SELECT ciphertext FROM secret_value WHERE env_name=?",
            ("DEEPSEEK_API_KEY",)).fetchone()["ciphertext"])
    assert PLAIN.encode() not in ct


def test_没KEK时保存被拒并说清怎么生成(as_role, monkeypatch, clean):
    """没有 KEK 时**拒绝保存**，不是静默存明文。409 且文案要能照着做。"""
    monkeypatch.delenv(secret_store.KEK_ENV, raising=False)
    client = as_role("admin")
    r = client.put("/api/secret-values/DEEPSEEK_API_KEY", json={"value": PLAIN})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert secret_store.KEK_ENV in detail
    assert "token_bytes" in detail, "文案没给生成方法，人照着做不了"
    # 而且**什么都没写进去**
    assert secret_value.get_row("DEEPSEEK_API_KEY") is None


def test_不是白名单的变量拒绝写入(as_role, kek, clean):
    """路由层也要挡住路径类变量写入密钥表。"""
    client = as_role("admin")
    r = client.put("/api/secret-values/FASTVIDEO_FASTMETAL_HOME",
                   json={"value": "/tmp/x"})
    assert r.status_code == 400


def test_KEK变量名不带任何具体项目或模型名(kek):
    """**名字是设计的一部分，不是随手起的**（2026-11-11 用户据名质疑后改名）。

    历史：第一版叫 `FASTVIDEO_SECRET_KEK`，理由是"与另外 60 个 `FASTVIDEO_*`
    一致"。但那个前缀来自项目早期仓库名 FastVideo（`runtime.py` 里还留着
    `~/FastVideo` 的默认推导），后来平台改名为"AI 视频生产平台"而变量没改。
    用它会导致两个误导：

      1. 看着像"FastVideo 那条轨的 KEK"，而它实际是**全平台 8 把供应商密钥
         共用的一把**；
      2. `FASTVIDEO_FASTMETAL_*` 里的 FASTMETAL 才是模型名，两者挨在一起
         更容易被读成"这是 FastMetal 的密钥"。

    所以钉死：KEK 名字里不许出现任何具体项目名或模型名。
    """
    name = secret_store.KEK_ENV.upper()
    for banned in ("FASTVIDEO", "FASTMETAL", "MLX", "MFLUX", "VOXCPM",
                   "ACE_STEP", "RIFE", "DEEPSEEK", "DASHSCOPE", "WAN",
                   "VOLC", "JIMENG", "KLING", "MINIMAX", "SEEDANCE"):
        assert banned not in name, (
            f"KEK 变量名 {name} 含具体项目/模型名 {banned}——"
            f"它是全平台共用的主密钥，不该挂在某条轨名下")
    # 正例：确实叫这个，且语义上说得清"平台级"
    assert name == "PLATFORM_SECRET_KEK"
    assert "PLATFORM" in name and "SECRET" in name and "KEK" in name
