"""G2 shim: lazy-dispatch helpers sanctioned for concrete profiles.

Concrete profiles may import only stdlib, profiles.base, profiles._*
helpers, and pipeline_v2.extractors.* (tests/test_profiles_import_lint.py).
The dispatch-resolver helpers live in pipeline_v2.lazy_imports; this
private-tier module re-exports the names profiles are allowed to reach.
"""

from faultline.pipeline_v2.lazy_imports import (
    dispatch_resolver_enabled,
    ts_lazy_binding_specs,
)

__all__ = ["dispatch_resolver_enabled", "ts_lazy_binding_specs"]
