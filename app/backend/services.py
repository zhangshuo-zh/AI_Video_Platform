"""跨模块复用的业务动作。放在路由之外的唯一理由：有第二个调用方。

三条：删项目时把定妆图文件也清掉；标签归一化；文档正文的"读—写—提交"这条链。
项目中心（删单个项目）和系统设置（删账号时连带删他名下的项目）都要走第一条；
项目标签与资产标签都要走第二条；第三条更要放在这里——W11 的 AI 改写跑完之后
要落盘，那时候的调用方是 worker 而不是路由，实现必须已经在路由之外。
写在某个 router 里让另一个 router 去 import 是反的（规格 02 §4.1）。
"""
import csv
import hashlib
import io
import json
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace

import access
import adapters
import docai
import extraction
import gitrepo
import jobfiles
import reviewgate
from config import ALLOW_MOCK, DOC_MAX_BYTES, FILES_DIR, LOCK_MINUTES
from models import CAPABILITIES, JOB_STATUSES, JobEstimateIn
from repositories.audio import clips
from repositories.content import assets
from repositories.dashboard import dashboard
from repositories.documents import documents as docs
from repositories.extraction import runs as extraction_runs
from repositories.jobs import jobs as job_repo
from repositories.projects import projects as project_repo
from repositories.reviews import reviews
from repositories.providers import providers as provider_repo
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
    # Job 目录在删行之前记下来：行一删就查不到 id，日志与产物会变成孤儿
    job_ids = job_repo.ids_for_project(project_id)
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
    # 作业日志与产物整目录摘掉；Job 行已随项目外键 CASCADE 消失
    for job_id in job_ids:
        jobfiles.remove_job(job_id)


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
        raise GitSaveFailed(
            f"Git 提交失败，正文已退回上一版；你这次写的内容另存在 {saved}，"
            f"不用重敲。git 说：{exc}") from exc

    # commit 为 None 表示"内容和上一版一模一样"：不写库、不推 updated_at——
    # 那一列的语义是"内容动过"，点一下保存什么都没改却把时间推走，等于让界面说谎。
    # 直接把现状回给前端，前端那行"改动"就不会跳。
    row = docs.save(doc["id"], commit) if commit else docs.get(doc["id"])
    docs.renew_on_save(doc["id"], user_id, minutes)
    return {"row": row, "version": version_token(content), "commit": commit,
            "changed": commit is not None}


def preserve_unapplied(doc, content: str) -> str:
    """把没提交上去的正文原样留一份，返回那个路径（进错误消息给人看）。"""
    target = unapplied_dir() / (
        f"doc{doc['id']}-{datetime.now():%Y%m%d-%H%M%S}.md")
    target.write_text(content, encoding="utf-8")
    return str(target)


# ---------- 文档 AI 操作（W11） ----------
class DocAiJobMissing(Exception):
    """作业不存在，或不是这份文档的 AI 作业。路由回 404，不泄露别的项目。"""


def text_ai_route() -> dict:
    """文本 AI 的唯一路由出口：默认轨 + 清单里的模型/价格 + 健康。

    文本没有合格的本地路线（规格 02 §8.2），所以这里只接受 cloud；
    默认轨没配好就报错，不偷偷换到另一家。
    """
    try:
        got = resolve_provider("text")
    except NoRoute as exc:
        raise docai.DocAiInvalid(f"文本 AI 路由不可用：{exc}") from exc
    if got["backend"] != "cloud":
        raise docai.DocAiInvalid("文本 AI 操作需要云端文本 provider；本地没有合格的文本路线")
    adapter = got["adapter"]
    model = next((m for m in adapter.models if m.code == got["model"]), None)
    if model is None:
        raise docai.DocAiInvalid(
            f"模型 {got['model']} 不在清单里；先到系统设置核对文本槽位")
    price = next((p for p in model.prices if p.unit == "million_token"), None)
    if price is None:
        raise docai.DocAiInvalid(
            f"文本模型 {model.code} 没有登记百万 token 价格，不能提交计费作业")
    health = adapters.health(got["row"])
    return {"row": got["row"], "adapter": adapter, "model": model,
            "price": price, "provider": got["provider"],
            "health_state": health["state"], "health_detail": health["detail"]}


def _ai_input(doc, operation: str, scope: str, start: int,
              end: int, selection_text: str) -> tuple[dict, dict]:
    """读盘上正文并收敛范围。所有 AI 入口都先过这一条，口径只有一份。"""
    data = read_content(doc)
    part = docai.resolve_scope(data["content"], operation, scope, start, end,
                               selection_text)
    return data, part


def doc_ai_options(doc, operation: str, scope: str, start: int, end: int,
                   selection_text: str = "") -> dict:
    """给弹窗的计价与健康。数量按“输入字数 + 输出上限”算，服务端不给猜。"""
    info = docai.operation_info(operation)
    _data, part = _ai_input(doc, operation, scope, start, end, selection_text)
    route = text_ai_route()
    quantity = docai.estimate_quantity(len(part["text"]), info["max_tokens"])
    return {
        "operation": operation,
        "operation_label": info["label"],
        "provider": route["provider"],
        "model": route["model"].code,
        "unit": route["price"].unit,
        "unit_price_micros": route["price"].amount_micros,
        "currency": route["price"].currency,
        "source": route["price"].source,
        "max_tokens": info["max_tokens"],
        "chars": len(part["text"]),
        "estimated_quantity": quantity,
        "health_state": route["health_state"],
        "health_detail": route["health_detail"],
        "hint": info["hint"],
    }


