"""唯一作业提交服务的兼容门面，业务实现住在 services/jobs_pkg。"""
from module_facade import install
from .jobs_pkg import core as _implementation
from .jobs_pkg import errors, source, admission, billing, monitor, views
from .jobs_pkg.core import *  # noqa: F403
from .jobs_pkg.core import __all__ as __all__

install(__name__, _implementation, (errors, source, admission, billing, monitor, views,), {
    'JobMissing': errors,
    'JobStateConflict': errors,
    'shot_source_version': source,
    'shots_view': source,
    'shot_asset_ids': source,
    'shot_variant_ids': source,
    'job_source_stale': source,
    'job_kind': admission,
    '_service_engine_gate': admission,
    'verify_job_links': admission,
    'submit_job': admission,
    'prepare_job': admission,
    'PreparedJob': admission,
    '_intent_of': admission,
    '_cost_of': billing,
    'job_cost_summary': billing,
    'job_page': views,
    'JobFilters': views,
    'reconcile_job': billing,
    'mark_job_attention': billing,
    '_seconds_since': monitor,
    'concurrency_now': monitor,
    'set_concurrency': monitor,
    'worker_status': monitor,
    'job_detail_view': views,
    'retry_preview': views,
    'json_loads_dict': views,
})
