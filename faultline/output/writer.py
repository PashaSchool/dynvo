import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from faultline.models.types import FeatureMap

logger = logging.getLogger(__name__)

# S19.5 — top-N for the optional ``compact_features`` derived view.
# Determined by sweep on the 26-repo S17 corpus: F1 peaks at N=12-15
# (see EVAL_REPORT.md S19.5 retro). 15 chosen as the sweet spot — F1
# within 0.3pp of the N=12 peak but with +4pp coverage for better UX.
DEFAULT_COMPACT_TOP_N = 15


def write_feature_map(
    feature_map: FeatureMap,
    output_path: str | None = None,
    *,
    compact_top_n: int | None = DEFAULT_COMPACT_TOP_N,
) -> str:
    """
    Writes the feature map to a JSON file.

    When output_path is not specified, generates a unique filename using the
    repository name and current UTC timestamp so each run produces a new file
    and history is preserved:
        .faultline/feature-map-{repo-slug}-{YYYYMMDD-HHMMSS}.json

    Args:
      compact_top_n: when set, also computes a derived ``compact_features``
        field — top-N features by path count, with cut features merged
        into nearest similar via reattribution. Default 15. Set to None
        to skip (raw output only).

    Returns the path where the file was saved.
    """
    if output_path is not None:
        path = Path(output_path)
    else:
        slug = _repo_slug(feature_map.repo_path)
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = Path.home() / ".faultline" / f"feature-map-{slug}-{ts}.json"

    path.parent.mkdir(parents=True, exist_ok=True)
    data = feature_map.model_dump(mode="json")

    # S19.5 — Variant B: dual output. Append derived `compact_features`
    # alongside raw `features`. Eval / landing read compact_features
    # when available; existing dashboard keeps reading `features`.
    if compact_top_n is not None and data.get("features"):
        try:
            from faultline.analyzer.feature_compaction import reattribute
            compact_feats, stats = reattribute(
                data["features"], top_n=compact_top_n,
            )
            data["compact_features"] = compact_feats
            data["compact_stats"] = stats
            logger.info(
                "writer: compact_features built — kept=%d, merged=%d, hard_dropped=%d",
                stats["kept"], stats["merged"], stats["hard_dropped"],
            )
        except Exception as exc:  # noqa: BLE001 — opportunistic
            logger.warning("writer: compact_features failed (%s)", exc)

    path.write_text(json.dumps(data, indent=2, default=str))

    return str(path)


def _repo_slug(repo_path: str) -> str:
    """Converts a repo path to a safe filename component."""
    name = Path(repo_path).name
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "repo"
