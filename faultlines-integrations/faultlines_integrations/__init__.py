"""Faultlines analytics integrations.

Standalone module that takes a Faultlines `feature-map.json` and
enriches it with traffic + error metrics from PostHog / Sentry.
The faultlines CLI itself stays focused on git/code analysis; this
package handles all third-party analytics plumbing.

Public surface
==============

    from faultlines_integrations import enrich_feature_map
    from faultlines_integrations.posthog import PostHogProvider
    from faultlines_integrations.sentry import SentryProvider
    from faultlines_integrations.models import (
        PageMetrics, ErrorMetrics, ImpactScore,
    )

CLI
===

    faultlines-enrich path/to/feature-map.json \
        --posthog-key phx_... --posthog-project 12345 \
        --sentry-token sntrys_... --sentry-org my-org --sentry-project my-proj \
        --out feature-map.enriched.json
"""

from .enrich import enrich_feature_map
from .models import (
    AnalyticsProvider,
    ErrorEntry,
    ErrorMetrics,
    ImpactScore,
    PageMetrics,
    compute_impact_scores,
)

__all__ = [
    "AnalyticsProvider",
    "ErrorEntry",
    "ErrorMetrics",
    "ImpactScore",
    "PageMetrics",
    "compute_impact_scores",
    "enrich_feature_map",
]

__version__ = "0.1.0"
