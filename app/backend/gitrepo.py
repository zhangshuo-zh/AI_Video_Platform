"""文档正文的 Git 层：把工作区当成一个仓库来读写历史。

三条硬规矩，都是规格 02 §7.3／§14.1 要求的：
1. **参数用列表传给 git，绝不拼 shell 字符串、绝不用 `shell=True`**。
   标题里带 `; rm -rf` 这种输入在拼接式实现里是会真执行的。
2. **提交身份每次现给**（`GIT_AUTHOR_*` / `GIT_COMMITTER_*` 环境变量），
   不写用户的全局 `~/.gitconfig`，也不改机器上的任何 git 配置。
   历史上因此能看到"是哪个人改的"，而不是"平台改的"。
3. **只碰自己那一条路径**。`git commit -- <path>` 是路径级提交：
   仓库里如果躺着别人手工 stage 的东西，不会被我们这一次保存一起提上去。

一次性的仓库设置只在 `ensure_repo()` 里做：没有 `.git` 就 `git init`。
不去 `git add .`——工作区指向已有旧稿目录时，那是替别人做决定。
"""
import os
import re
import subprocess
from pathlib import Path

from workspace import root

# 提交历史一次最多取多少版。前端版本面板就列这么多；
# 真要翻更老的，去工作区里用 git 命令行看（平台不是 git 的前端）
HISTORY_LIMIT = 30

# 一次 git 调用的超时（秒）。局域网机器上磁盘慢、或者仓库被别的过程锁住时，
# 没有这一条的话请求会一直挂着，前端连个错都拿不到。
GIT_TIMEOUT = 20

_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class GitError(Exception):
    """git 命令失败。调用方转成 500，并把 git 说的那句话带回去（通常是原因本身，
    比如"没有配置用户身份"这类），不要只回一句"内部错误"。"""


def _env_for(author: str) -> dict:
    """给一次调用准备环境变量＝这次提交的署名。

    邮箱用 `<用户名>@platform.local`：不是真地址，只是 git 要求有这个格式。
    把真实邮箱写进稿件仓库没有意义——这个仓库不出本机，署名只用来分人。
    """
    env = os.environ.copy()
    name = (author or "平台").strip() or "平台"
    env["GIT_AUTHOR_NAME"] = name
    env["GIT_AUTHOR_EMAIL"] = f"{name}@platform.local"
    env["GIT_COMMITTER_NAME"] = name
    env["GIT_COMMITTER_EMAIL"] = f"{name}@platform.local"
    return env


def _run(args: list[str], author: str | None = None) -> str:
    """跑一条 git，返回 stdout。失败抛 GitError。

    `-c commit.gpgsign=false` 只作用于这一次调用：如果这台机器全局开了签名，
    平台提交会以"没有密钥环"失败，整条保存链路就废了。这里的署名不是人的身份
    （是平台替编辑记的账），签不签名都不构成信任凭证，所以关掉它是安全的一边。

    `-c core.quotePath=false` 是为了中文文件名：默认 git 会把非 ASCII 路径转成
    `"\\351\\252\\214..."` 这种八进制串，版本面板里的 diff 头就完全没法读。
    """
    try:
        proc = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "-c", "core.quotePath=false", *args],
            cwd=str(root()), env=_env_for(author or "平台"),
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise GitError("这台机器上没有 git 命令，正文历史无法保存（装好 git 再试）") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git 操作超过 {GIT_TIMEOUT} 秒没返回，工作区可能被别的进程占着") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise GitError(detail or f"git {' '.join(args)} 返回 {proc.returncode}")
    return proc.stdout


def is_repo() -> bool:
    return (root() / ".git").is_dir()


def ensure_repo() -> None:
    """仓库不存在就建一个。已存在时什么都不做（幂等，启动时敢反复调）。"""
    if is_repo():
        return
    root().mkdir(parents=True, exist_ok=True)
    # --initial-branch 在旧 git 上不认（<2.28），它只会打印警告并回落到默认分支名，
    # 所以按返回码判断这条参数能不能用，而不是按版本号猜。
    if subprocess.run(["git", "--version"], capture_output=True, text=True).returncode != 0:
        raise GitError("这台机器上没有 git 命令")
    try:
        _run(["init", "--initial-branch=main", "."], author="平台")
    except GitError:
        _run(["init", "."], author="平台")   # 老版本 git：用它的默认分支名


def sha_ok(candidate: str) -> bool:
    """校验前端传来的 commit 号。

    这一条不是洁癖：commit 号会进 git 的参数列表，`--help`、`-p` 这类字符串
    一旦当作 sha 传进去，命令行为就变了（轻则报错，重则多输出别人的内容）。
    """
    return bool(_SHA.match(candidate or ""))