def submit_doc_ai_job(doc, payload, created_by: int | None, actor: str) -> dict:
    """把一次文档 AI 操作落成一条 text Job。

    Job 里冻结三样东西：拼好的提示词（含选中的原文）、base_version（正文哈希）、
    选区偏移与 apply_mode。应用时只认这份快照，不拿客户端后来传来的新正文。
    """
    data, part = _ai_input(doc, payload.operation, payload.scope,
                           payload.start, payload.end, payload.selection_text)
    route = text_ai_route()
    # 健康不过就不建作业：作业一旦落行，成本表里就有一条账单，
    # 而缺密钥这种失败一次请求都不会发出去——那笔钱不该出现在账上。
    if route["health_state"] != "ok":
        raise docai.DocAiInvalid(
            f"文本 provider 现在不可用：{route['health_detail']}")
    price = route["price"]
    estimate = payload.estimate
    if not payload.user_confirmed_cloud:
        raise docai.DocAiInvalid("云端文本作业要先确认按供应商计费")
    if estimate.unit != price.unit:
        raise docai.DocAiInvalid(f"计价单位应为 {price.unit}，请刷新计价后重试")
    if estimate.currency != price.currency:
        raise docai.DocAiInvalid(f"币种应为 {price.currency}，请刷新计价后重试")
    if estimate.unit_price_micros != price.amount_micros:
        raise docai.DocAiInvalid("单价与清单里的官方价不一致，请刷新计价后重试")

    prompt, params = docai.build_prompt(
        payload.operation, doc["title"], data["content"],
        part["text"], payload.instruction)
    errors = adapters.validate_params(route["adapter"], route["model"].code, params)
    if errors:
        raise docai.DocAiInvalid("；".join(errors))

    # 价格来源以清单为准，不采信客户端传上来的 source。
    # 数量仍用用户确认过的那个数（界面按同一公式预填）。
    confirmed = JobEstimateIn(
        quantity=estimate.quantity, unit=price.unit,
        unit_price_micros=price.amount_micros, currency=price.currency,
        source=price.source[:200])
    cost = _cost_of(SimpleNamespace(estimate=confirmed, user_confirmed_cloud=True),
                    "cloud", created_by)
    refs = [{
        "doc_id": doc["id"],
        "doc_title": doc["title"],
        "operation": payload.operation,
        "operation_label": docai.operation_label(payload.operation),
        "base_version": data["version"],
        "scope": part["scope"],
        "selection_start": part["start"],
        "selection_end": part["end"],
        "apply_mode": part["apply_mode"],
    }]
    return job_repo.create({
        "project_id": doc["project_id"],
        "episode_id": doc["episode_id"],
        "shot_id": None,
        "capability": "text",
        # kind 仍是 text：job.kind 有 CHECK 白名单，不为 W11 重建整张表。
        # “这条 text 作业属于哪份文档”由 input_refs 里的 doc_id/operation 回答。
        "kind": "text",
        "execution_mode": "cloud",
        "provider_code": route["provider"],
        "model_code": route["model"].code,
        "adapter_version": adapters.MANIFEST_VERSION,
        "prompt_snapshot": prompt,
        "params": params,
        "input_refs": refs,
        "priority": 0,
        "user_confirmed_cloud": True,
        "created_by": created_by,
        **cost,
    })


def _ai_ref(job) -> dict:
    try:
        refs = json.loads(job["input_refs_json"] or "[]")
    except (ValueError, TypeError):
        refs = []
    ref = refs[0] if isinstance(refs, list) and refs and isinstance(refs[0], dict) else None
    if not ref or not ref.get("doc_id") or not ref.get("operation") or not ref.get("base_version"):
        raise DocAiJobMissing("这条作业不是文档 AI 操作")
    return ref


def _ai_job_for_doc(doc, job_id: int):
    job = job_repo.get(job_id)
    if job is None:
        raise DocAiJobMissing("AI 作业不存在")
    ref = _ai_ref(job)
    if job["project_id"] != doc["project_id"] or int(ref["doc_id"]) != int(doc["id"]):
        raise DocAiJobMissing("AI 作业不存在")
    return job, ref


def _ai_result_text(job) -> str:
    path = jobfiles.artifact_at(job["result_paths_json"], 0)
    if path is None:
        raise docai.DocAiInvalid("作业成功了，但找不到可读的正文产物")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise docai.DocAiInvalid(f"读不到 AI 结果：{exc}") from exc
    if len(text.encode("utf-8")) > DOC_MAX_BYTES:
        raise docai.DocAiInvalid(
            f"AI 结果超过单份正文上限 {DOC_MAX_BYTES // 1024} KB，不能应用")
    return text


def doc_ai_job_view(job) -> dict:
    """提交后回给界面的那份摘要。"""
    ref = _ai_ref(job)
    return {
        "job_id": job["id"], "status": job["status"],
        "status_label": JOB_STATUSES.get(job["status"], job["status"]),
        "operation": ref["operation"],
        "operation_label": ref.get("operation_label")
        or docai.operation_label(ref["operation"]),
        "apply_mode": ref.get("apply_mode", "replace_document"),
        "base_version": ref["base_version"],
        "selection_start": int(ref.get("selection_start") or 0),
        "selection_end": int(ref.get("selection_end") or 0),
        "provider_code": job["provider_code"], "model_code": job["model_code"],
        "error_code": job["error_code"], "error_message": job["error_message"],
    }


def doc_ai_preview(doc, job_id: int) -> dict:
    """作业状态 + “盘上正文 → 应用后正文”的统一 diff。

    文档在 AI 跑的过程中被改过时**不计算 diff、不允许应用**：
    这时按旧偏移拼回去会改错位置，宁可让人重新跑一次。
    """
    job, ref = _ai_job_for_doc(doc, job_id)
    base = read_content(doc)
    out = {
        "job_id": job["id"], "status": job["status"],
        "status_label": JOB_STATUSES.get(job["status"], job["status"]),
        "operation": ref["operation"],
        "operation_label": ref.get("operation_label")
        or docai.operation_label(ref["operation"]),
        "apply_mode": ref.get("apply_mode", "replace_document"),
        "base_version": ref["base_version"], "current_version": base["version"],
        "can_apply": False, "conflict": False,
        "diff": "", "content": "",
        "base_chars": len(base["content"]), "result_chars": 0, "applied_chars": 0,
        "note": "作业还没有成功结束；原文不会变化。",
        "error_code": job["error_code"], "error_message": job["error_message"],
    }
    if job["status"] != "succeeded":
        return out
    if base["version"] != ref["base_version"]:
        out["conflict"] = True
        out["note"] = ("文档在 AI 运行期间被改过（版本已变），这次结果不能直接应用；"
                       "原文保持不动，需要的话重新跑一次。")
        return out
    try:
        result = _ai_result_text(job)
        applied = docai.apply_text(base["content"], result,
                                   ref.get("apply_mode", "replace_document"),
                                   int(ref.get("selection_start") or 0),
                                   int(ref.get("selection_end") or 0))
    except docai.DocAiInvalid as exc:
        out["note"] = str(exc)
        return out
    out.update({
        "can_apply": True, "diff": docai.diff_text(base["content"], applied),
        "content": result, "result_chars": len(result),
        "applied_chars": len(applied),
        "note": "版本一致，可以应用；写回时仍然过锁与冲突检查。",
    })
    return out


