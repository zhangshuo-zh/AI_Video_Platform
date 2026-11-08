import gitrepo
import jobfiles

from config import FILES_DIR
from pathlib import Path
from paths import listdir_empty, resolve_within
from repositories.content import episodes as episode_repo, variants
from repositories.documents import documents as docs
from repositories.projects import projects as project_repo
from workspace import PathError, path_of

from .errors import MAX_TAGS, MAX_TAG_LEN


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
    # 两份图清单都要收：asset.ref_images 是基础参考图，形态的图存在 asset_variant
    # 那一列里（0033）。只看前者删项目，形态图会变成没人认领的孤儿文件。
    images = project_repo.asset_images(project_id) + variants.images_by_project(project_id)
    doc_paths = docs.paths_by_project(project_id)
    # 作业行与它的日志／产物目录**不跟着删**（0039）：`job.project_id` 是
    # ON DELETE SET NULL，行留在库里，文件留在 jobs/<id>/ 下。
    # 钱花了账要留着——《系统使用说明书》§2.5 与 §3.2 承诺的就是这一条。
    project_repo.delete(project_id)

    # 上传的图落在 FILES_DIR/<资产id>/ 下（content.py 上传接口），文件清完
    # 把空了的资产目录也收掉；目录里还有别的东西就留着，不越界。
    asset_dirs: set[Path] = set()
    for rel in images:
        # 走 paths.resolve_within：bare startswith(str(root)) 会把 data/files
        # 匹配给 data/files-evil，一条脏记录就能把删除范围带出门外
        target = resolve_within(FILES_DIR, rel)
        if target is not None and target.is_file():
            target.unlink()
            asset_dirs.add(target.parent)
    for d in asset_dirs:
        if listdir_empty(d):
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
            if listdir_empty(folder):
                folder.rmdir()
        except PathError:
            pass
    # 字幕不在某个 Job 名下，是按项目存的，得单独收——不然删掉项目后
    # data/jobs/subtitles/<旧项目id>/ 会永远留在盘上，备份越背越重还没人能看见它
    jobfiles.remove_project_subtitles(project_id)




def delete_episode_cascade(episode_id: int, actor: str = "平台") -> bool:
    """删一集，并把它名下稿件的正文从工作区一起摘掉。

    为什么不让路由直接 `episodes.delete()` 就完事：`document.episode_id` 是
    `ON DELETE CASCADE`（0013），那条 SQL 会连文档行一起带走，可正文是文件系统里
    的 `.md`——行一没，路径就查不回来了，工作区只剩几份没人认领的孤儿稿。
    删项目那条链（上面的 `delete_project_cascade`）为同一件事已经写过一次清理，
    这里沿用同一套做法与同一个顺序：**先记路径 → 再删行 → 最后落一次 git 提交**。
    走 `git rm` 而不是直接 `unlink`：删一集是创作中心里最重的动作之一，
    正文进了历史就还在，谁想找回那一稿都有路，不必去翻备份。

    作业与产物**不在**删除范围里，这是有意的：`job.episode_id` 是
    `ON DELETE SET NULL`（0015），片子和日志本来就挂在项目上。删一集不该把
    别集也看得见的产物抹掉，也不该让"这一集跑过什么"变成查无此事。
    """
    doc_paths = docs.paths_by_episode(episode_id)
    if not episode_repo.delete(episode_id):
        return False
    for rel in doc_paths:
        try:
            gitrepo.remove_file(rel, f"删除创作单元 {episode_id} 的稿件：{rel}", actor)
        except gitrepo.GitError:
            pass          # 历史记不上不该挡住删集；下面的 unlink 会把文件本身收掉
        try:
            target = path_of(rel)
            if target.is_file():
                target.unlink()
        except PathError:
            pass          # 库里躺着一条越界路径是脏数据，正好不在我们的删除范围里
    # 这一集的文件目录空了顺手收掉，工作区不会攒出一堆空壳（同删项目那一档）
    for rel in doc_paths:
        try:
            folder = path_of(rel).parent
            if listdir_empty(folder):
                folder.rmdir()
        except PathError:
            pass
    return True
