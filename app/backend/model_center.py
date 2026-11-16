"""模型中心兼容门面；目录、槽位、标签、验证与场景各自内聚。"""
from module_facade import install
from model_center_pkg import core as _implementation
from model_center_pkg import labels, slots, verification, catalog_view, scenes, probe
from model_center_pkg.core import *  # noqa: F403
from model_center_pkg.core import __all__ as __all__

install(__name__, _implementation, (labels, slots, verification, catalog_view, scenes, probe,), {
'BASE_TAG': labels,
'IMAGE_TAG': labels,
'_loads': labels,
'derived_tags': labels,
'producible_tags': labels,
'_tags_of': labels,
'health_row': slots,
'_health_of': slots,
'_manifest_axis': slots,
'find_adapter': slots,
'_find_adapter': slots,
'_slot_current_model': slots,
'_status_of': slots,
'STATUS_LABEL': slots,
'_status_detail_of': slots,
'verified_kinds': verification,
'_verification_index': verification,
'_kinds_of': verification,
'UNINTEGRATED_ENGINES': catalog_view,
'catalog': catalog_view,
'item_for': catalog_view,
'_engine_only_row': catalog_view,
'_base_spec_fields': catalog_view,
'_registered_row': catalog_view,
'_unregistered_row': catalog_view,
'_orphan_row': catalog_view,
'stats': catalog_view,
'resolve_scene': scenes,
'_resolve': scenes,
'probe_all': probe,
})
