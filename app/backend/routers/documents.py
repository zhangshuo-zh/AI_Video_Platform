"""文档层的接口：元数据在库里，正文在工作区里（规格 02 §6.1）。

端点分三组，边界按"谁改什么"划：
1. 元数据（标题／挂靠的分集／状态）——只动库，不碰文件。
2. 正文（读、写）——必须过 workspace + gitrepo，路径永远由服务端算。
3. 锁与版本——围绕正文的协作与追溯。

三条口径先写在这，免得读代码的人猜：
- **前端从来不传路径**，只传文档 id。路径是建文档时服务端生成后钉在库里的，
  所以"路径穿越"这一类攻击面在这里不存在——存在的只有"库里躺着脏路径"，
  那由 workspace.normalize() 在每次读写前挡住。
- **`expected_version` 必填**。不匹配回 409，不做"最后一次写入覆盖前一次"的便道。
- **正文找不到不等于文档不存在**，回 409 而不是 404：404 在前端会被解释成
  "这不在你的可见范围内"（util.whyHint），而真实情况是"库里有记录、盘上没文件"，
  那是两套事实来源对不上，得说清楚。
"""
from fastapi import APIRouter, Depends, HTTPException, status as http_status

import docai
import gitrepo
import services
from access import ensure, ensure_by
from config import LOCK_MINUTES
from deps import actor_name, require_permission
from paths import describe as describe_path
from models import (DocAiApplyOut, DocAiJobOut, DocAiOptionsOut, DocAiPreviewOut,
                    DocAiSubmitIn, DocContentIn, DocContentOut, DocDiffOut, DocLockOut,
                    DocStatusIn, DocumentIn, DocumentOut, DocSaveOut, DocVersionOut,
                    DocumentUpdateIn)
from repositories.content import episodes as episode_repo
from repositories.documents import documents as docs
from workspace import PathError, path_of

def _git_detail(prefix: str, err: Exception) -> str:
    """把 `git` 的原话拼进 5xx 文案，但**先把路径脱敏**。

    为什么要带原话：git 的 stderr 往往就是原因本身——"没有配置用户身份"、
    "工作区不在磁盘上"、"权限不对"。只回一句"内部错误"的话，人对着屏幕
    无从下手。这是 `gitrepo.GitError` 的既有口径（见它的类注释）。

    为什么要脱敏：实测同一个 stderr 在锁冲突时会写成
    `fatal: Unable to create '/Users/<谁>/…/data/workspace/.git/index.lock'`——
    部署机的家目录与安装路径就这么跟着一条报错出门了，会进 HTTP 响应、
    进浏览器控制台、进用户截图。`paths.describe()` 把这些根换成
    `data/workspace/…` 这种标签：一样能定位，不出家门。
    （`services.py` 里那条 `GitSaveFailed` 早就是这么做的；这里两处直接抛
    `gitrepo.GitError` 的路上漏了。）
    """
    return f"{prefix}{describe_path(str(err))}"


router = APIRouter(prefix="/api", tags=["documents"])

DOC_KINDS_ALLOWED = ("novel", "script", "storyboard")


def _load(doc_id: int, user, need: str = "read"):
    """先按项目判可见性，再取文档行。两件事不能反：

    `ensure_by` 会替我们把"别人藏着的项目"回成 404，
    如果先取行再判权限，就等于先查了库、再决定让不让你看——
    项目不可见时不该有第一次查询。
    """
    ensure_by("document", doc_id, user, need, "文档")
    row = docs.get(doc_id)
    if row is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return row


def _check_episode(project_id: int, episode_id: int | None) -> None:
    """挂靠的分集必须是同一个项目里的。

    不拦的话，把 A 项目的文档挂到 B 项目的集上，创作中心那棵树会出现
    "这一集下面有一份不属于它的项目的稿子"，而且谁都看不见是怎么来的。
    """
    if episode_id is None:
        return
    ep = episode_repo.get(episode_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="要挂靠的创作单元不存在")
    if ep["project_id"] != project_id:
        raise HTTPException(status_code=422, detail="创作单元不属于这个项目，不能挂靠")


@router.get("/projects/{project_id}/documents", response_model=list[DocumentOut])
def list_documents(project_id: int, kind: str | None = None, episode_id: int | None = None,
                   user=Depends(require_permission("doc:read"))) -> list[DocumentOut]:
    """列元数据，一次都不读磁盘（规格 02 §9「仅元数据，不返回全文」）。"""
    ensure(user, project_id, "read")
    if kind and kind not in DOC_KINDS_ALLOWED:
        raise HTTPException(status_code=422,
                            detail=f"kind 只认 {'／'.join(DOC_KINDS_ALLOWED)}")
    return [DocumentOut.from_row(row)
            for row in docs.list_by_project(project_id, kind, episode_id)]


