"""完整兼容导出，新增业务逻辑放入对应子域。"""
from __future__ import annotations
from asset_generation_refs import parse_refs
from config import FILES_DIR
from db import transaction
from imagetypes import MAX_IMAGE_BYTES, sniff
from models import ASSET_GEN_OPERATIONS, JOB_STATUSES, JobSubmitIn, VARIANT_KINDS
from paths import resolve_within
from repositories.content import assets as asset_repo
from repositories.content import profiles as profile_repo
from repositories.content import variants as variant_repo
from repositories.jobs import jobs as job_repo
from types import SimpleNamespace
from typing import Literal
import adapters
import asset_import_recovery
import hashlib
import jobfiles
import json
import re
import shutil
import uuid
from .policy import OUTPUT_SPEC as OUTPUT_SPEC
from .policy import GUIDANCE as GUIDANCE
from .policy import TEXT_MODES as TEXT_MODES
from .policy import MODE_INSTRUCTIONS as MODE_INSTRUCTIONS
from .policy import MODES_NEED_EXCERPT as MODES_NEED_EXCERPT
from .policy import VARIANT_TYPES_BY_KIND as VARIANT_TYPES_BY_KIND
from .policy import TEXT_CAPABILITY as TEXT_CAPABILITY
from .policy import IMAGE_CAPABILITY as IMAGE_CAPABILITY
from .policy import IMAGE_OPERATION as IMAGE_OPERATION
from .policy import AssetGenInvalid as AssetGenInvalid
from .policy import ProfileInvalid as ProfileInvalid
from .policy import VariantMissing as VariantMissing
from .policy import VariantConflict as VariantConflict
from .policy import ImportUnusable as ImportUnusable
from .policy import ImportArtifactBad as ImportArtifactBad
from .policy import ImportImageRejected as ImportImageRejected
from .variants import save_profile as save_profile
from .variants import _checked_variant_type as _checked_variant_type
from .variants import create_variant as create_variant
from .variants import update_variant as update_variant
from .variants import delete_variant as delete_variant
from .prompts import build_refs as build_refs
from .prompts import _asset_context as _asset_context
from .prompts import current_fields as current_fields
from .prompts import sections_for as sections_for
from .prompts import build_text_prompt as build_text_prompt
from .prompts import parse_sections as parse_sections
from .generation import _cloud_estimate as _cloud_estimate
from .generation import generation_summary as generation_summary
from .generation import recent_generations as recent_generations
from .generation import submit_generation as submit_generation
from .generation import _derived_key as _derived_key
from .generation import _submit_job as _submit_job
from .generation import _status_label as _status_label
from .generation import generation_view as generation_view
from .images import _prepare_generated_image as _prepare_generated_image
from .images import _register_generated_image as _register_generated_image
from .images import import_generated_image as import_generated_image
from .images import remove_variant_image_tx as remove_variant_image_tx
from .images import import_generated_image_tx as import_generated_image_tx
from .images import remove_imported_file as remove_imported_file

__all__ = [
    'annotations',
    'Literal',
    'hashlib',
    'json',
    'parse_refs',
    're',
    'shutil',
    'uuid',
    'SimpleNamespace',
    'adapters',
    'asset_import_recovery',
    'jobfiles',
    'FILES_DIR',
    'transaction',
    'MAX_IMAGE_BYTES',
    'sniff',
    'ASSET_GEN_OPERATIONS',
    'JOB_STATUSES',
    'JobSubmitIn',
    'VARIANT_KINDS',
    'resolve_within',
    'asset_repo',
    'profile_repo',
    'variant_repo',
    'job_repo',
    'OUTPUT_SPEC',
    'GUIDANCE',
    'TEXT_MODES',
    'MODE_INSTRUCTIONS',
    'MODES_NEED_EXCERPT',
    'VARIANT_TYPES_BY_KIND',
    'TEXT_CAPABILITY',
    'IMAGE_CAPABILITY',
    'IMAGE_OPERATION',
    'AssetGenInvalid',
    'ProfileInvalid',
    'VariantMissing',
    'VariantConflict',
    'ImportUnusable',
    'ImportArtifactBad',
    'ImportImageRejected',
    'build_refs',
    'save_profile',
    '_checked_variant_type',
    'create_variant',
    'update_variant',
    'delete_variant',
    '_asset_context',
    'current_fields',
    'sections_for',
    'build_text_prompt',
    'parse_sections',
    '_cloud_estimate',
    'generation_summary',
    'recent_generations',
    'submit_generation',
    '_derived_key',
    '_submit_job',
    '_status_label',
    'generation_view',
    '_prepare_generated_image',
    '_register_generated_image',
    'import_generated_image',
    'remove_variant_image_tx',
    'import_generated_image_tx',
    'remove_imported_file',
]
