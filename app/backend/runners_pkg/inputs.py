"""执行器 inputs 域。"""
from __future__ import annotations

from pathlib import Path

from config import FILES_DIR, JOBS_DIR
from paths import resolve_within, within

from .protocol import RunnerUnavailable


def _job_dir_root(job_id) -> Path:
    """本作业目录（绝对、已 resolve）。"""
    return (JOBS_DIR / str(job_id)).resolve()




def _is_platform_artifact(target: Path, job) -> bool:
    """这个绝对路径是不是**本项目**的作业产物。

    分两步，第二步才查库：

    1. **形状**：落在 `data/jobs/` 底下，且第一层目录名是作业号（纯数字）。
       够挡住"界面随手填一个 /etc/passwd"，也够挡住有人往 `data/jobs/`
       旁边丢一个目录再引用它。
    2. **归属**（2026-11-09 外部审查第 2 条）：那个作业号**得属于本项目**。
       原来只有第 1 步——于是任何人提交作业时把 `image` 填成
       `data/jobs/<别人的作业号>/outputs/xx.png`，就能把**别人的**产物读进
       自己的作业。跨项目读画稿、读未发布的成片，都是真实影响。
       第 1 步的注释当初就写了"W12 的资产选择器接上之后这一档会再收一次
       （届时的判据是'这个 job_id 属于本项目'，那要查库，不归这一层）"——
       这次就是把它收掉。

    为什么这一档必须放行"别的作业目录"：产线会把上游关键帧的产物注入
    `image` 参数（`pipeline._upstream_image`），那个文件在**别的作业目录**里，
    而且同一条产线的上下游一定在同一个项目里。所以判据是"项目对不对"，
    不是"是不是本作业"。

    查不到那个作业（被删了、号是编的）一律拒——**默认拒绝**才是安全的默认。
    """
    try:
        rel = target.relative_to(JOBS_DIR.resolve())
    except ValueError:
        return False
    if not rel.parts or not rel.parts[0].isdigit():
        return False
    # 就是本作业：不必查库
    if int(rel.parts[0]) == int(job["id"]):
        return True
    # 别的作业：必须同项目
    return _job_project_of(int(rel.parts[0])) == job.get("project_id")




def _job_project_of(job_id: int) -> int | None:
    """这个作业号属于哪个项目；查不到（被删了、号是编的）返回 None。

    单独拆出来是为了能替身：`_is_platform_artifact` 的归属判据值得单测，
    但为它拉一套数据库夹具不值当（`tests/test_runners.py` 全是纯单元测试）。
    默认拒绝——查不到就是不属于。
    """
    from db import get_conn          # 延迟导入，避免模块级回环
    with get_conn() as conn:
        row = conn.execute("SELECT project_id FROM job WHERE id = ?",
                           (job_id,)).fetchone()
    return row["project_id"] if row is not None else None




def _asset_project_of(asset_id: int) -> tuple[bool, int | None]:
    """查询资产是否存在及其所属 project_id（全局资产为 None）。"""
    from db import get_conn
    with get_conn() as conn:
        row = conn.execute("SELECT project_id FROM asset WHERE id = ?",
                           (asset_id,)).fetchone()
    if row is None:
        return False, None
    return True, row["project_id"]




def _is_project_asset_file(target: Path, job) -> bool:
    """检查目标文件是否落在 data/files/<asset_id>/... 或 data/files/shots/<project_id>/... 且属于当前项目。"""
    try:
        rel = target.relative_to(FILES_DIR.resolve())
    except ValueError:
        return False
    if not rel.parts:
        return False
    if rel.parts[0] == "shots" and len(rel.parts) >= 3 and rel.parts[1].isdigit():
        return int(rel.parts[1]) == job["project_id"]
    if not rel.parts[0].isdigit():
        return False
    exists, asset_pid = _asset_project_of(int(rel.parts[0]))
    if not exists:
        return False
    return asset_pid is None or asset_pid == job["project_id"]




def _resolve_input_image(raw: str, job, *, label: str = "条件图") -> Path:
    """解析并校验证件目录/同项目作业产物/资产与镜头图库内的参考图路径。"""
    cleaned = (raw or "").strip()
    if cleaned.startswith("/api/files/"):
        cleaned = cleaned[len("/api/files/"):]
    elif cleaned.startswith("/files/"):
        cleaned = cleaned[len("/files/"):]
    elif cleaned.startswith("files/"):
        cleaned = cleaned[len("files/"):]
    image_path = Path(cleaned).expanduser()
    root = _job_dir_root(job["id"])
    files_root = FILES_DIR.resolve()
    jobs_root = JOBS_DIR.resolve()
    target: Path | None
    if image_path.is_absolute():
        target = image_path.resolve()
    else:
        candidates = [
            p for base in (root, jobs_root, files_root)
            if (p := resolve_within(base, str(image_path))) is not None
        ]
        target = next((p for p in candidates if p.is_file()), candidates[0] if candidates else None)
    if target is None or not (
        within(root, target)
        or _is_platform_artifact(target, job)
        or _is_project_asset_file(target, job)
    ):
        raise RunnerUnavailable(f"{label}路径越出作业目录")
    if not target.is_file():
        raise RunnerUnavailable(f"{label}不存在：{target.name}")
    return target