@router.post("/projects/{project_id}/documents", response_model=DocumentOut,
             status_code=http_status.HTTP_201_CREATED)
def create_document(project_id: int, payload: DocumentIn,
                    user=Depends(require_permission("doc:write"))) -> DocumentOut:
    """建一份文档＝工作区里多一个文件 + 历史里多一次提交 + 库里多一行元数据。

    三步谁先谁后由 services.create_document 决定（文件在前、库在后），
    这里只做输入把关。
    """
    ensure(user, project_id, "write")
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="标题不能为空")
    _check_episode(project_id, payload.episode_id)
    try:
        result = services.create_document(project_id, payload.episode_id, payload.kind,
                                         title, payload.content, actor_name(user))
    except PathError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    except services.DocTooLarge as err:
        raise HTTPException(status_code=413, detail=str(err)) from err
    except gitrepo.GitError as err:
        # 建文档失败说 507/500 都行，但要带上 git 原话（已脱敏）：
        # 工作区不在磁盘上、权限不对、git 没装，人看到的都是同一句"失败了"最难查
        raise HTTPException(status_code=500,
                            detail=_git_detail("正文没能提交到 Git：", err)) from err
    return DocumentOut.from_row(result["row"])


@router.get("/documents/{doc_id}", response_model=DocumentOut)
def get_document(doc_id: int, user=Depends(require_permission("doc:read"))) -> DocumentOut:
    return DocumentOut.from_row(_load(doc_id, user, "read"))


