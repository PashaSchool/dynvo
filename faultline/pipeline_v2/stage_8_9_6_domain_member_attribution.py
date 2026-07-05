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
# The trailing row is the UI-widget-container class (same family as the
# existing "widgets"/"primitives"/"forms" members): dialog/modal/overlay
# dirs are presentational plumbing, never a product domain.
_GENERIC_DOMAIN_SKIP = frozenset({
    "api", "ui", "ux", "common", "shared", "utils", "util", "helpers",
    "lib", "libs", "core", "components", "component", "hooks", "hook",
    "internal", "misc", "types", "styles", "assets", "base", "v1", "v2",
    "v3", "v4", "src", "app", "apps", "packages", "modules", "widgets",
    "primitives", "layouts", "icons", "forms",
    "dialog", "dialogs", "modal", "modals", "drawer", "drawers",
    "popover", "popovers", "overlay", "overlays",
})
# Domain-organised containers 8.9.6 walks. Extends the 8.9.5 fan-out set
# (kept FROZEN — the split stage's behaviour must not change) with the
# React feature-folder conventions: ``features/<domain>`` (feature-sliced
# design) and ``modules/<domain>`` — both are author-declared product
# domains, exactly the owner signal this stage corroborates. Universal
# convention, not repo tuning (rule-no-repo-specific-paths).
_CONTAINER_SEGS = frozenset(
    _COMPONENT_SEGS | {"features", "feature", "modules", "module"}
)
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
        # ORDERED iteration (name first, then display_name) + one seen-set PER
        # TIER: a shared set let a name's singular reduction shadow the
        # display_name's literal slug out of the exact index, and set-of-str
        # iteration order is hash-randomised across processes → transfer
        # decisions could differ between runs (audit #3, 2026-07-02).
        seen_exact: set[str] = set()
        seen_singular: set[str] = set()
        for raw in (f.name, getattr(f, "display_name", None)):
            if not raw:
                continue
            e = _norm(raw)
            if e and e not in seen_exact:
                seen_exact.add(e)
                exact.setdefault(e, []).append(f)
            s = _singular(e)
            if s and s not in seen_singular:
                seen_singular.add(s)
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
        (i for i in range(len(segs) - 1) if segs[i].lower() in _CONTAINER_SEGS),
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
            # Transfer: the target claims OWNERSHIP (path was unowned), so
            # mint a fresh owning MemberFile (role="anchor", primary=True —
            # the _make_subfeature pattern) rather than carrying the stale
            # shared-role entry: _owned_paths()/owned_max only count
            # primary/anchor/owner rows once a ledger exists, so a shared-role
            # append would leave paths and member_files in a split-brain
            # (audit #1, 2026-07-02). The source's ledger shrinks.
            from faultline.models.types import MemberFile  # local: import cycle
            target.member_files = (getattr(target, "member_files", None) or [])
            target.member_files.append(MemberFile(
                path=p, role="anchor", confidence=1.0, primary=True,
                evidence=f"domain-dir attribution from '{source.name}'",
            ))
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


# ── Iteration-5: mega-anchor service-domain carve-out ────────────────────────
# A workspace/infra anchor (Soc0 ``backend`` — 330 owned paths, 165 flows,
# pf=shared-platform) swallows whole product subsystems as FLOWS whose files
# sit under ``backend/services/<domain>/`` (investigation_playbook, edr,
# threat_hunts …). Because those flows are owned by the shared anchor, a
# journey built from them ("Manage investigation playbooks") is stuck on the
# shared/platform bucket (validator I10) — the 6.7d shared-UF reassignment
# finds no non-shared owner. The carve-out lifts each service-domain subtree
# into its OWN developer feature (name = the domain) that carries those flows,
# so 6.7d maps it to a real capability and the journey resettles.
#
# Structural rails (rule-no-magic-tuning / rule-no-repo-specific-paths):
#   * SOURCE must be a mega-anchor — a workspace anchor OR a dev whose flows
#     span >= 2 distinct carve-eligible service domains (a multi-domain flow
#     container, not a focused service);
#   * a flow joins a domain only when the MAJORITY of its files sit under one
#     ``services|service/<domain>/`` subtree (mixed flows stay with the anchor);
#   * the domain needs >= 3 distinct owned files (the operator floor — a
#     subsystem dir, not a stray helper) and a non-generic, non-infra name;
#   * a domain that names the source dev itself is never carved (no self-carve);
#   * name collisions with existing features skip that domain (conservative).
# Rides the 8.9.6 flag; independently killable via
# ``FAULTLINE_STAGE_8_9_6_SERVICE_CARVE=0``. Deterministic, $0 LLM.
# Domain-organised containers the carve lifts subsystems out of: the backend
# ``services/<domain>/`` convention AND the React feature-folder conventions
# (``features/<domain>/`` / ``modules/<domain>/``) — both are author-declared
# product domains a mega-anchor (backend / <app>-frontend) can swallow whole.
# Deliberately EXCLUDES the generic UI-layer containers (components / hooks):
# those are presentational plumbing, not product subsystems.
_SERVICE_CONTAINER_SEGS = frozenset({
    "services", "service", "features", "feature", "modules", "module",
})
# Universal infra-domain conventions (never product subsystems). Mirrors the
# 6.7d structure-leak / mock-scaffolding classes — not a repo list.
_INFRA_DOMAIN_SKIP = frozenset({
    "mock", "mocks", "mock-data", "fixture", "fixtures", "seed", "seeds",
    "test", "tests", "testing", "migration", "migrations", "scripts",
})
_CARVE_MIN_FILES = 3  # subsystem floor; validated tiny/medium/large in tests


