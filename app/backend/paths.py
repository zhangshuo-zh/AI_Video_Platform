"""库里的相对路径 → 根目录内的绝对路径，一条规则只写一遍。

为什么要单独一个文件：同一句"拼完再确认没跑出去"原本散在四处（删项目清图、
删资产清图、删单张图、发图接口），每一处写法还不一样。边界校验这种东西，
复制四遍就等于漏四次——`str(target).startswith(str(root))` 这种写法会把
`data/files` 匹配给 `data/files-evil`，前缀看着一样、其实已经在外面了。
`workspace.py` 与 `jobfiles.py` 早就各自用上了 `root + os.sep`，
这里把同一口径收过来，`FILES_DIR` 这一侧不再是唯一没改的那个。
那两处仍走自己的异常（`PathError` / `JobPathError`，报错文案要带给不同的人看），
规则相同、出口不同，所以不强行合并。

四条规则，缺一不可：
1. 反斜杠先换成 `/`。POSIX 上它不是分隔符，但库里混进 Windows 写法
   （`..\\x`）时，换完再按段检查就只剩一条判断规则。
2. 绝对路径与 `..` 段直接拒。上传接口生成的名字是 `<资产id>/<uuid>.<ext>`，
   出现这两种写法只有脏数据一个来源。
3. 解析（`resolve()`）之后再比前缀。不解析的话 `a/../b` 这种写法能在
   字符串层面骗过检查。
4. 比较用 `root + os.sep`，并且 `target == root` 也算越界——
   我们要的是"根目录里的一个文件"，不是根目录本身。
"""
import os
from pathlib import Path

import config


def within(root: Path, target: Path) -> bool:
    """target（解析后）是否严格落在 root 内部。root 自己不算内部。

    给已经拿到绝对路径的调用方用——`runners.py` 的条件图允许外面传绝对路径，
    那种写法没有 `rel` 可解析，只需要最后那一句边界判定。
    这里再解析一次是故意的：调用方传进来的可能是还没跟过软链接的路径，
    `data/files/x.png -> /etc/passwd` 只有在解析之后才看得出它在外面。
    """
    base = str(Path(root).resolve())
    real = str(Path(target).resolve())
    return real != base and real.startswith(base + os.sep)


def resolve_within(root: Path, rel: str) -> Path | None:
    """rel 落在 root 内 → 解析后的绝对路径；写法不合规或越界 → None。

    返回 None 而不是抛异常，因为绝大多数调用方是"清理文件"：库里躺着一条
    越界路径属于脏数据，跳过它正好（它本来就不在删除范围内），
    不该因为一条坏记录把删项目／删资产整个动作卡住。
    调用方是发图接口时，None 直接回 404——不把"路径长什么样"告诉外面。
    """
    rel = (rel or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return None
    base = Path(root).resolve()
    target = (base / rel).resolve()
    return target if within(base, target) else None


def listdir_empty(path: Path) -> bool:
    """目录存在且是空的。删完文件顺手收掉空目录用，读不到就当不空。

    为什么要吞掉异常：这条清理是"顺手"的，目录里剩下点什么都不是错误，
    不能因为它把主动作（删项目／删资产）打断。
    """
    try:
        return path.is_dir() and not any(path.iterdir())
    except OSError:
        return False


def describe(path: Path | str) -> str:
    """绝对路径 → 带标签的写法：落在配置目录里的，换成 `data/…` 这种短名。

    为什么要绕这一下：这类字符串会进 HTTP 响应、进作业列表接口、进 CSV 导出，
    也就是会离开这台机器的视野。部署机的 `/Users/<谁>/…/backend/data/unapplied/x.md`
    对使用者没有增量信息（他就在这台机器上工作），却白送一份家目录与安装路径；
    换成 `data/unapplied/x.md` 一样能定位，短得多也不出家门。

    两点交代清楚：
    - `data/…` 是**标签**，按文档里的默认布局取名。部署时改过 `FASTVIDEO_*`
      环境变量，真实位置以配置为准——标签的作用是人话，不是 `cd` 得进去。
    - 按长度倒序替换：`data` 是另外几个根的父目录，先替父目录会把子路径一起带跑。
    - 读的是 `config` 的模块属性而不是 import 常量，测试里换目录才能立刻生效。
    """
    text = str(path)
    roots = [(Path(root), label) for root, label in (
        (config.FILES_DIR, "data/files"),
        (config.JOBS_DIR, "data/jobs"),
        (config.WORKSPACE_DIR, "data/workspace"),
        (Path(config.WORKSPACE_DIR).parent / "unapplied", "data/unapplied"),
        (config.DB_PATH.parent, "data"),
    )]
    # 每个根目录备两种写法：环境变量给的是没解析过的路径时（macOS 上
    # `/var/… -> /private/var/…` 就会这样），传进来的字符串与被替掉的那一段
    # 必须长得不一样才换得掉。只备 resolve 后的那一种，这条替换会静默不生效，
    # 绝对路径照样出家门——静默不生效正是这类收口最坏的失效方式。
    pairs = set()
    for root, label in roots:
        for form in (str(root), str(root.resolve())):
            pairs.add((form, label))
    for root, label in sorted(pairs, key=lambda p: -len(p[0])):
        text = text.replace(root + os.sep, label + "/").replace(root, label)
    return text
