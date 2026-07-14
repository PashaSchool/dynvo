"""B59 artifact-ink classifier — non-authorial "ink" file-class detector.

THE CLASS (twenty + cal.com panel-EM verdicts + cold-board census, 2026-07-13):
a large share of a board's product `loc` is non-authorial "ink" — locale
catalogs (twenty: 828K of ``.po`` front+server = 50.5% of the whole board;
cal.com: ``I18n`` 173,588 LOC locale JSON = tile #1), machine-generated client
schemas (``__generated__/`` / ``*.generated.*``), test DATA (``__mocks__/`` /
fixtures), and dev seeders. These files are members of real features (they live
under a feature's directory) but their LINE COUNT is not product story: a staffing
reader who sees "Twenty Front = 676K LOC" is being told a locale-cache size, not
a feature size.

This module is a DETERMINISTIC, STRUCTURAL file-class detector ($0, no LLM, no
network, no file I/O — path-string only). It answers ONE question:
:func:`classify_artifact` — "is this owned file's LOC artifact ink, and of which
class?". Stage 6.97 uses it as an ACCOUNTING partition: an owned artifact file's
LOC is reclassified out of the feature's product ``loc`` into a separate
``artifact_ink_loc`` counter (+ a ``scan_meta.artifact_ink`` lane aggregate). The
file stays a member — path_index / line coordinates / membership / flows /
user_flows are UNTOUCHED (accounting layer only; lines are coordinates).

DISCIPLINE (the SACRED anti-cases):

  * STRUCTURAL ONLY — path conventions (data, YAML source of truth) + a config
    basename blocklist. Never a feature-name vocabulary. Doubt -> NOT artifact.
  * CONFIG BLOCKLIST FIRST — a functional config/schema/manifest JSON is product
    even under a locale/gen dir (``packages/i18n/package.json`` is the known
    false-positive: it lives beside i18n but IS the workspace package manifest).
  * SEGMENT-ANCHORED — ``/mocks/`` matches a ``mocks`` directory segment, never a
    filename suffix (``sign-in-background-mock/foo.ts`` is a real product feature,
    NOT test data).
  * DOUBLE-COUNT GUARD — files the LOC census already zeroes (test code via
    ``is_test_path``, compiled-codegen via ``is_generated_path``, lockfiles /
    binaries via ``_is_excluded_name``) return ``None``: they contribute 0 LOC
    anyway, so classifying them as ink would be meaningless.

Kill-switch ``FAULTLINE_ARTIFACT_INK_LANE`` (default OFF) → Stage 6.97 leaves
``loc`` exactly as today and scans are byte-identical to main. ``classify_artifact``
itself is pure and side-effect free; the FLAG gates only whether the drain runs.
"""

from __future__ import annotations

import fnmatch
import os
from functools import lru_cache
from typing import Any, NamedTuple

from faultline.pipeline_v2.data import load_yaml
from faultline.pipeline_v2.stage_6_9_test_strip import is_test_path
from faultline.pipeline_v2.stage_6_9b_generated_strip import is_generated_path
from faultline.pipeline_v2.stage_6_97_feature_loc import _is_excluded_name

ARTIFACT_INK_ENV = "FAULTLINE_ARTIFACT_INK_LANE"

#: The file-class labels :func:`classify_artifact` can return.
ARTIFACT_CLASSES = ("locale", "generated", "testing", "seed")

#: Conventions data (data, not code — YAML source of truth).
_CONVENTIONS_FILE = "artifact_conventions.yaml"

__all__ = [
    "ARTIFACT_CLASSES",
    "ARTIFACT_INK_ENV",
    "artifact_ink_enabled",
    "classify_artifact",
    "load_artifact_conventions",
]


class _Conventions(NamedTuple):
    """Parsed, normalized artifact-ink conventions (all lowercased)."""

    config_basenames: frozenset[str]
    config_patterns: tuple[str, ...]
    locale_exts: frozenset[str]
    locale_json_segments: frozenset[str]
    generated_segments: frozenset[str]
    generated_globs: tuple[str, ...]
    testing_segments: frozenset[str]
    testing_prefixes: tuple[str, ...]
    seed_segments: frozenset[str]


def artifact_ink_enabled() -> bool:
    """Default OFF; ``FAULTLINE_ARTIFACT_INK_LANE=1`` turns the drain on.
    OFF is byte-identical to main (the kill-switch law)."""
    return os.environ.get(ARTIFACT_INK_ENV, "0").strip().lower() in {
        "1", "true",
    }