def apply_doc_ai(doc, job_id: int, actor: str, user_id: int) -> dict:
    """把成功的 AI 结果写回正文。写盘、锁、冲突、Git 全部复用 save_content。"""
    job, ref = _ai_job_for_doc(doc, job_id)
    if job["status"] != "succeeded":
        raise docai.DocAiInvalid("AI 作业没有成功结束，不能应用")
    base = read_content(doc)
    if base["version"] != ref["base_version"]:
        raise Conflict(
            "文档在 AI 运行期间被改过（版本已变），这次结果不能应用。"
            "原文保持不动；需要的话重新跑一次。", base["version"])
    result = _ai_result_text(job)
    applied = docai.apply_text(base["content"], result,
                               ref.get("apply_mode", "replace_document"),
                               int(ref.get("selection_start") or 0),
                               int(ref.get("selection_end") or 0))
    label = ref.get("operation_label") or docai.operation_label(ref["operation"])
    saved = save_content(
        doc, applied, base["version"], actor, user_id, LOCK_MINUTES,
        commit_message=f"AI {label}：{doc['title']}（作业 #{job['id']}）")
    return {"job": job, "ref": ref, "saved": saved}


# ---------- 文本抽取候选（W13） ----------
class ExtractMissing(Exception):
    """抽取运行或候选不存在，或不属于这份文档／这个项目。路由回 404。"""


class ExtractConflict(Exception):
    """原文版本已变、候选已被别人处理，或同名资产已存在。路由回 409。"""


def _extract_run_materialize(run) -> None:
    """Job 成功后把模型输出落成候选。只在第一次调用时写。

    为什么不放到 worker 里：worker 只认识"跑命令、落文件"，抽取的 JSON 形状
    与字段白名单是业务知识；把它塞进执行壳会让两个进程互相依赖。
    这里做也没关系——GET 是幂等的，materialized_at 一写就不会重复。
    """
    if run["status"] != "pending":
        return
    if run["job_status"] != "succeeded":
        if run["job_status"] in ("failed", "cancelled"):
            extraction_runs.set_status(run["id"], "failed",
                                       run["error_message"] or "作业没有成功结束")
        return
    job = job_repo.get(run["job_id"])
    if job is None:
        extraction_runs.set_status(run["id"], "failed", "找不到对应的作业")
        return
    try:
        path = jobfiles.artifact_at(job["result_paths_json"], 0)
        if path is None:
            raise extraction.ExtractInvalid("作业成功了，但没有可读的结果文件")
        raw = path.read_text(encoding="utf-8")
        items, note = extraction.parse_output(_extraction_content(run), raw)
    except (OSError, UnicodeError, extraction.ExtractInvalid) as exc:
        extraction_runs.set_status(run["id"], "parse_failed", str(exc)[:500])
        return
    extraction_runs.insert_items(run["id"], items)
    extraction_runs.set_status(run["id"], "done", note, materialized=True)


def _extraction_content(run) -> str:
    """物化解析要做原文证据校验，所以必须把抽的那一版正文读回来。

    文档删了就无法校验证据，按 parse_failed 处理；不拿"标题相同"的别份正文顶替。
    """
    if run["doc_id"] is None:
        raise extraction.ExtractInvalid("原文档已删除，无法校验候选证据")
    doc = docs.get(run["doc_id"])
    if doc is None:
        raise extraction.ExtractInvalid("原文档已删除，无法校验候选证据")
    data = read_content(doc)
    if data["version"] != run["base_version"]:
        raise extraction.ExtractInvalid(
            "文档在抽取之后被改过，旧证据无法逐字核对；请重新抽取")
    return data["content"]


def extraction_submit(doc, payload, created_by: int | None) -> dict:
    """把一次抽取落成 text Job，25 小时内同文同模型同提示词版本直接复用。"""
    data = read_content(doc)
    prompt, params = extraction.build_prompt(doc["title"], data["content"],
                                             payload.instruction)
    route = text_ai_route()
    if route["health_state"] != "ok":
        raise extraction.ExtractInvalid(f"文本 provider 现在不可用：{route['health_detail']}")
    price = route["price"]
    estimate = payload.estimate
    if not payload.user_confirmed_cloud:
        raise extraction.ExtractInvalid("云端文本作业要先确认按供应商计费")
    if estimate.unit != price.unit:
        raise extraction.ExtractInvalid(f"计价单位应为 {price.unit}，请刷新计价后重试")
    if estimate.currency != price.currency:
        raise extraction.ExtractInvalid(f"币种应为 {price.currency}，请刷新计价后重试")
    if estimate.unit_price_micros != price.amount_micros:
        raise extraction.ExtractInvalid("单价与清单里的官方价不一致，请刷新计价后重试")
    errors = adapters.validate_params(route["adapter"], route["model"].code, params)
    if errors:
        raise extraction.ExtractInvalid("；".join(errors))

    existing = extraction_runs.find_recent(
        doc["id"], data["version"], route["model"].code, extraction.PROMPT_VERSION)
    if existing is not None:
        return {"run": existing, "deduped": True}

    confirmed = JobEstimateIn(
        quantity=estimate.quantity, unit=price.unit,
        unit_price_micros=price.amount_micros, currency=price.currency,
        source=price.source[:200])
    cost = _cost_of(SimpleNamespace(estimate=confirmed, user_confirmed_cloud=True),
                    "cloud", created_by)
    job = job_repo.create({
        "project_id": doc["project_id"],
        "episode_id": doc["episode_id"],
        "shot_id": None,
        "capability": "text",
        "kind": "text",
        "execution_mode": "cloud",
        "provider_code": route["provider"],
        "model_code": route["model"].code,
        "adapter_version": adapters.MANIFEST_VERSION,
        "prompt_snapshot": prompt,
        "params": params,
        "input_refs": [{
            "operation": "extract",
            "doc_id": doc["id"],
            "doc_title": doc["title"],
            "base_version": data["version"],
            "prompt_version": extraction.PROMPT_VERSION,
        }],
        "priority": 0,
        "user_confirmed_cloud": True,
        "created_by": created_by,
        **cost,
    })
    run = extraction_runs.create_run(
        project_id=doc["project_id"], doc_id=doc["id"], job_id=job["id"],
        doc_title=doc["title"], base_version=data["version"],
        content_hash=data["version"], model_code=route["model"].code,
        prompt_version=extraction.PROMPT_VERSION, created_by=created_by)
    return {"run": run, "deduped": False}


