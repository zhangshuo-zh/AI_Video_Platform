"""分镜引用、字段整理与视频帧提取的业务逻辑。"""
import json
import shutil
import subprocess
import uuid

import jobfiles
from composition import validators
from config import FFMPEG_BIN, FILES_DIR
from models import ShotExtractFrameIn, ShotExtractFrameOut
from repositories.content import assets, shots
from repositories.content import variants as variant_repo
from repositories.jobs import jobs as job_repo
from .jobs import shot_variant_ids

MAX_ALIASES = 10
MAX_ALIAS_LEN = 60
MAX_SHOT_ASSETS = 40


class ContentOperationError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail

def _clean_aliases(raw: list[str]) -> list[str]:
    """别名去空白、去重、限量。规则与 clean_tags 同形，但长度上限不同，
    所以不复用它——别名要跟名字一样长，标签只要 20 字。"""
    seen: list[str] = []
    for item in raw:
        alias = item.strip()[:MAX_ALIAS_LEN]
        if alias and alias not in seen:
            seen.append(alias)
    return seen[:MAX_ALIASES]


def _checked_asset_ids(project_id: int, raw: list[int]) -> list[int]:
    """把前端交上来的引用收敛成服务端认可的一串 id。

    三条都要在写入口拦：
    1. 去重并保序——同一资产点两次不该在 chips 里出现两遍；
    2. 必须是本项目资产或全局资产——别家项目的 id 一律拒，否则会出现
       "看不见的引用"（删除保护算得到、界面上却找不到）；
    3. 镜头编辑与新建走同一套，不然从创建口仍然能塞进脏 id。
    """
    ids: list[int] = []
    for value in raw:
        try:
            asset_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ContentOperationError(status_code=422, detail="资产 id 必须是整数") from exc
        if asset_id <= 0:
            raise ContentOperationError(status_code=422, detail="资产 id 必须是正整数")
        if asset_id not in ids:
            ids.append(asset_id)
    if len(ids) > MAX_SHOT_ASSETS:
        raise ContentOperationError(status_code=422,
                            detail=f"一镜最多引用 {MAX_SHOT_ASSETS} 个资产")
    if ids and assets.usable_ids(project_id, ids) != set(ids):
        raise ContentOperationError(
            status_code=422,
            detail="引用了不存在、不属于这个项目，或不是全局资产的 id")
    return ids


def _checked_variant_ids(project_id: int, raw: list[int],
                         asset_ids: list[int]) -> list[int]:
    """把镜头引用的形态收敛成一串服务端认可的 id，并**顺带补齐父资产引用**。

    三条纪律：
    1. 形态必须可见（父资产是本项目的或全局池的）——`variants.usable_ids` 判的就是这个；
    2. 引了形态就必须引它的父资产。这里不是"拒绝"而是**自动把父资产加进 asset_ids**：
       只勾"雨夜外套"没勾"掌灯人"的人并非少数，而漏掉父资产会让删除保护、
       引用计数、资产库那格"关联 N 镜"三处同时看不见这条引用——那是数据损坏，
       不是用户的错。补上之后两个清单永远一致，界面上也不用多解释一句。
    3. 去重保序，规则与 `_checked_asset_ids` 一样。
    """
    ids: list[int] = []
    for value in raw:
        try:
            variant_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ContentOperationError(status_code=422, detail="形态 id 必须是整数") from exc
        if variant_id <= 0:
            raise ContentOperationError(status_code=422, detail="形态 id 必须是正整数")
        if variant_id not in ids:
            ids.append(variant_id)
    if len(ids) > MAX_SHOT_ASSETS:
        raise ContentOperationError(status_code=422,
                            detail=f"一镜最多引用 {MAX_SHOT_ASSETS} 个形态")
    if ids and variant_repo.usable_ids(project_id, ids) != set(ids):
        raise ContentOperationError(
            status_code=422,
            detail="引用了不存在、或父资产不属于这个项目也不是全局资产的形态 id")
    # 一次查齐父资产：`parents_of` 本来就收列表，逐 id 调等于把"一镜挂 N 个形态"
    # 变成 N 趟库——而这条路径是分镜表每一镜保存时都要走的。
    parents = variant_repo.parents_of(ids) if ids else {}
    for variant_id in ids:
        parent = parents.get(variant_id)
        if parent is not None and parent not in asset_ids:
            asset_ids.append(parent)
    return ids