@router.put("/documents/{doc_id}", response_model=DocumentOut)
def update_document(doc_id: int, payload: DocumentUpdateIn,
                    user=Depends(require_permission("doc:write"))) -> DocumentOut:
    """改标题与挂靠的分集。**不改路径**（改名不挪文件，理由见仓储层）。"""
    row = _load(doc_id, user, "write")
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="标题不能为空")
    _check_episode(row["project_id"], payload.episode_id)
    updated = docs.update_meta(doc_id, title, payload.episode_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return DocumentOut.from_row(updated)


@router.patch("/documents/{doc_id}/status", response_model=DocumentOut)
def set_doc_status(doc_id: int, payload: DocStatusIn,
                   user=Depends(require_permission("doc:write"))) -> DocumentOut:
    """草稿 → 润色 → 定稿。定稿只是状态，不是"锁死不给改"：
    真要冻住得等 Review 闸口（W14），那一层会绑 git_commit。"""
    row = _load(doc_id, user, "write")
    updated = docs.set_status(row["id"], payload.status)
    if updated is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return DocumentOut.from_row(updated)


@router.delete("/documents/{doc_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_document(doc_id: int, user=Depends(require_permission("doc:write"))) -> None:
    """删文档：从历史里摘掉这一条正文，元数据与锁一起删。

    级别用 write 而不是 delete（"delete" 那一档在后端只留给删项目/管协作人）：
    这一刀是可回退的——`git rm` 是一次提交，那一稿还在历史里，
    要找回来说一声就行。真正的不可逆删除不该由"写文档"这个权限点拿到。
    """
    row = _load(doc_id, user, "write")
    try:
        gitrepo.remove_file(row["rel_path"], f"删除文档：{row['title']}", actor_name(user))
    except gitrepo.GitError as err:
        # 历史记不上就不删元数据：留着行、文件也留着，让人能再试一次，
        # 而不是留下"库里说没了、盘上还有一份"的对不上状态。
        # 回的是脱敏后的 git 原话（见 `_git_detail`）。
        raise HTTPException(status_code=500,
                            detail=_git_detail("Git 没记上这笔删除，文档保持原样：",
                                               err))
    try:
        path_of(row["rel_path"]).unlink(missing_ok=True)
    except PathError:
        pass
    docs.delete(doc_id)


@router.get("/documents/{doc_id}/content", response_model=DocContentOut)
def get_content(doc_id: int, user=Depends(require_permission("doc:read"))) -> DocContentOut:
    """读正文。**版本令牌一起回**，前端下一次保存要原样带回来。"""
    row = _load(doc_id, user, "read")
    try:
        data = services.read_content(row)
    except FileNotFoundError:
        raise HTTPException(
            status_code=409,
            detail=f"库里有这份文档，但工作区里找不到 {row['rel_path']}。"
                   f"要么被手工删了，要么工作区换过目录——先对上文件，再进来编辑")
    except PathError as err:
        # 库里躺着一条越界路径（脏数据／手工改过库）。这一条必须在读文件之前拦住：
        # 读不到是小事，读到了根目录外面那份东西才是大事。
        raise HTTPException(status_code=409,
                            detail=f"这条文档记录的路径不安全，平台不会去读它：{err}") from err
    return DocContentOut(
        id=row["id"], title=row["title"], content=data["content"], version=data["version"],
        git_commit=row["git_commit"],
        lock_holder_id=row["holder_user_id"], lock_holder_name=row["holder_name"],
        lock_expires_at=row["expires_at"] or "",
    )


@router.put("/documents/{doc_id}/content", response_model=DocSaveOut)
def save_content(doc_id: int, payload: DocContentIn,
                 user=Depends(require_permission("doc:write"))) -> DocSaveOut:
    """保存正文。这是整个 W05 最要紧的一个端点，四条规矩都在 services.save_content 里：
    令牌比对、锁检查、原子写、Git 提交失败退回上一版并另存未应用结果。"""
    row = _load(doc_id, user, "write")
    try:
        result = services.save_content(row, payload.content, payload.expected_version,
                                       actor_name(user), user["id"], LOCK_MINUTES)
    except services.Conflict as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except services.LockHeld as err:
        # 423 = 现在轮不到你写；和 409（你手上的太旧）分开，
        # 因为人要做的动作不同：一个是去比对，一个是等对方松手
        raise HTTPException(status_code=423, detail=str(err)) from err
    except services.DocTooLarge as err:
        raise HTTPException(status_code=413, detail=str(err)) from err
    except services.GitSaveFailed as err:
        raise HTTPException(status_code=500, detail=str(err)) from err
    except FileNotFoundError:
        raise HTTPException(status_code=409,
                            detail=f"工作区里找不到 {row['rel_path']}，先对上文件再保存")
    except PathError as err:
        # 和 get_content 同一口径：库里那条路径本身不安全。
        # 这一条拦在读文件之前，所以"越界写"在这一步就没了机会。
        raise HTTPException(status_code=409,
                            detail=f"这条文档记录的路径不安全，平台不会去写它：{err}") from err
    out = result["row"]
    return DocSaveOut(id=out["id"], version=result["version"], git_commit=out["git_commit"],
                      changed=result["changed"], updated_at=out["updated_at"])


@router.post("/documents/{doc_id}/lock", response_model=DocLockOut)
def acquire_lock(doc_id: int, user=Depends(require_permission("doc:write"))) -> DocLockOut:
    """拿编辑锁（前端在开始打字时调一次，之后每次保存自动续期）。

    别人拿着且没过期时**不报错**，回 `state=held` 加对方是谁：
    "抢不到锁"是界面要显示的状态，不是要弹给用户的失败。
    """
    _load(doc_id, user, "write")
    state = docs.acquire_lock(doc_id, user["id"], LOCK_MINUTES)
    lock = docs.lock_of(doc_id)
    return DocLockOut(
        doc_id=doc_id, state=state,
        holder_user_id=lock["holder_user_id"] if lock else None,
        holder_name=lock["holder_name"] if lock else "",
        expires_at=lock["expires_at"] if lock else "",
    )


@router.delete("/documents/{doc_id}/lock", response_model=DocLockOut)
def release_lock(doc_id: int, user=Depends(require_permission("doc:write"))) -> DocLockOut:
    """放锁。只有持锁人放得掉（仓储层里判），管理员也不给强放——
    锁 15 分钟自己会松，为这个加一条特权没意义。"""
    _load(doc_id, user, "write")
    released = docs.release_lock(doc_id, user["id"])
    lock = docs.lock_of(doc_id)
    return DocLockOut(
        doc_id=doc_id, state="released" if released else "not_held",
        holder_user_id=lock["holder_user_id"] if lock else None,
        holder_name=lock["holder_name"] if lock else "",
        expires_at=lock["expires_at"] if lock else "",
    )


@router.get("/documents/{doc_id}/versions", response_model=list[DocVersionOut])
def list_versions(doc_id: int, user=Depends(require_permission("doc:read"))) -> list[DocVersionOut]:
    """这一份正文的提交历史（新→旧，最多 30 条）。数据源是 Git，不是库。"""
    row = _load(doc_id, user, "read")
    return [DocVersionOut(**item) for item in gitrepo.history(row["rel_path"])]


@router.get("/documents/{doc_id}/versions/{commit}/diff", response_model=DocDiffOut)
def version_diff(doc_id: int, commit: str,
                 user=Depends(require_permission("doc:read"))) -> DocDiffOut:
    """某一次提交在这一份上造成的差异。

    `commit` 先做格式校验再进 git 的参数列表（gitrepo.sha_ok）：
    这一位是命令行参数，`--help`、`-p` 这种字符串传进去，命令行为就变了。
    """
    row = _load(doc_id, user, "read")
    if not gitrepo.sha_ok(commit):
        raise HTTPException(status_code=422, detail="commit 号格式不对")
    try:
        return DocDiffOut(commit=commit, diff=gitrepo.diff(commit, row["rel_path"]))
    except gitrepo.GitError as err:
        raise HTTPException(status_code=404, detail=f"看不到这一版的差异：{err}") from err


# ---------- 文档 AI 操作（W11） ----------
#
# 四条端点围着同一条 Job 转：计价 → 提交 → 看结果/diff → 应用。
# 它们都不自己写文件：应用那一步仍走 services.save_content()，
# 所以版本冲突、编辑锁、Git 提交三道关与手工保存是同一套。
def _ai_errors(call):
    """把 AI 那一层的异常翻成 HTTP。六个入口口径一致，不各写一遍。

    `FileNotFoundError` 与 `PathError` 回 409 而不是 404：
    库里有记录、盘上找不到（或路径越界）是两套事实源对不上，
    和"这份文档不属于你"不是一回事。
    """
    try:
        return call()
    except docai.DocAiInvalid as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    except services.DocAiJobMissing as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except services.Conflict as err:
        raise HTTPException(status_code=409, detail=str(err)) from err
    except services.LockHeld as err:
        raise HTTPException(status_code=423, detail=str(err)) from err
    except services.DocTooLarge as err:
        raise HTTPException(status_code=413, detail=str(err)) from err
    except services.GitSaveFailed as err:
        raise HTTPException(status_code=500, detail=str(err)) from err
    except FileNotFoundError as err:
        raise HTTPException(status_code=409,
                            detail="工作区里找不到这份正文，先对上文件再做 AI 操作") from err
    except PathError as err:
        raise HTTPException(status_code=409,
                            detail=f"这条文档记录的路径不安全，平台不会碰它：{err}") from err


@router.get("/documents/{doc_id}/ai-options", response_model=DocAiOptionsOut)
def ai_options(doc_id: int, operation: str, scope: str = "document",
               start: int = 0, end: int = 0, selection_text: str = "",
               user=Depends(require_permission("doc:read"))) -> DocAiOptionsOut:
    """弹窗先问这一条：模型、单价、预计用量、健康状态。

    读权限就够：这一步不花钱、不写库，也不会把正文发出去——
    它只数一遍选中多少字。
    """
    row = _load(doc_id, user, "read")
    data = _ai_errors(lambda: services.doc_ai_options(
        row, operation, scope, start, end, selection_text))
    return DocAiOptionsOut(**data)


@router.post("/documents/{doc_id}/ai-jobs", response_model=DocAiJobOut,
             status_code=http_status.HTTP_202_ACCEPTED)
def create_ai_job(doc_id: int, payload: DocAiSubmitIn,
                  user=Depends(require_permission("doc:write"))) -> DocAiJobOut:
    """提交一次 AI 改写。返回 202：作业排队，正文此刻一个字都没动。

    提示词、base_version、选区偏移、apply_mode 全部冻进 Job；
    模型跑完之后的写回（`/apply`）只认这份快照，不认客户端后来传的正文。
    """
    row = _load(doc_id, user, "write")
    job = _ai_errors(lambda: services.submit_doc_ai_job(
        row, payload, user["id"], actor_name(user)))
    return DocAiJobOut(**services.doc_ai_job_view(job))


@router.get("/documents/{doc_id}/ai-jobs/{job_id}", response_model=DocAiPreviewOut)
def get_ai_job(doc_id: int, job_id: int,
               user=Depends(require_permission("doc:read"))) -> DocAiPreviewOut:
    """作业跑到哪一步 + 应用后的 diff（成功后才算）。

    前端轮询这一条。文档在 AI 运行期间被改过时 `conflict=true`、
    `can_apply=false`：偏移已经对不上，宁可让人重跑，也不按旧位置拼回去。
    """
    row = _load(doc_id, user, "read")
    data = _ai_errors(lambda: services.doc_ai_preview(row, job_id))
    return DocAiPreviewOut(**data)


@router.post("/documents/{doc_id}/ai-jobs/{job_id}/apply", response_model=DocAiApplyOut)
def apply_ai_job(doc_id: int, job_id: int,
                 user=Depends(require_permission("doc:write"))) -> DocAiApplyOut:
    """把成功的结果写回正文：写盘 + Git 提交 + 更新元数据，一次原子保存。

    失败的任何一步都不会让正文半新半旧：`save_content` 先写临时文件再
    `os.replace`，Git 失败会把上一版写回去。版本号只在文件真的落盘后前进。
    """
    row = _load(doc_id, user, "write")
    result = _ai_errors(lambda: services.apply_doc_ai(
        row, job_id, actor_name(user), user["id"]))
    saved, ref, job = result["saved"], result["ref"], result["job"]
    out_row = saved["row"]
    return DocAiApplyOut(
        id=out_row["id"], version=saved["version"],
        git_commit=out_row["git_commit"], changed=saved["changed"],
        updated_at=out_row["updated_at"], job_id=job["id"],
        operation=ref["operation"],
        operation_label=ref.get("operation_label")
        or docai.operation_label(ref["operation"]))