_STATUS_LABELS = {
    "pending": "抽取中", "done": "候选已生成",
    "failed": "抽取失败", "parse_failed": "输出无法解析",
}
_ITEM_LABELS = {"pending": "待处理", "accepted": "已入库",
                "merged": "已合并", "rejected": "已驳回"}


def extraction_view(run, doc=None) -> dict:
    """运行摘要。`stale` 让界面一眼看出证据对的是旧正文。"""
    _extract_run_materialize(run)
    run = extraction_runs.get_run(run["id"]) or run
    current = ""
    if doc is not None:
        try:
            current = read_content(doc)["version"]
        except Exception:
            current = ""
    elif run["doc_id"] is not None:
        d = docs.get(run["doc_id"])
        if d is not None:
            try:
                current = read_content(d)["version"]
            except Exception:
                current = ""
    return {
        "id": run["id"], "project_id": run["project_id"], "doc_id": run["doc_id"],
        "doc_title": run["doc_title"], "job_id": run["job_id"],
        "status": run["status"], "status_label": _STATUS_LABELS.get(run["status"], run["status"]),
        "model_code": run["model_code"], "prompt_version": run["prompt_version"],
        "base_version": run["base_version"], "current_version": current,
        "stale": bool(current and current != run["base_version"]),
        "note": run["note"],
        "counts": extraction_runs.counts(run["id"]),
        "cost_micros": run["cost_micros"], "currency": run["currency"],
        "billable_status": run["billable_status"] or "",
        "error_code": run["error_code"], "error_message": run["error_message"],
        "created_at": run["created_at"],
    }


def extraction_item_view(row) -> dict:
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    try:
        evidence = json.loads(row["evidence"] or "{}")
    except ValueError:
        evidence = {}
    return {
        "id": row["id"], "run_id": row["run_id"], "kind": row["kind"],
        "kind_label": extraction.KIND_LABELS.get(row["kind"], row["kind"]),
        "payload": payload, "confidence": row["confidence"], "evidence": evidence,
        "status": row["status"],
        "status_label": _ITEM_LABELS.get(row["status"], row["status"]),
        "asset_id": row["asset_id"], "decided_by_name": row["decided_by_name"] or "",
        "decided_at": row["decided_at"],
    }


def extraction_items(run, kind: str | None = None, status: str | None = None) -> list[dict]:
    _extract_run_materialize(run)
    run_id = run["id"]
    return [extraction_item_view(row)
            for row in extraction_runs.items(run_id, kind, status)]


def _load_extract_item(item_id: int, user, need: str = "write"):
    row = extraction_runs.get_item(item_id)
    if row is None:
        raise ExtractMissing("候选不存在")
    run = extraction_runs.get_run(row["run_id"])
    if run is None:
        raise ExtractMissing("候选不存在")
    access.ensure(user, run["project_id"], need)
    return row, run


def _candidate_fields(item, overrides) -> tuple[str, str, str, list[str], list[str]]:
    try:
        payload = json.loads(item["payload"] or "{}")
    except ValueError:
        payload = {}
    if item["kind"] == "character":
        summary = "；".join(x for x in (payload.get("role"), payload.get("appearance"),
                                       payload.get("personality")) if x)
        prompt = payload.get("suggested_prompt", "")
    elif item["kind"] == "scene":
        summary = "；".join(x for x in (payload.get("time_of_day"), payload.get("weather"),
                                       payload.get("description")) if x)
        prompt = payload.get("suggested_prompt", "")
    else:
        summary = payload.get("description", "")
        prompt = payload.get("suggested_prompt", "")
    aliases = payload.get("aliases") or []
    name = (overrides.name or payload.get("name") or "").strip()
    if overrides.name is not None:
        name = overrides.name.strip()
    if overrides.summary is not None:
        summary = overrides.summary.strip()
    if overrides.consistency_prompt is not None:
        prompt = overrides.consistency_prompt.strip()
    if overrides.aliases is not None:
        aliases = overrides.aliases
    return name, summary, prompt, list(aliases), list(overrides.tags or [])


def extraction_accept(item_id: int, overrides, user) -> dict:
    """确认一条候选：从暂存表落成 asset。镜头候选走导出，不在这里入库。"""
    row, run = _load_extract_item(item_id, user, "write")
    if row["kind"] == "shot":
        raise extraction.ExtractInvalid("镜头候选只导出 Markdown，不入资产库")
    if row["status"] != "pending":
        raise ExtractConflict("这条候选已经处理过了，刷新后再看")
    doc = docs.get(run["doc_id"]) if run["doc_id"] else None
    if doc is None:
        raise ExtractConflict("原文档已删除，不能再按这条证据入库")
    if read_content(doc)["version"] != run["base_version"]:
        raise ExtractConflict("文档在抽取之后被改过，旧证据不能再用；请重新抽取")
    name, summary, prompt, aliases, tags = _candidate_fields(row, overrides)
    if not name:
        raise extraction.ExtractInvalid("资产名不能为空")
    if len(name) > 60:
        raise extraction.ExtractInvalid("资产名最多 60 字")
    if assets.exists_name(run["project_id"], row["kind"], name):
        raise ExtractConflict("同一范围、同一类型里已经有这个名字；请用合并")
    asset = assets.create(run["project_id"], row["kind"], name, summary, prompt,
                          aliases, tags)
    if not extraction_runs.decide(row["id"], "accepted", user["id"], asset["id"]):
        raise ExtractConflict("这条候选已经处理过了，刷新后再看")
    return {"item": extraction_runs.get_item(row["id"]), "asset": asset}