def _variant_labels(rows: list[dict]) -> dict[int, dict[int, str]]:
    """一批镜头行 → {shot_id: {形态 id: 「资产名 · 形态名」}}，一次读齐。

    放在服务端而不是让分镜表逐个 id 回查：一集十几镜、每镜两三个形态，
    浏览器去查就是几十趟请求，而且它还得先知道形态属于哪条资产才拼得出名字。
    """
    all_ids: set[int] = set()
    per_shot: dict[int, list[int]] = {}
    for row in rows:
        ids = sorted(shot_variant_ids(row))
        per_shot[int(row["id"])] = ids
        all_ids |= set(ids)
    if not all_ids:
        return {}
    got = variant_repo.get_many(sorted(all_ids))
    owners = assets.get_many({row["asset_id"] for row in got.values()})
    out: dict[int, dict[int, str]] = {}
    for shot_id, ids in per_shot.items():
        labels = {}
        for variant_id in ids:
            variant = got.get(variant_id)
            if variant is None:
                # 形态被删了但镜头还指着它：标签留 id，界面上看得见"这里断过"，
                # 比凭空消失一条引用好查（同 `assetChip` 对悬空资产 id 的处理）。
                labels[variant_id] = f"#{variant_id}（形态已删）"
                continue
            owner = owners.get(variant["asset_id"])
            labels[variant_id] = (f"{owner['name']} · {variant['name']}" if owner
                                  else f"{variant['name']}")
        out[shot_id] = labels
    return out


def _shot_form_fields(payload) -> dict:
    """0037 的镜头设定五栏：去空格后原样交给仓储。

    为什么单独一个函数而不是在两个端点各写五遍：新建与编辑必须认同一批字段，
    漏一栏就会出现"编辑时能填、新建时填了不存"。
    这五栏**不进 `shot_source_version()` 的指纹**（见 services 里那段说明）：
    今天没有执行器读它们，把它们算进源版本会让"补填景别"变成"全片产物过期"。
    """
    return {"shot_size": payload.shot_size.strip(),
            "camera_move": payload.camera_move.strip(),
            "aspect": payload.aspect.strip(),
            "style": payload.style.strip(),
            "negative_prompt": payload.negative_prompt.strip(),
            "trim_in_ms": int(payload.trim_in_ms or 0),
            "trim_out_ms": int(payload.trim_out_ms or 0),
            "transition": payload.transition,
            "first_frame_image": payload.first_frame_image.strip(),
            "last_frame_image": payload.last_frame_image.strip()}


def extract_shot_frame(shot_id: int, payload: ShotExtractFrameIn | None, project_id: int) -> ShotExtractFrameOut:
    """从镜头最新成功视频截取尾帧/首帧存为 PNG 参考图，并可一键填入下一镜首帧槽位。"""
    shot = shots.get(shot_id)
    if shot is None:
        raise ContentOperationError(status_code=404, detail="镜头不存在")
    opts = payload or ShotExtractFrameIn()
    video_by_shot = job_repo.latest_succeeded_videos_by_shot(int(shot["episode_id"]))
    video_job = video_by_shot.get(int(shot_id))
    if video_job is None:
        raise ContentOperationError(status_code=422, detail="该镜头还没有已完成的视频作业，无法截帧")
    try:
        rpaths = json.loads(video_job["result_paths_json"] or "[]")
    except ValueError:
        rpaths = []
    rel = str(rpaths[0]) if rpaths else ""
    target = jobfiles.resolve_artifact(rel) if rel else None
    if target is None:
        raise ContentOperationError(status_code=422, detail="视频产物文件不在磁盘上")
    ffmpeg = shutil.which(FFMPEG_BIN)
    if not ffmpeg:
        raise ContentOperationError(status_code=422, detail=f"找不到 ffmpeg（{FFMPEG_BIN}）")
    try:
        info = validators.inspect(target)
    except validators.MediaInvalid as exc:
        raise ContentOperationError(status_code=422, detail=f"视频文件不可读：{exc.message}") from exc
    dur_ms = int(info.get("duration_ms") or 0)
    if opts.offset_ms is not None:
        seek_s = min(max(0.0, opts.offset_ms / 1000.0), max(0.0, (dur_ms - 80) / 1000.0))
    elif opts.position == "first":
        seek_s = 0.0
    else:
        seek_s = max(0.0, (dur_ms - 120) / 1000.0)

    out_dir = FILES_DIR / "shots" / str(project_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.png"
    out_path = out_dir / name

    def _run_grab(ss_val: float) -> bool:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-y", "-ss", f"{ss_val:.3f}", "-i", str(target),
             "-frames:v", "1", "-q:v", "2", str(out_path)],
            capture_output=True, text=True, timeout=30, shell=False,
        )
        return proc.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0

    if not _run_grab(seek_s) and not _run_grab(0.0):
        raise ContentOperationError(status_code=422, detail="FFmpeg 截帧失败，请检查视频编码是否完整")

    frame_path = f"shots/{project_id}/{name}"
    frame_url = f"/files/{frame_path}"
    next_shot = shots.next_in_episode(int(shot["episode_id"]), int(shot["seq"]))
    applied_next = False
    if opts.apply_to_next_shot and next_shot is not None:
        shots.set_frame_image(int(next_shot["id"]), first_frame_image=frame_path)
        applied_next = True
    applied_self = False
    if opts.apply_to_self_last_frame:
        shots.set_frame_image(shot_id, last_frame_image=frame_path)
        applied_self = True
    return ShotExtractFrameOut(
        shot_id=shot_id,
        next_shot_id=int(next_shot["id"]) if next_shot is not None else None,
        frame_path=frame_path,
        frame_url=frame_url,
        applied_to_next_shot=applied_next,
        applied_to_self_last_frame=applied_self,
    )
