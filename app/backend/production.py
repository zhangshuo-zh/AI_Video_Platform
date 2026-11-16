"""production 向后兼容门面。实现维护于 production_pkg。"""
from module_facade import install
from production_pkg import core as _implementation
from production_pkg import state, audio, board, stats, workbench, impact
from production_pkg.core import *  # noqa: F403
from production_pkg.core import __all__ as __all__

install(__name__, _implementation, (state, audio, board, stats, workbench, impact,), {
    'PENDING': state,
    'DOING': state,
    'NEEDS': state,
    'DONE': state,
    'FAILED': state,
    'PUBLIC_STATUSES': state,
    'STAGES': state,
    '_stage': state,
    '_audio_wired': audio,
    '_audio_stage_note': audio,
    'audio_status_view': audio,
    'episode_board': board,
    'next_tasks': board,
    'project_overview': board,
    '_machine_total_gb': stats,
    '_MACHINE_TOTAL_GB': stats,
    '_memory_readout': stats,
    '_team_readout': stats,
    'project_stats': stats,
    '_job_rows': workbench,
    'shot_workbench': workbench,
    'referencing_shots': impact,
    'asset_impact': impact,
})