def extraction_merge(item_id: int, asset_id: int, user) -> dict:
    """把候选并进已有资产：别名、简介与提示词只补空，不覆盖人已经写的。"""
    row, run = _load_extract_item(item_id, user, "write")
    if row["kind"] == "shot":
        raise extraction.ExtractInvalid("镜头候选只导出 Markdown，不入资产库")
    if row["status"] != "pending":
        raise ExtractConflict("这条候选已经处理过了，刷新后再看")
    target = assets.get(asset_id)
    if target is None or (target["project_id"] not in (None, run["project_id"])):
        raise ExtractMissing("目标资产不存在")
    if target["kind"] != row["kind"]:
        raise extraction.ExtractInvalid("候选类型与目标资产类型不一致")
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    aliases = json.loads(target["aliases"] or "[]")
    for name in [payload.get("name"), *(payload.get("aliases") or [])]:
        if name and name != target["name"] and name not in aliases:
            aliases.append(name)
    if row["kind"] == "character":
        extra = "；".join(x for x in (payload.get("appearance"),
                                     payload.get("personality")) if x)
    else:
        extra = payload.get("description", "")
    summary = target["summary"] or extra
    prompt = target["consistency_prompt"] or payload.get("suggested_prompt", "")
    tags = json.loads(target["tags"] or "[]") if "tags" in target.keys() else []
    assets.update(target["id"], kind=target["kind"], name=target["name"],
                  summary=summary, consistency_prompt=prompt, aliases=aliases,
                  tags=tags, project_id=target["project_id"])
    if not extraction_runs.decide(row["id"], "merged", user["id"], target["id"]):
        raise ExtractConflict("这条候选已经处理过了，刷新后再看")
    return {"item": extraction_runs.get_item(row["id"]), "asset": assets.get(target["id"])}


def extraction_reject(item_id: int, reason: str, user) -> dict:
    row, _run = _load_extract_item(item_id, user, "write")
    if row["status"] != "pending":
        raise ExtractConflict("这条候选已经处理过了，刷新后再看")
    if not extraction_runs.decide(row["id"], "rejected", user["id"]):
        raise ExtractConflict("这条候选已经处理过了，刷新后再看")
    return {"item": extraction_runs.get_item(row["id"]), "reason": reason}


# ---------- 审核中心（W14） ----------
class ReviewMissing(Exception):
    """还没有校验报告，或报告不属于这份文档。路由回 404／409。"""


class ReviewStale(Exception):
    """正文版本变了，旧报告不能当作新版本已通过。路由回 409。"""


_REVIEW_STATUS = {"passed": "通过", "failed": "未通过"}
_DECISION_LABELS = {"approved": "通过", "rejected": "驳回", "returned": "退回"}


def _review_signature_view(row) -> dict:
    return {
        "id": row["id"], "anchor": row["anchor"], "decision": row["decision"],
        "decision_label": _DECISION_LABELS.get(row["decision"], row["decision"]),
        "note": row["note"], "signed_by_name": row["signed_by_name"] or "",
        "signed_at": row["signed_at"],
    }


def review_gate(doc, user_id: int | None) -> dict:
    """跑一遍确定性校验并落报告。不写正文、不建 Job（规则检查不花钱）。"""
    data = read_content(doc)
    status, findings, summary = reviewgate.run(data["content"])
    gate = reviews.create_gate(
        project_id=doc["project_id"], doc_id=doc["id"],
        base_version=data["version"], status=status, findings=findings,
        summary=summary, created_by=user_id)
    return review_state(doc, can_sign=True, gate=gate)


def review_state(doc, can_sign: bool = False, gate=None) -> dict:
    """当前文档的审核面：最新报告 + 同版本签字 + 是否失配。"""
    data = read_content(doc)
    gate = gate or reviews.latest_gate(doc["id"])
    if gate is None:
        return {
            "id": None, "project_id": doc["project_id"], "doc_id": doc["id"],
            "doc_title": doc["title"], "base_version": "",
            "current_version": data["version"], "stale": False,
            "status": "none", "status_label": "未校验",
            "findings": [], "summary": {}, "counts": {}, "signatures": [],
            "can_sign": False, "created_by_name": "", "created_at": "",
        }
    try:
        findings = json.loads(gate["findings_json"] or "[]")
    except ValueError:
        findings = []
    try:
        summary = json.loads(gate["summary_json"] or "{}")
    except ValueError:
        summary = {}
    stale = data["version"] != gate["base_version"]
    signs = reviews.signs(doc["id"], gate["base_version"])
    return {
        "id": gate["id"], "project_id": doc["project_id"], "doc_id": doc["id"],
        "doc_title": doc["title"], "base_version": gate["base_version"],
        "current_version": data["version"], "stale": stale,
        "status": gate["status"],
        "status_label": _REVIEW_STATUS.get(gate["status"], gate["status"]),
        "findings": findings, "summary": summary,
        "counts": reviews.counts(doc["id"], gate["base_version"]),
        "signatures": [_review_signature_view(row) for row in signs],
        "can_sign": bool(can_sign and not stale),
        "created_by_name": "", "created_at": gate["created_at"],
    }


def review_sign(doc, payload, user) -> dict:
    """按锚点签字。报告必须存在、必须对应当前正文版本、锚点必须在报告里。"""
    data = read_content(doc)
    gate = reviews.latest_gate(doc["id"])
    if gate is None:
        raise ReviewMissing("这份文档还没有校验报告；先跑一次校验")
    if gate["base_version"] != data["version"]:
        raise ReviewStale("正文在报告之后被改过；旧报告的签字不能算到新版本上，先重新校验")
    try:
        findings = json.loads(gate["findings_json"] or "[]")
    except ValueError:
        findings = []
    anchors = {f.get("anchor") for f in findings}
    if payload.anchor not in anchors and payload.anchor != "document":
        raise reviewgate.ReviewInvalid("这个锚点不在本次报告里")
    reviews.upsert_sign(
        project_id=doc["project_id"], doc_id=doc["id"], gate_id=gate["id"],
        base_version=gate["base_version"], anchor=payload.anchor,
        decision=payload.decision, note=payload.note, user_id=user["id"])
    return review_state(doc, can_sign=True, gate=gate)


