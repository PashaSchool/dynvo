"""Stage 8.9.6 — deterministic domain-dir member attribution.

The residual class Stage 8.9.5 CANNOT touch: files that are **member-only**
(present in a workspace anchor's ``member_files`` ledger but OWNED by no
feature's ``paths``) sitting under a domain-organised container subtree.
Canonical case (infisical, 2026-07-02): ``frontend/src/hooks/api/<domain>/*``
— 710 unowned data-hook files hang on the ``frontend-v2`` anchor's ledger and
keep it the repo's blob (22.8% member-share), while the backend split already
minted a feature for almost every ``<domain>`` (``appConnections`` →
``app-connection``, ``certificates`` → ``certificate`` …). The split stage is
powerless here because its fan-out walks OWNED paths only — there is nothing
owned to move.

The fix is pure vocabulary corroboration (the "cross-vocab" lever noted in the
framework-awareness mission): when a member-only file lives under a
``components``/``hooks`` container and its domain directory name **uniquely**
matches an existing developer feature (exact slug first, then crude-singular
slug), the file IS that feature's code — transfer the membership AND claim
ownership (the file was unowned, so the claim can only add signal; nothing is
ever stolen from another owner).

Safety rails (all structural, no tuned numbers — rule-no-magic-tuning):
  * only member-only files move (a path in ANY feature's ``paths`` is skipped);
  * the domain segment must not be a generic container/grouping token
    (``api``/``ui``/``common``/…) — those would mega-match unrelated features;
  * the name match must be UNIQUE at its tier (ambiguous singular collisions
    are skipped, exact beats singular);
  * a feature never transfers to itself; transfers are deterministic
    (features processed in input order, segments left-to-right).

Deterministic, $0 LLM. Default OFF (opt-in):
``FAULTLINE_STAGE_8_9_6_DOMAIN_ATTRIBUTION=1``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from faultline.pipeline_v2.stage_8_9_5_llm_component_split import _COMPONENT_SEGS

if TYPE_CHECKING:
    from faultline.models.types import Feature

# Generic grouping/container tokens that must never be read as a product
# domain (they would match same-named infra features and mega-transfer).
_GENERIC_DOMAIN_SKIP = frozenset({
    "api", "ui", "ux", "common", "shared", "utils", "util", "helpers",
    "lib", "libs", "core", "components", "component", "hooks", "hook",
    "internal", "misc", "types", "styles", "assets", "base", "v1", "v2",
    "v3", "v4", "src", "app", "apps", "packages", "modules", "widgets",
    "primitives", "layouts", "icons", "forms",
})
# How many segments below the container may hold the domain dir — the domain
# sits either directly under the container (``components/<domain>``) or under
# ONE grouping dir (``hooks/api/<domain>``), mirroring the 8.9.5 v2 descent.
_MAX_DESCENT = 2


def _is_enabled() -> bool:
    """Default OFF (opt-in) — ``FAULTLINE_STAGE_8_9_6_DOMAIN_ATTRIBUTION=1``."""
    return (
        os.environ.get("FAULTLINE_STAGE_8_9_6_DOMAIN_ATTRIBUTION", "0") != "0"
    )


def _norm(name: str) -> str:
    """camelCase/space/slash → kebab slug (exact tier)."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name or "")
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _singular(slug: str) -> str:
    """Crude per-token singularisation of a kebab slug (``app-connections``
    → ``app-connection``) — same convention as the 6.7d/uf-scorer tokenisers."""
    return "-".join(
        t[:-1] if len(t) > 3 and t.endswith("s") else t
        for t in slug.split("-")
    )


@dataclass
class DomainAttributionResult:
    """Per-scan telemetry."""

    enabled: bool = False
    sources_examined: int = 0
    files_transferred: int = 0
    targets_enriched: int = 0
    ambiguous_skipped: int = 0
    sample: list[dict[str, Any]] = field(default_factory=list)

    def as_telemetry(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "sources_examined": self.sources_examined,
            "files_transferred": self.files_transferred,
            "targets_enriched": self.targets_enriched,
            "ambiguous_skipped": self.ambiguous_skipped,
            "sample": list(self.sample[:20]),
        }