def _carve_enabled() -> bool:
    """Rides the 8.9.6 opt-in flag; killable via
    ``FAULTLINE_STAGE_8_9_6_SERVICE_CARVE=0``."""
    return (
        _is_enabled()
        and os.environ.get("FAULTLINE_STAGE_8_9_6_SERVICE_CARVE", "1") != "0"
    )


@dataclass
class ServiceCarveResult:
    """Per-scan carve-out telemetry."""

    enabled: bool = False
    anchors_examined: int = 0
    anchors_carved: int = 0
    domains_carved: int = 0
    flows_moved: int = 0
    files_claimed: int = 0
    collisions_skipped: int = 0
    sample: list[dict[str, Any]] = field(default_factory=list)

    def as_telemetry(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "anchors_examined": self.anchors_examined,
            "anchors_carved": self.anchors_carved,
            "domains_carved": self.domains_carved,
            "flows_moved": self.flows_moved,
            "files_claimed": self.files_claimed,
            "collisions_skipped": self.collisions_skipped,
            "sample": list(self.sample[:20]),
        }


def _flow_files(flow: Any) -> list[str]:
    out: list[str] = []
    ep = getattr(flow, "entry_point_file", None)
    if ep:
        out.append(ep)
    for p in getattr(flow, "paths", None) or []:
        if isinstance(p, str):
            out.append(p)
    return out


def _service_domain_of(path: str) -> str | None:
    """The ``services|service/<domain>/`` domain a file sits under (a file must
    remain BELOW the domain dir — ``services/<domain>/<file>``), else None."""
    segs = path.split("/")
    for i in range(len(segs) - 2):
        if segs[i].lower() in _SERVICE_CONTAINER_SEGS:
            dom = segs[i + 1]
            return dom if dom else None
    return None


def _flow_domain(flow: Any) -> tuple[str, list[str]] | None:
    """(domain, service-files) for the service subsystem a flow belongs to.

    Entry-point attribution: a flow whose ENTRY file sits under a
    ``services/<domain>/`` subtree IS that subsystem's flow — even when it also
    touches shared infra (``database.py`` / ``models/``), which must not dilute
    the decision (the Soc0 playbook flows enter at
    ``services/investigation_playbook/*`` but each also reads a shared model).
    When the entry is not under a service domain, fall back to the plurality
    service domain among the flow's files. Returns the domain plus the flow's
    files that live under it, or None when no file is service-domain-anchored."""
    files = _flow_files(flow)
    if not files:
        return None
    dom = _service_domain_of(files[0])  # entry file (first) is definitive
    if dom is None:
        by_dom: dict[str, list[str]] = {}
        for f in files:
            dd = _service_domain_of(f)
            if dd is not None:
                by_dom.setdefault(dd, []).append(f)
        if not by_dom:
            return None
        dom = max(by_dom.items(), key=lambda kv: (len(kv[1]), kv[0]))[0]
    dom_files = [f for f in files if _service_domain_of(f) == dom]
    return dom, dom_files


