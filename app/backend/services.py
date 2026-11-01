"""跨模块复用的业务动作。放在路由之外的唯一理由：有第二个调用方。

三条：删项目时把定妆图文件也清掉；标签归一化；文档正文的"读—写—提交"这条链。
项目中心（删单个项目）和系统设置（删账号时连带删他名下的项目）都要走第一条；
项目标签与资产标签都要走第二条；第三条更要放在这里——W11 的 AI 改写跑完之后
要落盘，那时候的调用方是 worker 而不是路由，实现必须已经在路由之外。
写在某个 router 里让另一个 router 去 import 是反的（规格 02 §4.1）。
"""
import hashlib
from datetime import datetime
from pathlib import Path

import gitrepo
from config import DOC_MAX_BYTES, FILES_DIR, LOCK_MINUTES
from repositories.documents import documents as docs
from repositories.projects import projects as project_repo
from workspace import PathError
from workspace import atomic_write, path_of, plan_rel_path, read as read_file
from workspace import unapplied_dir

MAX_TAGS = 10
MAX_TAG_LEN = 20


class Conflict(Exception):
    """正文已经不是他看的那一版了。路由层转成 409，绝不静默覆盖。"""

    def __init__(self, detail: str, current_version: str = ""):
        super().__init__(detail)
        self.current_version = current_version


class LockHeld(Exception):
    """别人正拿着编辑锁。路由层转成 423（和"版本冲突"分开：
    一个是"你手上的太旧"，一个是"现在轮不到你写"，人要做的事不一样）。"""

    def __init__(self, detail: str, holder_name: str = "", expires_at: str = ""):
        super().__init__(detail)
        self.holder_name = holder_name
        self.expires_at = expires_at


class DocTooLarge(Exception):
    """正文超了单份上限（config.DOC_MAX_BYTES）。路由回 413。"""


class GitSaveFailed(Exception):
    """Git 提交失败、文件已经退回上一版。路由回 500，把 git 的原话带回去。"""


def clean_tags(raw: list[str]) -> list[str]:
    """去空白、去重、限长、限量。超出的直接截掉而不是报错——标签是辅助信息，
    不该挡住主流程。项目与资产共用这一条规则，两边口径不能分叉。"""
    seen: list[str] = []
    for item in raw:
        tag = item.strip()[:MAX_TAG_LEN]
        if tag and tag not in seen:
            seen.append(tag)
    return seen[:MAX_TAGS]


def delete_project_cascade(project_id: int, actor: str = "平台") -> None:
    """删项目并清掉它名下的定妆图文件与稿件正文。

    先记下文件路径再删行：删完就查不到路径了，磁盘上的图会变成孤儿。
    路径校验是为了防止库里的脏数据把删除范围带出 FILE_DIR 之外。

    全局资产（project_id 为 NULL）天然不在清理范围内：`asset_images` 按
    `project_id = ?` 取，NULL 比不中。这正是"删项目不能删掉全局资产"
    （指令 §5.3）在文件这一侧的落点。

    稿件走 `git rm` 而不是 `unlink`：删项目是既有流程里最重的动作，
    正文如果直接从磁盘上抹掉，出了事只能去备份里捞。走一次提交，
    工作区干净了、历史里还在，谁想把那一稿找回来都还有路。
    """
    images = project_repo.asset_images(project_id)
    doc_paths = docs.paths_by_project(project_id)
    project_repo.delete(project_id)

    # 上传的图落在 FILES_DIR/<资产id>/ 下（content.py 上传接口），文件清完
    # 把空了的资产目录也收掉；目录里还有别的东西就留着，不越界。
    files_root = str(FILES_DIR.resolve())
    asset_dirs: set[Path] = set()
    for rel in images:
        target = (FILES_DIR / rel).resolve()
        if str(target).startswith(files_root) and target.is_file():
            target.unlink()
            asset_dirs.add(target.parent)
    for d in asset_dirs:
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()

    for rel in doc_paths:
        try:
            gitrepo.remove_file(rel, f"删除项目 {project_id} 的稿件：{rel}", actor)
        except gitrepo.GitError:
            pass          # 历史记不上不该挡住删项目；下面的 unlink 会把文件本身收掉
        try:
            target = path_of(rel)
            if target.is_file():
                target.unlink()
        except PathError:
            pass          # 库里躺着一条越界路径是脏数据，正好不在我们的删除范围里
    # 项目目录空了顺手收掉，工作区不会攒出一堆 p17/ 这种空壳
    for rel in doc_paths:
        try:
            folder = path_of(rel).parent
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        except PathError:
            pass


# ---------- 文档正文这条链（W05） ----------

def version_token(content: str) -> str:
    """正文 → 版本令牌。

    为什么用内容哈希而不是 git commit 号当 `expected_version`：
    commit 号只在"改动经过平台"时才会前进。这套设计里正文本来就是可以手工编辑的
    （设计稿 §3.3：小说/剧本留在文件系统并走 git），有人在编辑器外面改过一个字，
    commit 号还是旧的，拿它比就检不出冲突——而"第二个编辑者不会静默覆盖"
    （W05 验收项）要的恰恰是这种检出。哈希是当场算的，谁改的都跑不掉。
    前缀 `sha256:` 是为了哪天换成 git commit 或 file mtime 时，
    旧令牌一眼能看出来是哪一种，不用猜。
    """
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def read_content(doc) -> dict:
    """读一份正文。文件不在（被人手工删了／工作区换过目录）时抛 FileNotFoundError。"""
    content = read_file(doc["rel_path"])
    return {"content": content, "version": version_token(content)}


