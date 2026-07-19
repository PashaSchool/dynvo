"""AppRouterKeylessExtractor — App-Router page/route files as Stage-1 route
entries, keyed on the STRUCTURAL convention rather than the stack tag (S4-a).

The App-Router keyless evidence gap (VERIFIED forensics, B72): a Next.js App
Router tree (``app/**/page.tsx`` + ``app/**/route.ts``) never reaches
``routes_index`` whenever the tree does NOT sit in a clean, single
``next-app-router``-tagged workspace — so the demote/shield mechanisms that
read ``routes_index`` are blind to it. Two live shapes:

  * **Monorepo residue (cal.com).** ``apps/web`` IS tagged
    ``next-app-router``, but the repo also carries a ``next-pages`` unit
    (``apps/api/v1``). The composite profile scopes that unit's
    ``next-pages-react`` ``route``-named override, which Stage-1
    ``merge_profile_extractors`` REPLACES the stock global ``route``
    extractor with — an adapter that only covers the overriding units, so
    the App-Router residue (``apps/web/app/**``) loses its route pass
    entirely (939 rows, 0 under ``apps/web/app`` despite 186 ``page.tsx``).
  * **Polyglot leftover (onyx).** ``web/`` is NOT a declared workspace (the
    root ``package.json workspaces`` lists ``widget``/``desktop``/…), so it
    falls to the per-workspace LEFTOVER pass, which runs the stock ``route``
    extractor with the ROOT stack tag (``js-generic``). ``js-generic`` has no
    file-system routing convention, so ``web/src/app/**`` is never extracted
    (114 rows, 0 under ``web/src/app`` despite 102 ``page.tsx``).

Both are the SAME class: the App-Router pass is GATED on the scope's stack
TAG being exactly ``next-app-router`` (or on the stock ``route`` source
surviving the composite), and in these scopes neither holds. The fix is a
dedicated extractor that fires on the App-Router CONVENTION — a ``page``/
``route`` leaf under an ``app``/``src/app`` root run, matched ANYWHERE in the
path — independent of the stack tag or the composite's scoped-override seam.
Because it carries its OWN source name (``route-approuter``), Stage-1's
replace-by-name never narrows it, and because it keys on convention the
``js-generic`` leftover tag never suppresses it.

Contained by design: it emits ONLY explicit ``routes`` rows for
``routes_index`` (the missing evidence). It does NOT invent features / flows
— the App-Router profile already anchors those files (``feature_of`` /
``synthesize_features`` / ``flow_entries``) via the SAME first-segment slug,
so a same-slug anchor merges by name (the alignment contract) and never
twins. ``build_routes_index`` folds these candidates LAST, so a
``(pattern, method, file)`` triple an existing source already emitted (a
CLEAN ``next-app-router`` repo where the stock ``route`` pass ran) wins and
stays byte-identical — approuter rows only ADD.

Flag ``FAULTLINE_APPROUTER_KEYLESS`` — default **OFF**. Explicit ``0`` /
unset -> ``extract`` returns ``[]`` AND the registry does not even register
the source (``scan_meta.extractor_hits`` serializes every registered key —
the B67 kill-switch lesson), so the OFF scan is byte-identical to pre-S4a.

The App-Router convention (roots ``app/`` + ``src/app/``; ``page``/``route``
suffixes) is read from ``stacks/filesystem-routing.yaml`` (the
``next-app-router`` entry) — mechanisms from YAML, never a per-repo path.

No LLM. No network. Read-only.
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import TYPE_CHECKING

from faultline.analyzer.validation import is_test_file
from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._util import is_noise, posix, slugify
from faultline.pipeline_v2.extractors.base import AnchorCandidate
from faultline.pipeline_v2.indexes import derive_app_router_route

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


APPROUTER_KEYLESS_ENV = "FAULTLINE_APPROUTER_KEYLESS"

#: The single source slug on every emitted candidate (so ``extractor_hits``
#: grows by exactly one key when the flag is ON — the B67 kill-switch law).
APPROUTER_SOURCE = "route-approuter"

#: Confidence — same weak-scaling floor the stock route extractor uses.
_CONF_BASE = 0.6
_CONF_STEP = 0.05
_CONF_CAP = 0.95


def approuter_keyless_enabled() -> bool:
    """Default **ON** since the 2026-07-19 S*-pack flip (KEY_SCHEMA 32;
    cal 0->249, onyx 0->106 routes; conservation 0 — S4a).
    ``FAULTLINE_APPROUTER_KEYLESS=0`` (or false/no/off) keeps the extractor
    inert (``extract`` -> ``[]``) AND unregistered (see
    :mod:`faultline.pipeline_v2.stage_1_extractors`), so the scan is
    byte-identical to pre-S4a — explicit off stays a valid kill-switch
    forever."""
    return os.environ.get(APPROUTER_KEYLESS_ENV, "1").strip().lower() not in {
        "0", "false", "no", "off", "",
    }


def _app_router_suffixes() -> tuple[str, ...]:
    """The ``next-app-router`` page/route suffixes from the YAML convention
    (``/page.tsx`` … ``/route.js``). Falls back to nothing if the entry is
    absent — a packaging bug surfaces as "no rows", never a hardcoded list."""
    config = load_stack_yaml("filesystem-routing")
    entry = (config.get("stacks") or {}).get("next-app-router") or {}
    return tuple(str(s) for s in (entry.get("suffixes") or ()))


class AppRouterKeylessExtractor:
    """App-Router ``page``/``route`` files -> ``routes_index`` entries (S4-a).

    Implements the Stage-1 ``AnchorExtractor`` Protocol. Emits one candidate
    per (file, slug) carrying the explicit ``(pattern, method, file)`` route;
    the slug is the first static URL segment (the SAME slug the App-Router
    profile surfaces), so a same-name profile anchor merges rather than twins.
    """

    name = APPROUTER_SOURCE

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        if not approuter_keyless_enabled():
            return []
        suffixes = _app_router_suffixes()
        if not suffixes:
            return []

        # slug -> {file -> (pattern, method)}. One route per page/route file.
        by_slug: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
        for raw in ctx.tracked_files:
            p = posix(raw)
            # Gate on the YAML suffix set FIRST — this restricts to page/
            # route leaves (excludes layout/loading/error, exactly as the
            # stock next-app-router pass does) and cheaply rejects non-routes.
            if not any(p.endswith(suf.lstrip("/")) for suf in suffixes):
                continue
            if is_test_file(p):
                continue
            derived = derive_app_router_route(p)
            if derived is None:
                continue  # not under an App-Router root — honest non-match
            pattern, method = derived
            slug = _first_static_slug(pattern)
            if not slug:
                # A group-index / all-dynamic page (``app/(marketing)/page.tsx``
                # -> ``/``): no static segment to name a capability. Honest
                # skip — the same law the SPA/stock passes apply; the bulk of
                # the tree (named segments) is captured.
                continue
            by_slug[slug][p] = (pattern, method)

        out: list[AnchorCandidate] = []
        for slug in sorted(by_slug):
            files = by_slug[slug]
            paths = tuple(sorted(files))
            routes = tuple(
                (pat, meth, f) for f, (pat, meth) in sorted(files.items())
            )
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=paths,
                    source=APPROUTER_SOURCE,
                    confidence_self=min(
                        _CONF_BASE + _CONF_STEP * len(paths), _CONF_CAP,
                    ),
                    routes=routes,
                    rationale=(
                        f"app-router keyless slug '{slug}' derived from "
                        f"{len(paths)} page/route file(s)"
                    ),
                ),
            )
        return out


def _first_static_slug(pattern: str) -> str:
    """Slug of the first static (non-``:param``) URL segment, or ``""``."""
    for raw in pattern.split("/"):
        seg = raw.strip()
        if not seg or seg.startswith((":", "*")):
            continue
        slug = slugify(seg)
        if slug and not is_noise(slug):
            return slug
    return ""


__all__ = [
    "APPROUTER_KEYLESS_ENV",
    "APPROUTER_SOURCE",
    "AppRouterKeylessExtractor",
    "approuter_keyless_enabled",
]
