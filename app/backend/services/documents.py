import gitrepo
import hashlib

from config import DOC_MAX_BYTES, LOCK_MINUTES
from datetime import datetime
from paths import describe as describe_path
from repositories.documents import documents as docs
from workspace import atomic_write, path_of, plan_rel_path, read as read_file, unapplied_dir

from .errors import Conflict, DocTooLarge, GitSaveFailed, LockHeld


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
    """正文一律以换行收尾——**空正文除外**。

    不带结尾换行的文件，diff 末尾会挂一句 `\\ No newline at end of file`，
    版本面板里那行看着像稿子的一部分；更麻烦的是下一次在末尾追加一句时，
    diff 会把上一行也标成改动（其实只是补了个换行）。
    规范化放在写盘与算令牌之前，两边用的是同一份字节，令牌才不会自相矛盾。

    空稿不补：刚建好的「第一章」本来就是一个字都没写，硬塞一个换行会让
    界面上的字数显示成 1，读起来像"平台替我写了一个字"。空就是空。
    """
    if not content:
        return ""
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
                 user_id: int, minutes: int = LOCK_MINUTES,
                 commit_message: str | None = None) -> dict:
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
        commit = gitrepo.commit_file(
            doc["rel_path"],
            commit_message or f"保存 {doc['kind']}：{doc['title']}", actor)
    except gitrepo.GitError as exc:
        # Git 提交失败。规格 02 §6.1 给了两条路：回滚文件，或保留未应用的结果。
        # 这里两条一起做——先把正文退回刚才读到的那一份（工作区和库重新对齐），
        # 再把这次没提上去的内容另存一份到工作区外面（人不用重敲）。
        atomic_write(doc["rel_path"], current)
        saved = preserve_unapplied(doc, content)
        # 整句过一道 describe：这句要原样进 HTTP 500 的 detail，
        # 而 git 的 stderr 常把仓库绝对路径写进去（"… is outside repository"），
        # 另存位置也是绝对路径。家目录和安装路径不该跟着一条错误消息出门，
        # 换成 `data/unapplied/…` 这种写法反而更好定位。
        raise GitSaveFailed(describe_path(
            f"Git 提交失败，正文已退回上一版；你这次写的内容另存在 {saved}，"
            f"不用重敲。git 说：{exc}")) from exc

    # commit 为 None 表示"内容和上一版一模一样"：不写库、不推 updated_at——
    # 那一列的语义是"内容动过"，点一下保存什么都没改却把时间推走，等于让界面说谎。
    # 直接把现状回给前端，前端那行"改动"就不会跳。
    row = docs.save(doc["id"], commit) if commit else docs.get(doc["id"])
    docs.renew_on_save(doc["id"], user_id, minutes)
    return {"row": row, "version": version_token(content), "commit": commit,
            "changed": commit is not None}




def preserve_unapplied(doc, content: str) -> str:
    """把没提交上去的正文原样留一份，返回那个路径。

    这里返回绝对路径是给调用方的——`save_content` 把它拼进错误消息时会先过一道
    `describe_path`，人看到的是 `data/unapplied/…`，不是部署机的家门。
    """
    target = unapplied_dir() / (
        f"doc{doc['id']}-{datetime.now():%Y%m%d-%H%M%S}.md")
    target.write_text(content, encoding="utf-8")
    return str(target)