def _tend(content: str) -> str:
    """正文一律以换行收尾。

    不带结尾换行的文件，diff 末尾会挂一句 `\\ No newline at end of file`，
    版本面板里那行看着像稿子的一部分；更麻烦的是下一次在末尾追加一句时，
    diff 会把上一行也标成改动（其实只是补了个换行）。
    规范化放在写盘与算令牌之前，两边用的是同一份字节，令牌才不会自相矛盾。
    """
    return content if content.endswith("\n") else content + "\n"


def create_document(project_id: int, episode_id: int | None, kind: str,
                    title: str, content: str, actor: str) -> dict:
    """建一份文档：定路径 → 落盘 → 提交 → 记元数据。

    顺序是"文件在前、库在后"：路径与提交都成了才有行，
    中途任何一步失败都不会留下"库里有、磁盘上没有"的那份假文档
    （点进去就是 404，比建失败难查得多）。
    """
    content = _tend(content)
    if len(content.encode("utf-8")) > DOC_MAX_BYTES:
        raise DocTooLarge(f"正文超过 {DOC_MAX_BYTES // 1024} KB，先分文档再写")
    # 路径"被占"有两种：库里还有这行，或者这行删了但 Git 历史里还记着这个文件。
    # 后者也要算占——否则同名重建会接上上一份稿的提交历史（见 gitrepo.path_has_history）
    rel_path = plan_rel_path(project_id, kind, title,
                             lambda p: docs.path_taken(p) or gitrepo.path_has_history(p))
    atomic_write(rel_path, content)
    try:
        commit = gitrepo.commit_file(rel_path, f"新建 {kind}：{title}", actor)
    except gitrepo.GitError:
        # 提交不上就撤销整个动作：文件删掉、不建行。
        # 留下一个没有历史的文件，等于把"正文有、历史无"这种状态塞进工作区。
        path_of(rel_path).unlink(missing_ok=True)
        raise
    try:
        row = docs.create(project_id, episode_id, kind, title, rel_path, commit)
    except Exception:
        path_of(rel_path).unlink(missing_ok=True)
        raise
    return {"row": row, "content": content, "version": version_token(content),
            "commit": commit}


def save_content(doc, content: str, expected_version: str, actor: str,
                 user_id: int, minutes: int = LOCK_MINUTES) -> dict:
    """保存正文：冲突检测 → 锁检查 → 原子写 → 提交 → 更新元数据。

    检查的顺序按"代价从小到大"排：先比令牌（纯内存）、再看锁（一次查询）、
    然后才动磁盘。反过来的话，一个过期浏览器标签页点一下保存就能把别人的
    一次编辑从工作区里挤掉，而挤掉之前什么都没告诉他。
    """
    content = _tend(content)      # 先规范化再算字节数与令牌，两边才是同一份内容
    if len(content.encode("utf-8")) > DOC_MAX_BYTES:
        raise DocTooLarge(f"正文超过 {DOC_MAX_BYTES // 1024} KB，先分文档再写")

    # 拿磁盘上的现状算令牌，而不是拿库里的 git_commit：理由见 version_token
    current = read_file(doc["rel_path"])
    current_token = version_token(current)
    if expected_version != current_token:
        raise Conflict(
            "这份工作区里的正文已经变了（可能有人在你之后保存过，或者在编辑器外面改过）。"
            "先把最新的拉下来比对，别直接覆盖。", current_token)

    lock = docs.lock_of(doc["id"])
    if lock and lock["holder_user_id"] != user_id:
        raise LockHeld(
            f"{lock['holder_name']} 正在编辑这一份，锁到 "
            f"{lock['expires_at']} 自己松开；要接着写请等他保存或过期。",
            lock["holder_name"], lock["expires_at"])

    try:
        atomic_write(doc["rel_path"], content)
    except OSError:
        # 写盘失败（典型是磁盘满）：什么都没改，直接回错，不需要回滚
        raise
    try:
        commit = gitrepo.commit_file(doc["rel_path"], f"保存 {doc['kind']}：{doc['title']}", actor)
    except gitrepo.GitError as exc:
        # Git 提交失败。规格 02 §6.1 给了两条路：回滚文件，或保留未应用的结果。
        # 这里两条一起做——先把正文退回刚才读到的那一份（工作区和库重新对齐），
        # 再把这次没提上去的内容另存一份到工作区外面（人不用重敲）。
        atomic_write(doc["rel_path"], current)
        saved = preserve_unapplied(doc, content)
        raise GitSaveFailed(
            f"Git 提交失败，正文已退回上一版；你这次写的内容另存在 {saved}，"
            f"不用重敲。git 说：{exc}") from exc

    row = docs.save(doc["id"], commit if commit else doc["git_commit"])
    docs.renew_on_save(doc["id"], user_id, minutes)
    # commit 为 None 表示"内容和上一版一模一样"，版本指针不动（gitrepo.commit_file 里说了为什么）
    return {"row": row, "version": version_token(content), "commit": commit,
            "changed": commit is not None}


def preserve_unapplied(doc, content: str) -> str:
    """把没提交上去的正文原样留一份，返回那个路径（进错误消息给人看）。"""
    target = unapplied_dir() / (
        f"doc{doc['id']}-{datetime.now():%Y%m%d-%H%M%S}.md")
    target.write_text(content, encoding="utf-8")
    return str(target)


def ensure_workspace() -> str:
    """启动时把工作区准备成可用状态，返回一句状态给健康检查显示。

    幂等：仓库已经在了就什么都不做。顺带清一次过期的锁行
    （`drop_expired` 只影响"几个人在编辑"这种计数，正确性不靠它）。
    """
    docs.drop_expired()
    gitrepo.ensure_repo()
    return gitrepo.status_line()

