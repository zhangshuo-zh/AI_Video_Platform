from pydantic import BaseModel, Field


# ---------- 成片中心（阶段 D） ----------

class CompositionIn(BaseModel):
    episode_id: int
    width: int | None = None
    height: int | None = None
    fps: int | None = None
    subtitle_path: str | None = Field(default=None, max_length=300)
    idempotency_key: str | None = Field(default=None, max_length=120)




class CompositionOut(BaseModel):
    id: int
    project_id: int
    episode_id: int
    episode_seq: int = 0
    episode_title: str = ""
    version: int
    job_id: int | None = None
    status: str
    status_label: str
    job_status: str = ""
    error_code: str = ""
    error_message: str = ""
    params: dict = {}
    shot_count: int = 0
    audio_count: int = 0
    total_duration_ms: int = 0
    output_path: str = ""
    sha256: str = ""
    bytes: int = 0
    duration_ms: int = 0
    width: int | None = None
    height: int | None = None
    created_at: str
    updated_at: str
    artifact_url: str = ""
    timeline: dict = {}
    audio: list = []
    subtitles: list = []




class SubtitleFileOut(BaseModel):
    """一份可用的字幕文件＋这台机器能不能烧它。

    `subtitle_supported` 跟着列表一起回，是因为界面上"有没有字幕文件"和
    "这台机器的 FFmpeg 烧不烧得动"必须一起决定输入框是可选还是禁用——
    只给文件不给能力，人就能选完再吃一句 422。
    """
    items: list = []
    subtitle_supported: bool = False
    ffmpeg_hint: str = ""




class SeasonIn(BaseModel):
    """发起一季批量生产。

    不给 episode_ids＝按集的 seq 排全季；给了就只排这些集（顺序照给）。
    `max_active_runs` 是这一季同时推进几集的上限，建季时冻结。
    """
    template_id: int
    episode_ids: list[int] | None = None
    name: str = Field(default="", max_length=80)
    max_active_runs: int | None = Field(default=None, ge=1, le=64)
    idempotency_key: str | None = Field(default=None, max_length=120)




class SeasonCancelIn(BaseModel):
    reason: str = Field(default="", max_length=200)




class SeasonOut(BaseModel):
    id: int
    project_id: int
    project_name: str = ""
    template_id: int
    template_name: str = ""
    template_version: int = 0
    name: str = ""
    max_active_runs: int = 1
    status: str
    status_label: str
    paused: bool = False
    cancelled: bool = False
    cancel_reason: str = ""
    finished: bool = False
    counts: dict = {}
    progress: dict = {}
    cost: dict = {}
    items: list[dict] = []
    retry: dict | None = None
    created_by: int | None = None
    created_by_name: str = ""
    started_at: str = ""
    finished_at: str = ""
    created_at: str
    updated_at: str
