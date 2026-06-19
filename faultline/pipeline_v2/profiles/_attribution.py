"""Profile-driven attribution mechanism (P4) — generic, framework-agnostic.

This module is the *wiring* that lets the active :class:`FrameworkProfile`
give the FIRST say over which feature a file belongs to, BEFORE the
generic path-proximity / conflict-resolution logic in Stage 2 / 2.6
runs. It is the lever that kills the physical-container blob: instead of
clustering files by where they sit on disk, the framework's own
structural model (route-group / feature-folder semantics) decides
ownership.

Design (matches [[design-pattern-expert]] Chain of Responsibility +
Strategy):

  * The profile is the *strategy*: ``profile.feature_of(path, ctx)``
    returns a kebab feature key or ``None`` ("no opinion").
  * The wiring is the *chain*: profile-claims are applied first; every
    unclaimed path falls through to the existing logic UNCHANGED.
  * The :class:`DefaultProfile` claims nothing (``feature_of`` → ``None``,
    empty :class:`AttributionSpec`), so this whole module is a no-op
    under the default profile — the byte-for-byte regression guard.

NO LLM, NO network, NO corpus-specific paths, NO magic numbers. All
policy comes from the profile's declarative
:class:`~faultline.pipeline_v2.profiles.base.AttributionSpec`; this
module only *applies* it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from faultline.pipeline_v2.profiles.base import AttributionSpec, FileRole

if TYPE_CHECKING:
    from faultline.pipeline_v2.profiles.base import FrameworkProfile
    from faultline.pipeline_v2.stage_0_intake import ScanContext


class _HasNamePaths(Protocol):
    """The minimal feature shape this module reads (structural typing).

    Both :class:`~faultline.pipeline_v2.stage_2_reconcile.DeveloperFeature`
    and any future feature record satisfy this; we never import the
    concrete class, keeping the wiring decoupled (DIP).
    """

    name: str
    paths: tuple[str, ...]


def is_active(profile: "FrameworkProfile | None") -> bool:
    """``True`` when ``profile`` is a concrete profile that can attribute.

    The DefaultProfile / ``None`` case returns ``False`` so callers can
    cheaply short-circuit to the unchanged legacy path. A profile is
    "active" for attribution iff it is not ``None`` and its ``name`` is
    not the reserved ``"default"`` slug — the single, explicit gate that
    guarantees zero behaviour change when no concrete profile wins
    detection.
    """
    return profile is not None and getattr(profile, "name", "default") != "default"


def synth_features(
    profile: "FrameworkProfile | None",
    ctx: "ScanContext",
) -> list["_SynthFeature"]:
    """Capability features the profile wants CREATED before re-homing.

    Optional synthesis contract: a profile MAY implement
    ``synthesize_features(ctx) -> list`` to declare capability boundaries
    the deterministic extractors miss (e.g. Next route groups / module
    folders inside a single workspace package). The wiring creates a
    feature per returned item whose ``name`` does not already exist, then
    the normal re-home (:func:`apply_profile_attribution`) moves the
    boundary's files onto it.

    Duck-typed via ``getattr`` so profiles WITHOUT the method (the
    DefaultProfile and any stack that doesn't sub-decompose) are a strict
    no-op — guaranteeing byte-for-byte regression safety. Returns ``[]``
    for the default / ``None`` profile and for any profile lacking the
    method.
    """
    if not is_active(profile):
        return []
    assert profile is not None
    method = getattr(profile, "synthesize_features", None)
    if method is None:
        return []
    result = method(ctx)
    return list(result) if result else []


def profile_claims(
    profile: "FrameworkProfile | None",
    paths: list[str],
    ctx: "ScanContext",
) -> dict[str, str]:
    """Map ``path -> feature_key`` for every path the profile claims.

    Returns an empty dict for the default / ``None`` profile (no-op
    guard) and for any path where ``feature_of`` returns ``None``. The
    profile is queried at most once per path; results are deterministic
    and side-effect-free.
    """
    if not is_active(profile):
        return {}
    assert profile is not None  # narrowed by is_active
    out: dict[str, str] = {}
    for path in paths:
        key = profile.feature_of(path, ctx)
        if key:
            out[path] = key
    return out


def shared_roles(profile: "FrameworkProfile | None") -> frozenset[FileRole]:
    """The set of file roles the profile wants fanned out (blast-radius).

    Empty for the default / ``None`` profile. A file whose role is in
    this set is NOT collapsed into a single owner; instead it is
    attributed to every feature that genuinely exercises it, up to
    :func:`max_fanout`.
    """
    if not is_active(profile):
        return frozenset()
    assert profile is not None
    spec: AttributionSpec = profile.attribution_rules()
    return frozenset(spec.shared_roles)


def max_fanout(profile: "FrameworkProfile | None") -> int | None:
    """Cap on how many features a shared file may attribute to.

    ``None`` (the default) means "let the consuming stage decide" — the
    wiring imposes NO magic number; the policy is the profile's.
    """
    if not is_active(profile):
        return None
    assert profile is not None
    return profile.attribution_rules().max_fanout


def role_of(profile: "FrameworkProfile | None", path: str) -> FileRole:
    """Classify ``path`` via the profile, or :attr:`FileRole.UNKNOWN`.

    Pure pass-through to ``profile.classify_file``; the default profile
    returns ``UNKNOWN`` for everything, so callers see no shared roles.
    """
    if profile is None:
        return FileRole.UNKNOWN
    return profile.classify_file(path)


def apply_profile_attribution(
    features: list[_HasNamePaths],
    profile: "FrameworkProfile | None",
    ctx: "ScanContext",
    *,
    rebuild: "RebuildFn",
    make_feature: "MakeFeatureFn | None" = None,
) -> list[_HasNamePaths]:
    """Re-home claimed paths to the feature the profile names — no-op for default.

    For each path the profile claims (:func:`profile_claims`), ensure the
    path lives on the feature whose ``name`` equals the claimed key and
    is removed from every OTHER feature — UNLESS the path's role is a
    declared shared role, in which case it is left in place (fan-out is
    handled by the membership stage, not here).

    Before re-homing, the profile's optional :func:`synth_features` are
    materialised: a feature is CREATED for each synthesised boundary whose
    name does not already exist, so the re-home has a landing target. This
    is what lets the Next profile sub-decompose a single-package
    workspace anchor (route groups / module folders) — without it those
    boundaries are never features and the blob persists. The default /
    ``None`` profile synthesises nothing → byte-for-byte no-op.

    This runs BEFORE the generic conflict-resolution strip so the
    framework's structural truth wins over path-proximity. Paths the
    profile does not claim are untouched, so the legacy path is
    preserved exactly.

    Args:
        features: the working feature list (read-only; rebuilt copies are
            returned).
        profile: the active framework profile (``None``/default → no-op).
        ctx: the scan context, threaded to ``feature_of``.
        rebuild: a callback ``(feature, new_paths) -> feature`` the caller
            supplies so this module never imports the concrete
            ``DeveloperFeature`` (keeps the wiring decoupled). It must
            return a copy of ``feature`` with ``paths`` replaced.
        make_feature: optional callback ``(name, paths) -> feature`` used
            to materialise synthesised boundary features. When ``None``,
            synthesis is skipped (older callers keep their exact
            behaviour); the new caller supplies it so sub-decomposition
            lands.

    Returns:
        A new feature list with synthesised boundaries created + claimed
        paths re-homed. When the profile is the default / ``None``, the
        input list is returned unchanged (identity), guaranteeing zero
        regression.
    """
    if not is_active(profile):
        return features

    # Materialise synthesised boundary features (route groups / module
    # folders) the extractors missed, so the re-home below has a target.
    # Only CREATE a name that does not already exist — never overwrite an
    # extractor-surfaced feature.
    if make_feature is not None:
        existing_names = {f.name for f in features}
        created: list[_HasNamePaths] = []
        for synth in synth_features(profile, ctx):
            name = getattr(synth, "name", "")
            if not name or name in existing_names:
                continue
            existing_names.add(name)
            # Seed with no paths; the re-home pass below pulls the
            # boundary's files onto it from the workspace anchor. This
            # keeps a single source of truth (feature_of) for which file
            # lands where, and lets _attribute_paths arbitrate cleanly.
            created.append(make_feature(name, ()))
        if created:
            features = list(features) + created

    all_paths = [p for f in features for p in f.paths]
    claims = profile_claims(profile, all_paths, ctx)
    if not claims:
        return features

    by_name = {f.name: f for f in features}

    # A path is re-homed only when the claimed feature actually exists in
    # this scope (created above from synthesis, or surfaced by an
    # extractor). A profile CLAIM (``feature_of`` non-None) is an
    # ownership statement: the file is COLOCATED inside that capability
    # boundary (a route-segment / route-group / module folder), so it
    # belongs to it even when its role is component/hook/lib — colocation
    # inside a feature folder beats the generic "shared primitive" rule.
    # Genuinely-shared files (repo-level ``components/`` / ``lib/`` with no
    # owning boundary) are NEVER claimed (``feature_of`` → ``None``), so
    # the fan-out policy (:func:`shared_roles`) applies to them downstream
    # in the membership stage, not here. Re-homing a claimed colocated
    # component is exactly what strips it off the workspace anchor (the
    # blob fix); excluding shared-role claims here re-glued module-owned
    # components to the anchor.
    rehome: dict[str, str] = {
        path: key
        for path, key in claims.items()
        if key in by_name
    }
    if not rehome:
        return features

    rebuilt: list[_HasNamePaths] = []
    for f in features:
        owner_for = {p: rehome[p] for p in f.paths if p in rehome}
        # Paths this feature should KEEP: ones not claimed for another
        # feature (claimed-for-self or unclaimed).
        keep = tuple(p for p in f.paths if owner_for.get(p, f.name) == f.name)
        # Paths claimed for THIS feature that it doesn't already hold.
        gained = tuple(
            p for p, key in rehome.items() if key == f.name and p not in f.paths
        )
        new_paths = keep + gained
        if new_paths == f.paths:
            rebuilt.append(f)
        else:
            rebuilt.append(rebuild(f, new_paths))
    return rebuilt


class RebuildFn(Protocol):
    """Caller-supplied copy-with-new-paths callback (avoids a hard import)."""

    def __call__(self, feature: _HasNamePaths, new_paths: tuple[str, ...]) -> _HasNamePaths:
        ...


class MakeFeatureFn(Protocol):
    """Caller-supplied construct-new-feature callback (avoids a hard import).

    Builds a fresh feature record with the given name + paths so this
    module never imports the concrete ``DeveloperFeature`` — the synthesis
    of profile boundaries stays decoupled (DIP), exactly like
    :class:`RebuildFn`.
    """

    def __call__(self, name: str, paths: tuple[str, ...]) -> _HasNamePaths:
        ...


class _SynthFeature(Protocol):
    """Minimal shape of a profile-synthesised boundary (structural typing)."""

    name: str
    paths: tuple[str, ...]


__all__ = [
    "apply_profile_attribution",
    "is_active",
    "max_fanout",
    "profile_claims",
    "role_of",
    "shared_roles",
    "synth_features",
]
