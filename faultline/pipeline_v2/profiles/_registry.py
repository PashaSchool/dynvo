"""Framework-profile discovery + selection registry.

Mirrors the Stage 1 extractor discovery
(:mod:`faultline.pipeline_v2.stage_1_extractors`) and the Stage 6.4
linker discovery (:mod:`faultline.framework_linkers._discovery`):

  1. Python entry-points under the ``faultlines.profiles`` group ADD any
     third-party / customer profiles. Customers register a framework by
     dropping a module + an entry-point line — zero core changes (OCP).
  2. The built-in registry is the fallback / merge base. Today it is
     just :class:`DefaultProfile`; deterministic per-framework profiles
     (Next App Router, NestJS, Remix, ...) are appended in later phases
     by adding ONE ``_try(...)`` line + ONE entry-point line.

The :class:`DefaultProfile` is ALWAYS present (it is injected even when
entry-points are found) so unknown stacks can never be left without a
profile — the universal guarantee.

Selection (:func:`select_profile`) is pure highest-``detects``-wins,
with :class:`DefaultProfile`'s positive floor breaking the all-zero
case. No LLM, no network.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points  # module-level so tests can monkeypatch

from faultline.pipeline_v2.profiles.base import FrameworkProfile
from faultline.pipeline_v2.profiles.default import DefaultProfile

logger = logging.getLogger(__name__)


_ENTRY_POINT_GROUP = "faultlines.profiles"


def _load_default_profiles() -> list[FrameworkProfile]:
    """Direct-import the built-in profiles (fallback / merge base).

    To add a deterministic profile later: append one ``_try(...)`` call
    here AND one line under
    ``[project.entry-points."faultlines.profiles"]`` in pyproject.toml.
    Never modify the Protocol or the selection core.
    """
    out: list[FrameworkProfile] = [DefaultProfile()]

    def _try(import_path: str, class_name: str) -> None:
        try:
            module = __import__(import_path, fromlist=[class_name])
            cls = getattr(module, class_name)
            instance = cls()
            if isinstance(instance, FrameworkProfile):
                out.append(instance)
            else:
                logger.warning(
                    "%s.%s does not satisfy FrameworkProfile",
                    import_path, class_name,
                )
        except (ImportError, AttributeError) as exc:
            logger.debug(
                "default profile %s.%s not available: %s",
                import_path, class_name, exc,
            )

    # Phase 2+ deterministic profiles register here (built-in fallback so
    # the profile is active even without an editable reinstall picking up
    # the entry-point; a colliding entry-point is ignored — in-tree wins).
    _try("faultline.pipeline_v2.profiles.next_app_router",
         "NextAppRouterProfile")
    _try("faultline.pipeline_v2.profiles.fastapi_family",
         "FastApiFamilyProfile")
    _try("faultline.pipeline_v2.profiles.django",
         "DjangoProfile")
    _try("faultline.pipeline_v2.profiles.next_pages_react",
         "NextPagesReactProfile")

    return out


def discover_profiles() -> list[FrameworkProfile]:
    """Discover all registered profiles, default always included.

    Entry-point profiles are MERGED on top of the built-ins (the same
    snapshot-drift-immune model Stage 1 uses): an entry-point whose
    ``name`` collides with a built-in is ignored so the in-tree class
    stays authoritative. :class:`DefaultProfile` is always present.
    """
    builtins = _load_default_profiles()
    by_name: dict[str, FrameworkProfile] = {p.name: p for p in builtins}

    try:
        eps = entry_points(group=_ENTRY_POINT_GROUP)
    except TypeError:  # Python <3.10 entry_points() returns a dict
        all_eps = entry_points()
        eps = all_eps.get(_ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — defensive
        eps = []

    for ep in eps:
        try:
            cls = ep.load()
            instance = cls()
        except Exception as exc:  # noqa: BLE001 — non-fatal
            logger.warning("failed to load framework profile %s: %s", ep.name, exc)
            continue
        if not isinstance(instance, FrameworkProfile):
            logger.warning(
                "entry-point %s did not satisfy FrameworkProfile", ep.name,
            )
            continue
        if instance.name in by_name:
            # In-tree built-in wins over a stale / colliding entry-point.
            continue
        by_name[instance.name] = instance

    return list(by_name.values())


class ProfileRegistry:
    """An explicit, testable registry of framework profiles.

    Most callers want :func:`discover_profiles` + :func:`select_profile`
    and never touch this class. It exists for (a) tests that need to
    register/lookup deterministically without entry-points and (b)
    callers that want to inject a fixed profile set (DIP — pass the
    registry in rather than reaching for global discovery).
    """

    def __init__(self, profiles: list[FrameworkProfile] | None = None) -> None:
        seed = profiles if profiles is not None else discover_profiles()
        self._by_name: dict[str, FrameworkProfile] = {}
        for p in seed:
            self.register(p)
        # The default profile is non-negotiable — inject it if absent so
        # selection can never return ``None``.
        if "default" not in self._by_name:
            self.register(DefaultProfile())

    def register(self, profile: FrameworkProfile, *, replace: bool = True) -> None:
        """Add ``profile`` to the registry, keyed by its ``name``.

        ``replace=False`` makes an existing same-name profile win
        (built-in-authoritative); the default ``replace=True`` lets a
        deliberate caller override (used by tests).
        """
        if not isinstance(profile, FrameworkProfile):
            raise TypeError(
                f"{profile!r} does not satisfy the FrameworkProfile protocol"
            )
        if not replace and profile.name in self._by_name:
            return
        self._by_name[profile.name] = profile

    def get(self, name: str) -> FrameworkProfile | None:
        """Look a profile up by its ``name`` slug, or ``None``."""
        return self._by_name.get(name)

    def all(self) -> list[FrameworkProfile]:
        """All registered profiles."""
        return list(self._by_name.values())

    @property
    def default(self) -> FrameworkProfile:
        """The default / null-object profile (always present)."""
        return self._by_name["default"]

    def select(self, ctx) -> FrameworkProfile:  # noqa: ANN001 — ScanContext, avoid import cycle
        """Pick the highest-``detects`` profile for ``ctx`` — deterministically.

        G1 (stack-profile-architecture spec): exactly one profile per
        scan-unit, selected by the highest ``detects()`` score. Ties
        break by profile ``name`` lexicographic (ascending) so the
        winner NEVER depends on registration / discovery order — a new
        profile can change another repo's selection only by genuinely
        outscoring the incumbent, which the per-pinned-repo selection
        fixtures catch. No positive score at all → :class:`DefaultProfile`
        (its floor normally guarantees this branch is unreachable, but
        the guard keeps selection total even with a floorless default).
        """
        scored = sorted(
            ((profile.detects(ctx), profile) for profile in self._by_name.values()),
            key=lambda pair: (-pair[0], pair[1].name),
        )
        best_score, best = scored[0]
        if best_score <= 0.0:
            return self.default
        return best


def select_profile(ctx, profiles: list[FrameworkProfile] | None = None):  # noqa: ANN001, ANN201
    """Convenience: build a registry (discovering if needed) and select.

    Pass ``profiles`` to select within a fixed set (tests / injection);
    omit it to discover via entry-points + built-ins.
    """
    return ProfileRegistry(profiles).select(ctx)


__all__ = [
    "ProfileRegistry",
    "discover_profiles",
    "select_profile",
]
