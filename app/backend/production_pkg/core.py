"""完整兼容导出，新增业务逻辑放入对应子域。"""
from __future__ import annotations
from pathlib import Path
from repositories.content import episodes, shots
from repositories.jobs import jobs as job_repo
from repositories.production import production
from repositories.reviews import reviews
import adapters
import diskplan
import error_catalog
import subprocess
from .state import PENDING as PENDING
from .state import DOING as DOING
from .state import NEEDS as NEEDS
from .state import DONE as DONE
from .state import FAILED as FAILED
from .state import PUBLIC_STATUSES as PUBLIC_STATUSES
from .state import STAGES as STAGES
from .state import _stage as _stage
from .audio import _audio_wired as _audio_wired
from .audio import _audio_stage_note as _audio_stage_note
from .audio import audio_status_view as audio_status_view
from .board import episode_board as episode_board
from .board import next_tasks as next_tasks
from .board import project_overview as project_overview
from .stats import _machine_total_gb as _machine_total_gb
from .stats import _MACHINE_TOTAL_GB as _MACHINE_TOTAL_GB
from .stats import _memory_readout as _memory_readout
from .stats import _team_readout as _team_readout
from .stats import project_stats as project_stats
from .workbench import _job_rows as _job_rows
from .workbench import shot_workbench as shot_workbench
from .impact import referencing_shots as referencing_shots
from .impact import asset_impact as asset_impact

__all__ = [
    'annotations',
    'subprocess',
    'Path',
    'adapters',
    'diskplan',
    'error_catalog',
    'episodes',
    'shots',
    'job_repo',
    'production',
    'reviews',
    'PENDING',
    'DOING',
    'NEEDS',
    'DONE',
    'FAILED',
    'PUBLIC_STATUSES',
    'STAGES',
    '_stage',
    '_audio_wired',
    '_audio_stage_note',
    'audio_status_view',
    'episode_board',
    'next_tasks',
    'project_overview',
    '_machine_total_gb',
    '_MACHINE_TOTAL_GB',
    '_memory_readout',
    '_team_readout',
    'project_stats',
    '_job_rows',
    'shot_workbench',
    'referencing_shots',
    'asset_impact',
]