def review_queue(project_id: int) -> list[dict]:
    """项目里的文档审核队列。只读聚合：最新报告 + 当前版本是否失配。"""
    from models import DOC_KINDS

    out = []
    for doc in docs.list_by_project(project_id):
        try:
            data = read_content(doc)
        except Exception:
            continue
        gate = reviews.latest_gate(doc["id"])
        counts = reviews.counts(doc["id"], data["version"])
        if gate is None:
            out.append({
                "doc_id": doc["id"], "doc_title": doc["title"],
                "doc_kind": doc["kind"], "kind_label": DOC_KINDS.get(doc["kind"], doc["kind"]),
                "episode_id": doc["episode_id"], "episode_title": doc["episode_title"],
                "doc_status": doc["status"], "current_version": data["version"],
                "gate_status": "none", "gate_status_label": "未校验",
                "gate_at": "", "findings": 0, "errors": 0, "warnings": 0,
                "signed": sum(counts.values()), "approved": counts["approved"],
                "rejected": counts["rejected"], "returned": counts["returned"],
                "stale": False,
            })
            continue
        try:
            summary = json.loads(gate["summary_json"] or "{}")
        except ValueError:
            summary = {}
        stale = data["version"] != gate["base_version"]
        out.append({
            "doc_id": doc["id"], "doc_title": doc["title"],
            "doc_kind": doc["kind"], "kind_label": DOC_KINDS.get(doc["kind"], doc["kind"]),
            "episode_id": doc["episode_id"], "episode_title": doc["episode_title"],
            "doc_status": doc["status"], "current_version": data["version"],
            "gate_status": gate["status"] if not stale else "stale",
            "gate_status_label": ("已失配" if stale else
                                  _REVIEW_STATUS.get(gate["status"], gate["status"])),
            "gate_at": gate["created_at"],
            "findings": int(summary.get("errors", 0)) + int(summary.get("warnings", 0)),
            "errors": summary.get("errors", 0),
            "warnings": summary.get("warnings", 0),
            "signed": sum(counts.values()), "approved": counts["approved"],
            "rejected": counts["rejected"], "returned": counts["returned"],
            "stale": stale,
        })
    return out


# ---------- 音频中心（W15） ----------
class AudioInvalid(Exception):
    """音频素材输入不成立，或当前没有可用的本地音频路线。路由回 422。"""


AUDIO_KINDS = {"dialogue": "台词", "narration": "旁白",
               "music": "音乐", "sfx": "音效"}
AUDIO_STATUS = {"draft": "草稿", "queued": "排队中",
                "ready": "已就绪", "failed": "失败"}


