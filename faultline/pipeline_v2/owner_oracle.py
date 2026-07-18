"""S1 — Owner-oracle: the single deterministic file→owner election ($0).

The disease (S1 root-cause, VERIFIED by the 2026-07-18 census probe): THREE
independent "who owns this file" resolutions ran side by side —

  * R1 :func:`indexes.build_path_index`      — first-claimant by FEATURES-LIST
                                               order;
  * R2 :func:`conservation.build_file_pf_owner` — first-claimant over the dev
                                               paths (facet/shared excluded);
  * R3 the Stage 6.97 ``_primary`` election  — a real deterministic election
                                               (module-subtree > non-facet >
                                               dir-count > flows > slug).

R1/R2 are ORDER-SENSITIVE: any fix that inserts or reorders features silently
flips the owner of a contested file WITHOUT any change in evidence — the
inter-version "spilling from glass to glass". The census probe measured the
R1/R2 (first-claimant) vs R3 (election) split at DEV grain 6.47% documenso /
2.55% cal / 19.14% novu, PF grain 0.85 / 1.23 / 3.08%.

THE FIX (this module): promote the Stage 6.97 election to a SHARED service —
``owner_of(file)`` — computed ONCE from the settled dev-feature membership and
consumed by every owner READ (path_index, conservation votes, i16, dispatch,
the terminal dir-vote, and Stage 6.97 itself). First-claimant dies as a
semantics. The facet/shared exclusion that R2 applied is preserved as a
COVERAGE VIEW over the same election (same owner, filtered visibility) — NOT a
separate rule.

SACRED invariants (S1 spec):
  * The oracle NEVER moves membership — it only unifies the READ-resolution of
    an owner. No new ownership-mutators; the conservation ladder / rulers are
    untouched (same ruler, a more stable vote base).
  * The election is the SAME algorithm Stage 6.97 already shipped
    (:func:`elect_primary_owners` is the single implementation both call), so
    turning the oracle ON does not change WHO 6.97 elected — it only makes
    R1/R2 elect the same owner instead of first-claimant.
  * The graveyard stays buried: co-commit / name-attribution are NEVER
    election inputs.

Flag: ``FAULTLINE_OWNER_ORACLE`` (default OFF). Unset / ``0`` → every consumer
keeps its shipped first-claimant path → byte-identical output. The KEY_SCHEMA
bump rides the SEPARATE later flip commit only (path_index consumers shift).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature

__all__ = [
    "OWNER_ORACLE_ENV",
    "owner_oracle_enabled",
    "elect_primary_owners",
    "OwnerElection",
    "build_owner_election",
]

OWNER_ORACLE_ENV = "FAULTLINE_OWNER_ORACLE"

#: Mirror of :data:`conservation._SHARED_PF_KEYS` — a dev bound to the
#: shared-platform bucket never owns a PF-grain file (the coverage view).
_SHARED_PF_KEYS = frozenset(("shared-platform", "platform"))


def owner_oracle_enabled() -> bool:
    """Default **OFF**. ``FAULTLINE_OWNER_ORACLE`` in ``{1,true,on,yes}``
    arms the unified election; unset / ``0`` / any falsy token keeps every
    consumer on its shipped first-claimant path (byte-identical)."""
    return os.environ.get(OWNER_ORACLE_ENV, "0").strip().lower() in {
        "1", "true", "on", "yes",
    }


# ── The single election implementation ──────────────────────────────────
#
# Extracted VERBATIM from ``stage_6_97_feature_loc.apply_feature_loc._primary``
# so Stage 6.97 and the oracle can never drift: both call THIS function. The
# two string primitives below are copied (trivially) to keep this function
# self-contained (no module-level import of stage_6_97 — that would cycle,
# since 6.97 imports this).


def _parent_dir(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _module_match_len(fp: str, roots: "frozenset[str]") -> int:
    """Length of the LONGEST module root in *roots* that contains *fp*
    (an ancestor directory), else 0. Longest = most specific module wins."""
    best = 0
    for r in roots:
        if len(r) > best and fp.startswith(r + "/"):
            best = len(r)
    return best


def elect_primary_owners(
    file_to_devs: dict[str, list[int]],
    dev_is_facet: list[int],
    dev_module_roots: list["frozenset[str]"],
    dev_dircount: list[dict[str, int]],
    dev_flowcount: list[int],
    dev_slug: list[str],
    mod_enabled: bool,
) -> dict[str, int]:
    """``{file: dev_index}`` — the deterministic primary-owner election.

    THE single owner election (Stage 6.97's ``_primary``, promoted). For a
    file with one claimant that claimant owns it; for a contested file:

      1. (``mod_enabled``) the dev whose OWN module subtree most specifically
         contains the file wins — non-facet claimants only, longest match; a
         tie among same-depth modules falls through to the ordinary rule;
      2. the ordinary rule: non-facet first, then the most sibling-dir counted
         files, then the most flows (behavioural mass), then the smallest slug.

    Order-INDEPENDENT: the result is a pure function of the per-dev signals,
    not of the features-list order (that is the whole point of S1).
    """
    primary_of: dict[str, int] = {}
    for fp, owners in file_to_devs.items():
        if len(owners) == 1:
            primary_of[fp] = owners[0]
            continue
        contest = owners
        if mod_enabled:
            best_len = 0
            claimants: list[int] = []
            for i in contest:
                if dev_is_facet[i]:
                    continue
                mlen = _module_match_len(fp, dev_module_roots[i])
                if mlen > best_len:
                    best_len = mlen
                    claimants = [i]
                elif mlen == best_len and mlen > 0:
                    claimants.append(i)
            if best_len > 0:
                if len(claimants) == 1:
                    primary_of[fp] = claimants[0]
                    continue
                contest = claimants  # tie among same-depth modules -> ordinary
        d = _parent_dir(fp)
        primary_of[fp] = min(
            contest,
            key=lambda i: (
                dev_is_facet[i],
                -dev_dircount[i].get(d, 0),
                -dev_flowcount[i],
                dev_slug[i],
            ),
        )
    return primary_of


# ── The oracle service ──────────────────────────────────────────────────


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").strip().strip("/")


class OwnerElection:
    """A computed file→owner election over one settled dev-feature snapshot.

    Holds the raw ``primary_of`` (file → dev index) plus the dev features so
    every consumer resolves the SAME owner. Two projected views:

      * :meth:`file_owner_uuid_map` — file → owning dev ``uuid`` (path_index R1);
      * :meth:`file_pf_owner_map`   — file → owning PF key, with the R2 COVERAGE
        VIEW applied (facet / shared-bound / stale-pfid owners are filtered to
        None — same owner, filtered visibility).
    """

    __slots__ = ("dev_features", "dev_files", "file_to_devs", "primary_of")

    def __init__(
        self,
        dev_features: list["Feature"],
        dev_files: list[dict[str, int]],
        file_to_devs: dict[str, list[int]],
        primary_of: dict[str, int],
    ) -> None:
        self.dev_features = dev_features
        self.dev_files = dev_files
        self.file_to_devs = file_to_devs
        self.primary_of = primary_of

    # -- single-file reads --------------------------------------------------

    def owner_index(self, file: str) -> int | None:
        idx = self.primary_of.get(file)
        if idx is None:
            idx = self.primary_of.get(_norm(file))
        return idx

    def owner_uuid(self, file: str) -> str | None:
        idx = self.owner_index(file)
        if idx is None:
            return None
        uuid = str(getattr(self.dev_features[idx], "uuid", "") or "")
        return uuid or None

    def owner_pfid(
        self, file: str, real_pf_keys: "frozenset[str] | None" = None,
    ) -> str | None:
        idx = self.owner_index(file)
        if idx is None:
            return None
        return self._pfid_for(idx, real_pf_keys)

    # -- projected maps -----------------------------------------------------

    def file_owner_uuid_map(self) -> dict[str, str]:
        """``{file: owning-dev uuid}`` for build_path_index (R1)."""
        out: dict[str, str] = {}
        for fp, idx in self.primary_of.items():
            uuid = str(getattr(self.dev_features[idx], "uuid", "") or "")
            if uuid:
                out[fp] = uuid
        return out

    def file_pf_owner_map(
        self, real_pf_keys: "frozenset[str] | None" = None,
    ) -> dict[str, str]:
        """``{file: owning PF key}`` for build_file_pf_owner (R2), with the
        facet / shared / stale-pfid COVERAGE VIEW applied (owner unchanged;
        a filtered owner simply does not appear — a None in that context)."""
        out: dict[str, str] = {}
        for fp, idx in self.primary_of.items():
            pfid = self._pfid_for(idx, real_pf_keys)
            if pfid is not None:
                out[fp] = pfid
        return out

    # -- internal -----------------------------------------------------------

    def _pfid_for(
        self, idx: int, real_pf_keys: "frozenset[str] | None",
    ) -> str | None:
        dev = self.dev_features[idx]
        if getattr(dev, "role", None) == "facet":
            return None  # §4.1 — a facet never owns a PF-grain file
        pfid = getattr(dev, "product_feature_id", None)
        if not pfid or str(pfid).strip().lower() in _SHARED_PF_KEYS:
            return None
        key = str(pfid)
        if real_pf_keys is not None and key not in real_pf_keys:
            return None
        return key


def build_owner_election(
    dev_features: list["Feature"],
    repo_root: Path | str,
    *,
    cache: dict[str, int] | None = None,
) -> OwnerElection:
    """Compute the owner election over ``dev_features`` (developer layer only).

    Reuses the EXACT Stage 6.97 signal builders (``_expand_feature_files`` for
    the counted-file expansion, ``_module_dirs`` for the module subtree roots,
    ``is_facet`` for the concern-facet flag, ``ownership_v2_enabled`` for the
    module-subtree gate) so the election is identical to the one Stage 6.97
    computes over the same snapshot. ``cache`` (file→loc) may be shared with
    Stage 6.97 to avoid re-walking the tree.

    Callers must pass ONLY developer-layer features (product-layer duplicates
    carry the same paths and would double the file→dev map). The election is a
    pure function of the snapshot — no I/O beyond the file-expansion the LOC
    cache already performed.
    """
    # Late imports — stage_6_97 imports THIS module at top level, so the
    # reverse edge must be deferred to run time (no import cycle).
    from faultline.pipeline_v2.ownership_v2 import ownership_v2_enabled
    from faultline.pipeline_v2.spine_hygiene import is_facet
    from faultline.pipeline_v2.stage_6_97_feature_loc import (
        _expand_feature_files,
        _module_dirs,
    )

    root = Path(repo_root)
    if cache is None:
        cache = {}

    dev_files: list[dict[str, int]] = [
        _expand_feature_files(root, getattr(f, "paths", None) or [], cache)
        for f in dev_features
    ]
    file_to_devs: dict[str, list[int]] = {}
    for i, files in enumerate(dev_files):
        for fp in files:
            file_to_devs.setdefault(fp, []).append(i)

    dev_dircount: list[dict[str, int]] = []
    for files in dev_files:
        dc: dict[str, int] = {}
        for fp in files:
            d = _parent_dir(fp)
            dc[d] = dc.get(d, 0) + 1
        dev_dircount.append(dc)
    dev_flowcount = [len(getattr(f, "flows", None) or []) for f in dev_features]
    dev_slug = [str(getattr(f, "name", "") or "") for f in dev_features]
    dev_is_facet = [1 if is_facet(f) else 0 for f in dev_features]
    mod_enabled = ownership_v2_enabled()
    dev_module_roots: list[frozenset[str]] = (
        [_module_dirs(f) for f in dev_features] if mod_enabled else []
    )

    primary_of = elect_primary_owners(
        file_to_devs,
        dev_is_facet,
        dev_module_roots,
        dev_dircount,
        dev_flowcount,
        dev_slug,
        mod_enabled,
    )
    return OwnerElection(dev_features, dev_files, file_to_devs, primary_of)


def build_owner_election_from(
    features: Iterable["Feature"],
    repo_root: Path | str,
    *,
    cache: dict[str, int] | None = None,
) -> OwnerElection:
    """Convenience: build the election from a MIXED features list (filters to
    developer-layer, matching the Stage 6.97 ownership universe)."""
    devs = [
        f for f in features
        if getattr(f, "layer", "developer") != "product"
    ]
    return build_owner_election(devs, repo_root, cache=cache)
