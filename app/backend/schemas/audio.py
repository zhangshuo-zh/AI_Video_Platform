from pydantic import BaseModel, Field

from .enums import AudioKind


class AudioClipIn(BaseModel):
    episode_id: int | None = None
    shot_id: int | None = None
    kind: AudioKind = "dialogue"
    speaker: str = Field(default="", max_length=80)
    text: str = Field(min_length=1, max_length=4000)
    notes: str = Field(default="", max_length=500)




class AudioClipUpdateIn(BaseModel):
    kind: AudioKind
    speaker: str = Field(default="", max_length=80)
    text: str = Field(min_length=1, max_length=4000)
    notes: str = Field(default="", max_length=500)




class AudioClipOut(BaseModel):
    id: int
    project_id: int
    episode_id: int | None
    episode_seq: int = 0
    episode_title: str = ""
    shot_id: int | None
    shot_seq: int = 0
    kind: str
    kind_label: str
    speaker: str
    text: str
    status: str
    status_label: str
    path: str | None = None
    duration_ms: int | None = None
    timestamps: list = []
    visemes: list = []
    job_id: int | None = None
    job_status: str | None = None
    job_error: str | None = None
    notes: str = ""
    created_at: str
    updated_at: str