def _member_path(mf: Any) -> str | None:
    if isinstance(mf, dict):
        return mf.get("path")
    return getattr(mf, "path", None)


def _build_name_index(
    devs: list["Feature"],
) -> tuple[dict[str, list["Feature"]], dict[str, list["Feature"]]]:
    """(exact slug → features, singular slug → features). Lists keep every
    collision so the caller can enforce the uniqueness rail."""
    exact: dict[str, list["Feature"]] = {}
    singular: dict[str, list["Feature"]] = {}
    for f in devs:
        seen_keys: set[str] = set()
        for raw in {f.name, getattr(f, "display_name", None)}:
            if not raw:
                continue
            e = _norm(raw)
            if e and e not in seen_keys:
                seen_keys.add(e)
                exact.setdefault(e, []).append(f)
            s = _singular(e)
            if s and s not in seen_keys:
                seen_keys.add(s)
                singular.setdefault(s, []).append(f)
    return exact, singular


def _match_domain(
    path: str,
    exact: dict[str, list["Feature"]],
    singular: dict[str, list["Feature"]],
    result: DomainAttributionResult,
) -> "Feature | None":
    """The feature a member-only *path*'s domain dir uniquely names, else
    ``None``. Walks segments after the first container segment, up to
    ``_MAX_DESCENT`` levels deep; exact tier beats singular tier."""
    segs = path.split("/")
    comp_idx = next(
        (i for i in range(len(segs) - 1) if segs[i].lower() in _COMPONENT_SEGS),
        None,
    )
    if comp_idx is None:
        return None
    for d in range(comp_idx + 1, min(comp_idx + 1 + _MAX_DESCENT, len(segs) - 1)):
        dom = segs[d]
        dom_slug = _norm(dom)
        if not dom_slug or dom_slug in _GENERIC_DOMAIN_SKIP:
            continue
        for tier in (exact, singular):
            hits = tier.get(dom_slug) or tier.get(_singular(dom_slug)) or []
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                result.ambiguous_skipped += 1
                return None
    return None


def attribute_domain_members(
    features: list["Feature"],
) -> DomainAttributionResult:
    """Transfer member-only domain-dir files from their holder to the
    same-named developer feature. Mutates *features* in place; returns
    telemetry. No-op when disabled — safe to wire unconditionally."""
    result = DomainAttributionResult(enabled=_is_enabled())
    if not result.enabled:
        return result

    devs = [
        f for f in features
        if getattr(f, "layer", "developer") == "developer"
    ]
    if not devs:
        return result

    owned_all = {p for f in devs for p in (getattr(f, "paths", None) or [])}
    exact, singular = _build_name_index(devs)
    enriched: set[int] = set()

    for source in devs:
        mfs = getattr(source, "member_files", None) or []
        if not mfs:
            continue
        result.sources_examined += 1
        keep: list[Any] = []
        moved_here = 0
        for mf in mfs:
            p = _member_path(mf)
            # Rail 1: never touch a file some feature actually OWNS.
            if not p or p in owned_all:
                keep.append(mf)
                continue
            target = _match_domain(p, exact, singular, result)
            if target is None or target is source:
                keep.append(mf)
                continue
            # Transfer: the target claims ownership (path was unowned) and
            # inherits the ledger entry; the source's ledger shrinks.
            target.member_files = (getattr(target, "member_files", None) or [])
            target.member_files.append(mf)
            target.paths.append(p)
            owned_all.add(p)
            enriched.add(id(target))
            result.files_transferred += 1
            moved_here += 1
        if moved_here:
            source.member_files = keep
            if len(result.sample) < 20:
                result.sample.append(
                    {"source": source.name, "moved": moved_here},
                )

    result.targets_enriched = len(enriched)
    return result


__all__ = ["DomainAttributionResult", "attribute_domain_members"]