def _bare_segments(tokens: Any) -> frozenset[str]:
    """``["/locales/", …]`` → ``{"locales", …}`` (slash-stripped, lowercased)."""
    out: set[str] = set()
    for t in tokens or []:
        s = str(t).strip().strip("/").lower()
        if s:
            out.add(s)
    return frozenset(out)


def _lower_tuple(tokens: Any) -> tuple[str, ...]:
    return tuple(
        str(t).strip().lower() for t in (tokens or [])
        if str(t).strip()
    )


@lru_cache(maxsize=1)
def load_artifact_conventions() -> _Conventions:
    """Load + normalize the packaged ``artifact_conventions.yaml`` (mirrors
    ``ws_blob_domain_drain._containers``: ``load_yaml`` + ``@lru_cache``)."""
    data = load_yaml(_CONVENTIONS_FILE)
    locale = data.get("locale") or {}
    generated = data.get("generated") or {}
    testing = data.get("testing") or {}
    seed = data.get("seed") or {}
    return _Conventions(
        config_basenames=frozenset(
            str(b).strip().lower()
            for b in (data.get("config_blocklist_basenames") or [])
            if str(b).strip()
        ),
        config_patterns=_lower_tuple(data.get("config_blocklist_patterns")),
        locale_exts=frozenset(
            str(e).strip().lower()
            for e in (locale.get("extensions") or [])
            if str(e).strip()
        ),
        locale_json_segments=_bare_segments(locale.get("json_under_segments")),
        generated_segments=_bare_segments(generated.get("dir_segments")),
        generated_globs=_lower_tuple(generated.get("filename_globs")),
        testing_segments=_bare_segments(testing.get("dir_segments")),
        testing_prefixes=_lower_tuple(testing.get("path_prefixes")),
        seed_segments=_bare_segments(seed.get("dir_segments")),
    )


def classify_artifact(rel_path: str) -> str | None:
    """Classify *rel_path* as one of :data:`ARTIFACT_CLASSES` or ``None``.

    Pure, deterministic, no I/O — matches on the lowercased, forward-slash
    path only. Precedence (first hit wins):

      1. CONFIG BLOCKLIST — a config/manifest/schema JSON is product even under
         a locale/gen dir (SACRED). ``None``.
      2. DOUBLE-COUNT GUARD — test code / compiled-codegen / lockfile+binary
         files the census already zeroes. ``None``.
      3. ``locale`` — ``*.po`` / ``*.pot``, or ``*.json`` under a
         ``locales`` / ``messages`` / ``translations`` / ``lang`` dir segment.
      4. ``generated`` — a ``__generated__`` / ``generated`` /
         ``generated-metadata`` dir segment, or a ``*.generated.*`` basename.
      5. ``testing`` — a ``__mocks__`` / ``__fixtures__`` / ``mocks`` dir
         segment, or a ``testing/mock…`` path prefix.
      6. ``seed`` — a ``seeds`` / ``dev-seeder`` / ``seed-data`` dir segment.
    """
    if not rel_path or not isinstance(rel_path, str):
        return None
    conv = load_artifact_conventions()
    rel = rel_path.replace("\\", "/").strip("/").lower()
    if not rel:
        return None
    segs = rel.split("/")
    base = segs[-1]
    dir_segs = frozenset(segs[:-1])  # directory segments (exclude the filename)
    ext = os.path.splitext(base)[1]

    # (1) config blocklist — SACRED, highest priority.
    if base in conv.config_basenames:
        return None
    if any(fnmatch.fnmatch(base, pat) for pat in conv.config_patterns):
        return None

    # (2) double-count guard — already zeroed by the LOC census upstream.
    if is_test_path(rel) or is_generated_path(rel) or _is_excluded_name(rel):
        return None

    # (3) locale.
    if ext in conv.locale_exts:
        return "locale"
    if ext == ".json" and (dir_segs & conv.locale_json_segments):
        return "locale"

    # (4) generated.
    if dir_segs & conv.generated_segments:
        return "generated"
    if any(fnmatch.fnmatch(base, g) for g in conv.generated_globs):
        return "generated"

    # (5) testing DATA (test CODE already left via the guard).
    if dir_segs & conv.testing_segments:
        return "testing"
    wrapped = "/" + rel
    if any(pref in wrapped for pref in conv.testing_prefixes):
        return "testing"

    # (6) seed.
    if dir_segs & conv.seed_segments:
        return "seed"

    return None
