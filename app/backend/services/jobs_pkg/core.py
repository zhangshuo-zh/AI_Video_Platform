"""作业服务完整兼容导出，业务逻辑维护于对应子域。"""
from ..errors import JobInvalid, NoRoute
from ..providers import resolve_provider
from config import LOG_SILENCE_SECONDS, QUEUE_STALL_SECONDS, WORKER_MAX_RUNTIME_SECONDS, WORKER_ONLINE_SECONDS
from datetime import datetime
from db import get_conn
from decimal import Decimal, ROUND_HALF_UP
from models import JOB_STATUSES
from repositories.content import assets, shots, variants
from repositories.jobs import jobs as job_repo
from repositories.jobs_parts.common import JobStateConflict as _RepoStateConflict
from repositories.model_center import model_center as registration_repo
import adapters
import config
import engines
import hashlib
import jobfiles
import json
import sqlite3
from .errors import JobMissing as JobMissing
from .errors import JobStateConflict as JobStateConflict
from .source import shot_source_version as shot_source_version
from .source import shots_view as shots_view
from .source import shot_asset_ids as shot_asset_ids
from .source import shot_variant_ids as shot_variant_ids
from .source import job_source_stale as job_source_stale
from .admission import job_kind as job_kind
from .admission import _service_engine_gate as _service_engine_gate
from .admission import verify_job_links as verify_job_links
from .admission import submit_job as submit_job
from .admission import prepare_job as prepare_job, PreparedJob as PreparedJob, _intent_of as _intent_of
from .billing import _cost_of as _cost_of
from .billing import reconcile_job as reconcile_job
from .billing import mark_job_attention as mark_job_attention
from .monitor import _seconds_since as _seconds_since
from .monitor import concurrency_now as concurrency_now
from .monitor import set_concurrency as set_concurrency
from .monitor import worker_status as worker_status
from .views import job_detail_view as job_detail_view
from .views import retry_preview as retry_preview
from .views import json_loads_dict as json_loads_dict

__all__ = [
    "PreparedJob", "prepare_job", "_intent_of",
    'adapters',
    'engines',
    'hashlib',
    'jobfiles',
    'json',
    'sqlite3',
    'config',
    'LOG_SILENCE_SECONDS',
    'QUEUE_STALL_SECONDS',
    'WORKER_MAX_RUNTIME_SECONDS',
    'WORKER_ONLINE_SECONDS',
    'datetime',
    'get_conn',
    'Decimal',
    'ROUND_HALF_UP',
    'JOB_STATUSES',
    'assets',
    'shots',
    'variants',
    'job_repo',
    '_RepoStateConflict',
    'registration_repo',
    'JobInvalid',
    'NoRoute',
    'resolve_provider',
    'job_kind',
    'shot_source_version',
    'shots_view',
    'shot_asset_ids',
    'shot_variant_ids',
    'job_source_stale',
    '_service_engine_gate',
    'verify_job_links',
    'submit_job',
    '_cost_of',
    'JobMissing',
    'JobStateConflict',
    '_seconds_since',
    'concurrency_now',
    'set_concurrency',
    'worker_status',
    'job_detail_view',
    'retry_preview',
    'json_loads_dict',
    'reconcile_job',
    'mark_job_attention',
]
