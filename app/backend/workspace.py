"""文档正文的文件层：工作区路径校验 + 原子写。

这一层只干两件事，都不碰数据库、也不碰 Git（那是 gitrepo.py 的事）：
1. **把"相对路径"这个词当真**。规格 02 §6.1 要的是"规范化之后仍在允许根目录内"，
   所以这里不接受绝对路径、不接受 `..`、不接受指向根目录外的软链，
   也不接受把 `.git` 当普通目录写进去。任何一层失守，界面上都只是"读不到文件"，
   失守的是别人的数据。
2. **写文件要么完整要么没发生**。先写同目录的临时文件、fsync、再 `os.replace`。
   直接 `write_text` 的话，写一半崩了（磁盘满、进程被杀）留下的就是半篇正文，
   而 Git 会把这半篇当成新版本提交上去——损坏会被"历史"永久合法化。

一条设计取舍先记在这：路径由服务端生成（`plan_rel_path`），客户端永远只传文档 id。
所以路径校验防的不是"正常使用"，而是库里的脏数据／将来某个把路径当入参的接口。
"""
import os
import re
import uuid
from pathlib import Path

from config import WORKSPACE_DIR

# 正文统一 Markdown：Git diff 对纯文本最有用，二进制文件没法做差异比较，
# 也就没有"看这一版改了什麼"这件事（W05 验收项）。
DOC_SUFFIX = ".md"

# 一条相对路径最多几段／多长。防止有人拿超长路径去撞操作系统的文件名上限，
# 那种错误报出来是 Errno 63，人看不懂、日志也帮不上。
MAX_SEGMENTS = 6
MAX_TOTAL_LEN = 200


class PathError(Exception):
    """路径不合法或不安全。调用方一律转成 422，不回 500：
    这是"你给的东西不对"，不是"平台坏了"。"""


def root() -> Path:
    """工作区根目录（已建好、已解析软链）。"""
    root_dir = WORKSPACE_DIR
    root_dir.mkdir(parents=True, exist_ok=True)
    return Path(os.path.realpath(root_dir))


def unapplied_dir() -> Path:
    """Git 提交失败时，那份"没进历史"的正文放这里。

    故意放在工作区**外面**：仓库里多出一堆没提交的 .md，`git status` 就长期是脏的，
    下一次保存到底提上去没提上去，人没法从仓库状态判断。
    """
    target = WORKSPACE_DIR.parent / "unapplied"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _bad(reason: str) -> PathError:
    return PathError(f"文档路径不合法：{reason}")


def normalize(rel_path: str) -> str:
    """校验并返回规范化的相对路径（`a/b.md` 这种，不带前导斜杠、不含软链逃逸）。

    顺序是有讲究的：先做纯字符串层面的拒收（绝对路径、空段、`..`、隐藏文件、后缀），
    再落到文件系统上做真实路径比对。前者挡的是明显不对的输入，后者挡的是
    "字符串看着干净但某个中间目录是软链"这种情况。
    """
    raw = (rel_path or "").strip()
    if not raw:
        raise _bad("空路径")
    if os.path.isabs(raw) or raw.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:", raw):
        raise _bad("必须是工作区内的相对路径，不能是绝对路径")
    if "\\" in raw:
        raise _bad("路径分隔符只能是 /")
    if "\x00" in raw or any(ord(ch) < 32 for ch in raw):
        raise _bad("含不可见控制字符")

    segments = raw.split("/")
    if len(segments) > MAX_SEGMENTS:
        raise _bad(f"层级最多 {MAX_SEGMENTS} 层")
    if len(raw) > MAX_TOTAL_LEN:
        raise _bad(f"路径最长 {MAX_TOTAL_LEN} 字符")
    for seg in segments:
        if not seg or seg in (".", ".."):
            raise _bad("含空段或 . / .. 段")
        # 隐藏目录一律拒收。.git 是仓库本体，写进去等于改历史；
        # 别的点开头目录（.vscode、.ssh）没有理由出现在稿件工作区里。
        if seg.startswith("."):
            raise _bad("不能以 . 开头（隐藏目录／文件不收）")
    if not segments[-1].endswith(DOC_SUFFIX):
        raise _bad(f"正文必须是 {DOC_SUFFIX} 后缀")

    root_dir = root()
    target = (root_dir / "/".join(segments)).resolve()
    real = os.path.realpath(target)
    # startswith(root + sep) 而不是 startswith(root)：
    # /data/workspace 会把自己匹配给 /data/workspaceevil，看着一样、其实在外面
    if real != str(root_dir) and not real.startswith(str(root_dir) + os.sep):
        raise _bad("规范化之后跑到了工作区外面")
    if Path(real).is_symlink():
        raise _bad("正文不能是软链接")
    return "/".join(segments)


def path_of(rel_path: str) -> Path:
    """相对路径 → 绝对路径（校验通过的才行）。"""
    return root() / normalize(rel_path)


def exists(rel_path: str) -> bool:
    try:
        return path_of(rel_path).is_file()
    except PathError:
        return False


def read(rel_path: str) -> str:
    """读正文。文件不存在时抛 FileNotFoundError，由调用方转成 404。"""
    target = path_of(rel_path)
    if not target.is_file():
        raise FileNotFoundError(rel_path)
    # 稿件是 UTF-8 文本；读不出来就是有人往里面塞了二进制，宁可报错也不做替换解码——
    # 替换解码会把坏字节变成一堆能看不能信的字符，再保存一次就等于毁档。
    return target.read_text(encoding="utf-8")


def atomic_write(rel_path: str, content: str) -> None:
    """原子写：同目录临时文件 → fsync → os.replace。

    临时文件必须和目标是同一个目录（同一个文件系统），否则 `os.replace`
    退化成"跨设备拷贝"，就不再是原子的了。
    """
    target = path_of(rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        #  replace 成功后 tmp 已经没了；失败时把半截文件收掉，不留垃圾给 git status
        tmp.unlink(missing_ok=True)


_SLUG = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff-]+")


def slugify(title: str, fallback: str = "doc") -> str:
    """标题 → 文件名。保留中文、字母、数字、下划线和连字符，其余换成 `-`。

    为什么不是"标题直接当文件名"：标题里有 `/`、`:`、`#`、emoji 和空格，
    这些在命令行、URL 与不同文件系统上的行为各不相同；一次生成、终身拼路径的字符串
    必须只含最保守的字符集。文件名是给 Git 和 shell 用的，给人看的名字在 title 列。
    """
    slug = _SLUG.sub("-", (title or "").strip()).strip("-_ ")
    slug = re.sub(r"-{2,}", "-", slug)[:60].strip("-_")
    return slug or fallback


def plan_rel_path(project_id: int, kind: str, title: str, taken) -> str:
    """为新建文档生成一条相对路径。

    `taken` 是一个"这条路径被占了没有"的判断函数（仓储层查 document.rel_path）。
    重名不报错而是排号：`p3/novel/开场-2.md`。报错的话，两个人给不同章节起了
    同一个副标题就得先来一轮"你们改名字协调一下"，那是平台该干的事。
    """
    base = f"p{project_id}/{kind}/{slugify(title)}"
    candidate = base + DOC_SUFFIX
    n = 2
    while taken(candidate):
        candidate = f"{base}-{n}{DOC_SUFFIX}"
        n += 1
        if n > 50:
            raise PathError("同名文档太多，请给标题加一点区分")
    return candidate
