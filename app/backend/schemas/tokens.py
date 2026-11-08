import hashlib


# ---------- 弱版本令牌（与 `asset_version`／`profile_version` 同一套做法） ----------


def provider_slot_version(row) -> str:
    """一行 `provider_config` 的令牌：时间 + 会被改的那六列。

    为什么需要：`PUT /api/providers` 是**整批覆盖**（十行一起提交、一个事务写下去），
    所以两个人同时打开这张配置页、各自改一行、先后保存，后保存的那一次会把
    前一个人改的那一行**静默盖回旧值**——他看到的"已保存"是真的，只是他改的东西没了。
    这不是理论风险：这一页是"配好一次就不常动"的那种页面，人打开它时
    通常会花几分钟对着文档核对 provider 与密钥变量名。

    为什么不用 `updated_at` 本身：那一列精度只到秒（`datetime('now','localtime')`），
    两人同一秒内先后保存时它一模一样，闸就成了摆设。所以令牌压的是
    "时间 + 内容"：内容真动过哈希必变，一字没改地再保存一次仍然通过（没有东西被覆盖）。
    这套做法与 `models.asset_version` 一字不差，只是字段换成这一张表的六列。
    """
    keys = row.keys() if hasattr(row, "keys") else row

    def get(k):
        return (row[k] if k in keys else "") or ""

    return _digest(str(get("updated_at")), str(get("capability")), str(get("backend")),
                   str(get("provider")), str(get("model")), str(get("enabled")),
                   str(get("is_default")), str(get("key_env")), str(get("notes")))


# ---------- 弱版本令牌 ----------

def _digest(*parts: str) -> str:
    return hashlib.sha1("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()[:12]




def asset_version(row) -> str:
    """这条资产**此刻这一版**的令牌。界面带回来，服务端一比就知道有没有人插进去改过。

    为什么不用 `updated_at` 本身当令牌：那一列的精度是**秒**
    （`datetime('now','localtime')`），两个人在同一秒里先后保存，时间一模一样，
    闸就形同虚设——而"同时编辑同一条人物"恰恰是两个人手快时最容易发生的事。
    所以令牌压的是"时间＋会被改的那几列"：内容真的动过，哈希必变；
    内容一字没改地再保存一次，回 200 也是对的（没有东西被覆盖）。

    这不是乐观锁的完整实现（没有冲突后自动合并），它只保证一件事：
    **不会在人不知情的情况下把他的编辑盖掉**。
    """
    # 与上面 `provider_slot_version` 同一条口径：`sqlite3.Row` 有 `keys()`，
    # 普通 dict 没有——退到 dict 本身（`k in dict` 同样是成员判断）。
    # 原来这里两个分支写的是同一个表达式（`… else row.keys()`），
    # 退到 dict 时会因为没有 `keys()` 直接 AttributeError。
    keys = row.keys() if hasattr(row, "keys") else row
    def get(k):
        return (row[k] if k in keys else "") or ""
    return _digest(str(get("updated_at")), str(get("kind")), str(get("name")),
                   str(get("summary")), str(get("consistency_prompt")),
                   str(get("aliases")), str(get("ref_images")))




def profile_version(profile: dict) -> str:
    """人物档案的令牌：档案自己的时间打头，再加档案四段。

    **为什么不拿 `asset.updated_at` 一起压**（第一版是这么写的，测一次就露馅了）：
    资产创作那一页点一次「保存这份设定」是两次写——先 PUT 资产、再 PUT 档案，
    而 PUT 资产会把 `asset.updated_at` 推到下一秒。档案令牌要是含了这个时间，
    紧接着那次 PUT 档案就必然带着一个"过期"的令牌，**自己把自己判成冲突**。
    同一秒内两次真改还是能分开：内容进了哈希，改过的人令牌必变。
    """
    return _digest(str(profile.get("updated_at") or ""),
                   *(profile.get(k) or "" for k in
                     ("biography", "personality", "growth_arc", "text_prompt")))