def _changed(rel_path: str, author: str) -> bool:
    """这条路径有没有待提交的变化（含新增未跟踪文件）。"""
    return bool(_run(["status", "--porcelain", "--", rel_path], author).strip())


def commit_file(rel_path: str, message: str, author: str) -> str | None:
    """把这一条正文提交上去，返回新的 commit 号。

    内容没变时返回 None（而不是造一个空提交）：每次点保存都攒一个"什么都没改"的
    提交，版本列表会被垃圾填满，diff 也全是空——那一栏就没有参考价值了。
    调用方把 None 理解成"正文和上一版一致，版本不变"。
    """
    if not is_repo():
        ensure_repo()
    if not _changed(rel_path, author):
        return None
    _run(["add", "--", rel_path], author)
    # 先 add 再带路径提交：新文件不 add 的话路径级提交找不到它；
    # 提交时仍然限定这一条路径，别人 stage 的东西不会被顺带提上去。
    _run(["commit", "-m", message, "--", rel_path], author)
    return _run(["rev-parse", "HEAD"], author).strip()


def remove_file(rel_path: str, message: str, author: str) -> None:
    """删一份正文并在历史里记这一笔。

    文件本来就没被跟踪（比如提交失败留下的孤儿）时，`git rm` 会报错——
    这时直接不吭声：磁盘上的文件由调用方删，历史里本来也没有它。
    """
    if not is_repo():
        return
    try:
        _run(["rm", "-f", "--", rel_path], author)
    except GitError:
        return
    _run(["commit", "-m", message, "--", rel_path], author)


def history(rel_path: str, limit: int = HISTORY_LIMIT) -> list[dict]:
    """这一条正文的提交历史（新 → 旧）。

    `\x1f` 是分隔符：标题里不会出现，用空格／逗号切都会被 commit message 里的字符撞坏。
    """
    if not is_repo():
        return []
    try:
        out = _run(
            ["log", f"-n{limit}", "--date=format:%Y-%m-%d %H:%M:%S",
             "--format=%H\x1f%an\x1f%ad\x1f%s", "--", rel_path],
            "平台",
        )
    except GitError:
        # 路径还没提交过（新文档刚写完）时 git log 会报"unknown revision"——
        # 那不是错误，是"这一份还没有历史"，回空列表让界面显示"暂无版本"
        return []
    items = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        items.append({"commit": parts[0], "author": parts[1], "at": parts[2], "subject": parts[3]})
    return items


def diff(commit: str, rel_path: str) -> str:
    """某一次提交在这条路径上造成的差异（纯文本 unified diff）。

    用 `git show` 而不是 `git diff <sha>^ <sha>`：第一次提交没有父提交，
    后者会当场报错，而"看第一版写了什么"恰恰是最常按的一个。
    """
    if not sha_ok(commit):
        raise GitError("commit 号格式不对")
    return _run(["show", "--no-color", "--format=commit %H (%an, %ad)%n",
                 "--date=format:%Y-%m-%d %H:%M:%S", commit, "--", rel_path], "平台")


def path_has_history(rel_path: str) -> bool:
    """这条路径在历史里有没有过东西。

    建文档定路径时要用：删掉一篇《取火 第一章》之后再建同名的一篇，
    DB 里那条行没了、路径也就"空"了，新文档会占回同一个文件——
    于是它的版本面板里混着上一篇的"新建／删除"提交，读的人会以为
    自己漏看了什么。宁可排号成 `取火-第一章-2.md`：新稿子就该有一条干净的历史。
    """
    if not is_repo():
        return False
    try:
        return bool(_run(["log", "-1", "--format=%H", "--", rel_path], "平台").strip())
    except GitError:
        return False      # 仓库坏了不该挡住建文档；路径撞车由 rel_path 的 UNIQUE 兜底


def status_line() -> str:
    """给健康检查用的一句话：仓库在不在、HEAD 是什么。只读，不改任何东西。"""
    if not is_repo():
        return "未初始化"
    try:
        return _run(["rev-parse", "--short", "HEAD"], "平台").strip()
    except GitError:
        # 新建的仓库还没有任何提交，`rev-parse HEAD` 必然报错。
        # 这不是坏，是"空"——报成异常会让人以为工作区出了问题。
        return "空仓库（还没有提交）"


# 只用来给日志/健康检查拼一句人话，不做路径判断（判断一律走 workspace.py）
def workspace_path() -> Path:
    return root()