def carve_service_domains(features: list["Feature"]) -> ServiceCarveResult:
    """Carve service-domain flow subtrees out of mega-anchor devs into their
    own developer features. Appends the minted features to *features* in place;
    returns telemetry. No-op when disabled — safe to wire unconditionally."""
    from faultline.pipeline_v2.stage_8_7_anchor_desink import _is_workspace_anchor
    from faultline.pipeline_v2.stage_8_9_anchor_subdecompose import _make_subfeature

    result = ServiceCarveResult(enabled=_carve_enabled())
    if not result.enabled:
        return result

    devs = [f for f in features
            if getattr(f, "layer", "developer") == "developer"]
    taken_names = {f.name for f in features if getattr(f, "name", None)}
    by_name = {f.name: f for f in devs if getattr(f, "name", None)}
    minted_all: list["Feature"] = []

    for source in devs:
        flows = list(getattr(source, "flows", None) or [])
        if not flows:
            continue
        # Group flows by their dominant service domain; collect domain files.
        dom_flows: dict[str, list[Any]] = {}
        dom_files: dict[str, set[str]] = {}
        for fl in flows:
            hit = _flow_domain(fl)
            if hit is None:
                continue
            dom, files = hit
            dom_flows.setdefault(dom, []).append(fl)
            dom_files.setdefault(dom, set()).update(files)
        src_slug = _norm(source.name or "")
        eligible = {
            dom for dom, files in dom_files.items()
            if len(files) >= _CARVE_MIN_FILES
            and _norm(dom) not in _GENERIC_DOMAIN_SKIP
            and _norm(dom) not in _INFRA_DOMAIN_SKIP
            and _norm(dom) != src_slug and _singular(_norm(dom)) != src_slug
        }
        # SOURCE gate: a workspace anchor OR a multi-domain flow container.
        if not eligible:
            continue
        if not _is_workspace_anchor(source) and len(eligible) < 2:
            continue
        result.anchors_examined += 1
        minted: list["Feature"] = []
        moved_ids: set[int] = set()
        carved_files: set[str] = set()
        touched_any = False
        for dom in sorted(eligible):
            name = _norm(dom)
            files = sorted(dom_files[dom])
            dom_flow_list = list(dom_flows[dom])
            existing = by_name.get(name)
            if existing is not None:
                # A same-named developer feature already exists — TRANSFER the
                # anchor's domain flows onto it (it already carries the domain's
                # identity + a real product-feature mapping) instead of minting
                # a duplicate. Never transfer to the source or to another anchor.
                if existing is source or _is_workspace_anchor(existing):
                    result.collisions_skipped += 1
                    continue
                from faultline.models.types import MemberFile  # local: cycle
                existing.flows = list(getattr(existing, "flows", None) or []) \
                    + dom_flow_list
                owned = {p for p in (existing.paths or [])}
                for p in files:
                    if p not in owned:
                        existing.paths.append(p)
                        existing.member_files = (
                            getattr(existing, "member_files", None) or [])
                        existing.member_files.append(MemberFile(
                            path=p, role="anchor", confidence=1.0, primary=True,
                            evidence=f"service-domain carve from '{source.name}'"))
                        owned.add(p)
                result.domains_carved += 1
                result.flows_moved += len(dom_flow_list)
                result.files_claimed += len(files)
            else:
                sub = _make_subfeature(source, dom, files, name)
                # _make_subfeature leaves flows on the source by contract; the
                # carve MOVES the domain's flows onto the carved dev (its
                # identity is those journeys — the point of the lift).
                sub.flows = dom_flow_list
                taken_names.add(name)
                by_name[name] = sub
                minted.append(sub)
                result.domains_carved += 1
                result.flows_moved += len(dom_flow_list)
                result.files_claimed += len(files)
            moved_ids.update(id(fl) for fl in dom_flow_list)
            carved_files.update(files)
            touched_any = True
        if not touched_any:
            continue
        # Source keeps the rest: carved flows leave; carved files (now owned by
        # the sub) leave the source's paths/member ledger.
        source.flows = [fl for fl in flows if id(fl) not in moved_ids]
        if carved_files:
            source.paths = [p for p in (source.paths or [])
                            if p not in carved_files]
            source.member_files = [
                mf for mf in (getattr(source, "member_files", None) or [])
                if _member_path(mf) not in carved_files
            ]
        minted_all.extend(minted)
        result.anchors_carved += 1
        if len(result.sample) < 20:
            result.sample.append({
                "source": source.name,
                "carved_domains": sorted(_norm(d) for d in eligible
                                         if id(dom_flows[d][0]) in moved_ids),
                "flows_moved": len(moved_ids),
            })

    features.extend(minted_all)
    return result


__all__ = [
    "DomainAttributionResult",
    "attribute_domain_members",
    "ServiceCarveResult",
    "carve_service_domains",
]