def audio_clip_view(row) -> dict:
    try:
        timestamps = json.loads(row["timestamps_json"] or "[]")
    except ValueError:
        timestamps = []
    try:
        visemes = json.loads(row["viseme_json"] or "[]")
    except ValueError:
        visemes = []
    return {
        "id": row["id"], "project_id": row["project_id"],
        "episode_id": row["episode_id"], "episode_seq": row["episode_seq"],
        "episode_title": row["episode_title"], "shot_id": row["shot_id"],
        "shot_seq": row["shot_seq"], "kind": row["kind"],
        "kind_label": AUDIO_KINDS.get(row["kind"], row["kind"]),
        "speaker": row["speaker"], "text": row["text"],
        "status": row["status"],
        "status_label": AUDIO_STATUS.get(row["status"], row["status"]),
        "path": row["path"], "duration_ms": row["duration_ms"],
        "timestamps": timestamps, "visemes": visemes,
        "job_id": row["job_id"], "job_status": row["job_status"],
        "job_error": row["job_error"], "notes": row["notes"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def audio_sync(clip):
    """作业终态后把产物路径抄回素材。

    只在 queued 时同步一次；失败保留原因，成功就 ready。
    时长与字级时间戳只有真适配器能写，这一包不伪造。
    """
    if clip["status"] != "queued" or clip["job_id"] is None:
        return clip
    if clip["job_status"] == "succeeded":
        job = job_repo.get(clip["job_id"])
        path = None
        if job is not None:
            try:
                paths = json.loads(job["result_paths_json"] or "[]")
            except ValueError:
                paths = []
            path = paths[0] if paths else None
        clips.finish(clip["id"], status="ready", path=path,
                     duration_ms=clip["duration_ms"])
    elif clip["job_status"] in ("failed", "cancelled"):
        clips.finish(clip["id"], status="failed", path=None,
                     duration_ms=None)
    return clips.get(clip["id"]) or clip


def audio_submit(clip, user) -> dict:
    """把一条素材送进唯一队列。音频中心不另建队列，这里只建 Job。"""
    kind = clip["kind"]
    if kind == "sfx":
        raise AudioInvalid("环境音还没有可核对的自托管适配器，这一版标为实验态，不能提交")
    capability = "music" if kind == "music" else "tts"
    try:
        route = resolve_provider(capability)
    except NoRoute as exc:
        raise AudioInvalid(str(exc)) from exc
    if route["backend"] != "local":
        raise AudioInvalid("音频这一期只走本地轨；云端配音/音乐还没有接确认单")
    health = adapters.health(route["row"])
    if health["state"] != "ok":
        raise AudioInvalid(f"{route['adapter'].label} 现在不可用：{health['detail']}")
    # 时长与字级时间戳由适配器自己写；这里只冻结文本与关联
    payload = SimpleNamespace(
        project_id=clip["project_id"], episode_id=clip["episode_id"],
        shot_id=clip["shot_id"], capability=capability,
        execution_mode="local", provider_code=route["provider"],
        model_code=route["model"], prompt=clip["text"], params={},
        input_refs=[{"clip_id": clip["id"], "kind": kind,
                     "shot_id": clip["shot_id"]}],
        estimate=None, user_confirmed_cloud=False, priority=0,
        idempotency_key=None)
    try:
        job = submit_job(payload, user["id"])
    except JobInvalid as exc:
        raise AudioInvalid(str(exc)) from exc
    return audio_clip_view(clips.set_job(clip["id"], job["id"]))


# ---------- 项目看板与导出（W16） ----------
_DASH_STATUS = {
    "draft": "草稿", "active": "在产", "done": "定稿",
    "queued": "排队中", "starting": "启动中", "running": "运行中",
    "succeeded": "成功", "failed": "失败", "cancel_requested": "取消中",
    "cancelled": "已取消", "needs_attention": "待核对",
}


def project_dashboard(project_id: int) -> dict:
    data = dashboard.summary(project_id)
    return {
        "episodes": [{"status": k, "label": _DASH_STATUS.get(k, k), "count": v}
                     for k, v in sorted(data["episodes"].items())],
        "episode_total": data["episode_total"],
        "shots": data["shots"], "assets": data["assets"], "documents": data["documents"],
        "jobs": [{"status": k, "label": _DASH_STATUS.get(k, k), "count": v}
                 for k, v in sorted(data["jobs"].items())],
        "job_total": data["job_total"],
        "costs": data["costs"],
        "gates": [{"status": k, "label": {"passed": "通过", "failed": "未通过"}.get(k, k),
                   "count": v} for k, v in sorted(data["gates"].items())],
        "signs": [{"decision": k, "label": {"approved": "通过", "rejected": "驳回",
                                            "returned": "退回"}.get(k, k), "count": v}
                  for k, v in sorted(data["signs"].items())],
        "audio": [{"status": k, "label": {"draft": "草稿", "queued": "排队中",
                                          "ready": "已就绪", "failed": "失败"}.get(k, k),
                   "count": v} for k, v in sorted(data["audio"].items())],
    }


def _export_rows(project_id: int) -> list[dict]:
    rows = []
    for row in dashboard.shot_export(project_id):
        try:
            paths = json.loads(row["job_paths"] or "[]")
        except ValueError:
            paths = []
        rows.append({
            "episode_seq": row["episode_seq"], "episode_title": row["episode_title"],
            "seq": row["seq"], "summary": row["summary"], "prompt": row["prompt"],
            "duration": row["duration"], "assets": row["asset_names"] or "",
            "job_id": row["job_id"] or "", "job_status": row["job_status"] or "",
            "paths": "；".join(str(p) for p in paths),
        })
    return rows


def shots_csv(project_id: int) -> str:
    """镜头清单 CSV。带 BOM，Excel 打开中文才不是乱码。"""
    out = io.StringIO()
    out.write("\ufeff")
    writer = csv.writer(out)
    writer.writerow(["集序", "集标题", "镜号", "内容", "提示词", "时长(秒)",
                     "引用资产", "最新作业", "作业状态", "产物路径"])
    for r in _export_rows(project_id):
        writer.writerow([r["episode_seq"], r["episode_title"], r["seq"],
                         r["summary"], r["prompt"], r["duration"], r["assets"],
                         r["job_id"], r["job_status"], r["paths"]])
    return out.getvalue()


def shots_markdown(project_id: int, project_name: str) -> str:
    lines = [f"# {project_name} · 镜头清单", "",
             "| 集 | 镜 | 内容 | 提示词 | 时长 | 引用资产 | 最新作业 | 产物 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in _export_rows(project_id):
        cell = lambda v: str(v or "").replace("|", "\\|").replace("\n", " ")
        lines.append("| {e} | {s} | {m} | {p} | {d}s | {a} | {j} {st} | {path} |".format(
            e=cell(r["episode_title"]), s=r["seq"], m=cell(r["summary"]),
            p=cell(r["prompt"]), d=r["duration"], a=cell(r["assets"]),
            j=r["job_id"] and f"#{r['job_id']}" or "—", st=cell(r["job_status"]),
            path=cell(r["paths"])))
    return "\n".join(lines) + "\n"


def ensure_workspace() -> str:
    """启动时把工作区准备成可用状态，返回一句状态给健康检查显示。

    幂等：仓库已经在了就什么都不做。顺带清一次过期的锁行
    （`drop_expired` 只影响"几个人在编辑"这种计数，正确性不靠它）。
    """
    docs.drop_expired()
    gitrepo.ensure_repo()
    return gitrepo.status_line()



# ---------- Provider 路由（W06） ----------
class NoRoute(Exception):
    """这一类能力现在没有可用的执行路线。

    路由层把它转成 422/409 之类都行，关键是**绝不静默换一条轨**：
    默认走本地而本地没配，就直接报"本地没配"，不能替人切到云端——
    那会在账单上留下人没勾过的费用（规格 02 §8.3「不自动路由、不静默 fallback」）。
    """


def resolve_provider(capability: str, execution_mode: str | None = None,
                     *, confirmed_cloud: bool = False) -> dict:
    """按能力解析执行方案：默认轨，或调用方显式指定的那一轨。

    三条不肯让步的规则：
    1. 不传 execution_mode 时只认 is_default 那一条；它没配好就报错，不补位。
    2. 视频的云端路线必须同时满足"显式指定 + 人已确认付费"，
       默认轨永远走不到云端（保存配置时也不许把云端设成默认）。
    3. provider 没有登记 adapter 也报错——没有参数白名单就生成命令，
       等于让模型名去猜运行时，规格 01-4.8.2 明令禁止。
    """
    if capability not in CAPABILITIES:
        raise NoRoute(f"不认识的能力：{capability}")

    if execution_mode:
        if execution_mode not in ("local", "cloud"):
            raise NoRoute(f"执行方式只有本地/云端两种：{execution_mode}")
        row = provider_repo.slot(capability, execution_mode)
        explicit = True
    else:
        row = next((r for r in provider_repo.defaults()
                    if r["capability"] == capability), None)
        explicit = False

    if row is None:
        raise NoRoute(f"{CAPABILITIES[capability]}没有配置默认后端")
    if not explicit and row["backend"] == "cloud" and capability == "video":
        raise NoRoute("云端视频只能在镜头里逐条选择，不能作为默认")
    if capability == "video" and row["backend"] == "cloud" and not confirmed_cloud:
        raise NoRoute("云端视频要逐镜头勾选并确认按供应商计费")
    if not row["provider"].strip() or not row["model"].strip():
        raise NoRoute(f"{CAPABILITIES[capability]}的"
                      f"{'本地' if row['backend'] == 'local' else '云端'}档还没填 provider/模型")
    if not row["enabled"]:
        raise NoRoute(f"{CAPABILITIES[capability]}的"
                      f"{'本地' if row['backend'] == 'local' else '云端'}档没有启用")

    adapter = adapters.find(capability, row["backend"], row["provider"])
    if adapter is None:
        raise NoRoute(f"provider「{row['provider']}」没有登记适配器，平台不会猜它的参数")

    return {"row": row, "adapter": adapter, "capability": capability,
            "backend": row["backend"], "provider": row["provider"],
            "model": row["model"], "explicit": explicit}


# ---------- Job 提交（W07） ----------
class JobInvalid(Exception):
    """提交参数不成立（路由回 422）。和 NoRoute 分开：NoRoute 是"现在没有路线"，
    JobInvalid 是"这次请求本身不对"，前端能改的东西不一样。"""


def job_kind(capability: str, execution_mode: str) -> str:
    """adapter 请求类别。视频必须区分本地/云端——同样的 capability，
    本地跑 FastMetal 与云端调 Wan API 是两条完全不同的执行路径。"""
    return f"video_{execution_mode}" if capability == "video" else capability


def submit_job(payload, created_by: int | None) -> dict:
    """校验并写入 Job 行，返回 sqlite3.Row。**不触发执行**——执行是 worker 的事。

    三条闸门在这里一次性过完：路线（默认轨或显式槽位）、参数白名单、
    云端视频的人工确认。任何一条不过就抛 JobInvalid，绝不落一条"看起来能跑"的空作业。
    """
    capability = payload.capability
    mode = payload.execution_mode
    provider = (payload.provider_code or "").strip()
    model = (payload.model_code or "").strip()
    explicit = bool(mode or provider or model)
    adapter_version = adapters.MANIFEST_VERSION

    if provider == "mock":
        # 测试/演示专用：不进清单、不执行命令，只在 ALLOW_MOCK 打开时可用
        if not ALLOW_MOCK:
            raise JobInvalid("假适配器没有启用（FASTVIDEO_ALLOW_MOCK=1 才可用）")
        if mode not in (None, "local"):
            raise JobInvalid("假适配器只能走本地")
        if model != adapters.MOCK_MODEL:
            raise JobInvalid(f"假适配器的模型档只有 {adapters.MOCK_MODEL}")
        errors = adapters.validate_mock_params(payload.params)
        if errors:
            raise JobInvalid("；".join(errors))
        mode = "local"
        adapter_version = "mock"
    elif explicit:
        if not (mode and provider and model):
            raise JobInvalid("执行方式、供应商、模型要么都不传（走默认轨），要么三件一起给")
        row = provider_repo.slot(capability, mode)
        if row is None:
            raise JobInvalid(f"{capability} 没有 {mode} 这一轨")
        if (row["provider"], row["model"]) != (provider, model):
            raise JobInvalid(f"这次指定的 {provider}/{model} 与配置槽位 "
                             f"{row['provider']}/{row['model']} 不一致；请先改配置")
        if not row["enabled"]:
            raise JobInvalid("这个槽位没有启用")
        if capability == "video" and mode == "cloud" and not payload.user_confirmed_cloud:
            raise JobInvalid("云端视频要逐镜头确认按供应商计费")
        adapter = adapters.find(capability, mode, provider)
        if adapter is None:
            raise JobInvalid(f"provider「{provider}」没有登记适配器，平台不会猜它的参数")
        errors = adapters.validate_params(adapter, model, payload.params)
        if errors:
            raise JobInvalid("；".join(errors))
    else:
        try:
            got = resolve_provider(capability, confirmed_cloud=payload.user_confirmed_cloud)
        except NoRoute as exc:
            raise JobInvalid(str(exc)) from exc
        row = got["row"]
        provider, model, mode = row["provider"], row["model"], row["backend"]
        errors = adapters.validate_params(got["adapter"], model, payload.params)
        if errors:
            raise JobInvalid("；".join(errors))

    cost = _cost_of(payload, mode, created_by)

    return job_repo.create({
        "project_id": payload.project_id,
        "episode_id": payload.episode_id,
        "shot_id": payload.shot_id,
        "capability": capability,
        "kind": job_kind(capability, mode),
        "execution_mode": mode,
        "provider_code": provider,
        "model_code": model,
        "adapter_version": adapter_version,
        "prompt_snapshot": payload.prompt,
        "params": payload.params,
        "input_refs": payload.input_refs,
        "priority": payload.priority,
        "idempotency_key": payload.idempotency_key,
        "user_confirmed_cloud": payload.user_confirmed_cloud,
        "created_by": created_by,
        **cost,
    })


def _cost_of(payload, mode: str, created_by: int | None) -> dict:
    """把"预计成本确认单"变成 Job 的成本列。

    三条口径，任何一条不成立都不落库：
    1. 云端必须带确认单、必须勾确认。确认不是走形式——它把"按供应商计费"
       这句话和一组具体的量 × 单价绑在一起，事后能指着这一行问"当时是谁点的、按什么价"。
    2. 金额由服务端用 Decimal 算，四舍五入到整数 micros；客户端传什么总价都不采信。
    3. 本地作业不计费。带确认单说明调用方把两条轨搞混了，直接拒掉，
       比默默忽略、等到有人拿这条账去对预算更早暴露问题。
    """
    estimate = payload.estimate
    if mode == "cloud":
        if not payload.user_confirmed_cloud:
            raise JobInvalid("云端作业要先确认按供应商计费的预计成本")
        if estimate is None:
            raise JobInvalid("云端作业必须带预计成本（数量、单位、单价、币种与价格来源）")
        quantity = estimate.quantity
        cost_micros = int((quantity * Decimal(estimate.unit_price_micros))
                          .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        snapshot = {
            "quantity": str(quantity),
            "unit": estimate.unit,
            "unit_price_micros": estimate.unit_price_micros,
            "currency": estimate.currency,
            "source": estimate.source.strip(),
            "cost_micros": cost_micros,
            "formula": "quantity * unit_price_micros, half-up to integer micros",
            "confirmed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "confirmed_by": created_by,
            "billable": "pending_reconciliation",
        }
        return {"quantity": float(quantity), "unit": estimate.unit,
                "unit_price_micros": estimate.unit_price_micros,
                "cost_micros": cost_micros, "currency": estimate.currency,
                "pricing_snapshot": snapshot,
                "billable_status": "pending_reconciliation"}
    if estimate is not None:
        raise JobInvalid("本地作业不计费，不要提交预计成本；云端作业请显式指定 execution_mode")
    return {"quantity": None, "unit": None, "unit_price_micros": None,
            "cost_micros": 0, "currency": None, "pricing_snapshot": None,
            "billable_status": "not_billable"}
