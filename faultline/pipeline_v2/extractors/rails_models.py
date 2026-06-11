"""RailsModelsExtractor — walk ``app/models/**/*.rb``.

Each ``class XYZ < ApplicationRecord`` (or ``< ActiveRecord::Base``)
yields one anchor whose slug is the singularized model name. The
extractor also reads ``has_many`` / ``belongs_to`` / ``has_one``
declarations and surfaces them in the rationale; Stage 2 can later
use them for association edges, but that wiring is outside this
extractor's contract (which only emits :class:`AnchorCandidate`).

We compile the class pattern once per repo and walk only files under
``app/models``. STI subclasses (``class Foo < Bar``) are NOT emitted —
the parent class anchor already covers them.

No LLM. No network. Read-only.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from faultline.pipeline_v2.extractors._rails import RailsPatternExtractor
from faultline.pipeline_v2.extractors._util import (
    is_noise,
    posix,
    read_text,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


_MODEL_PREFIX = "app/models/"


class _Compiled:
    """Compiled patterns + scalar config for the ``models`` section."""

    __slots__ = ("class_re", "assoc_res", "confidence")

    def __init__(self, config: dict) -> None:
        cfg = config.get("models") or {}
        if not isinstance(cfg, dict):
            cfg = {}

        self.confidence = float(cfg.get("confidence") or 0.85)
        self.class_re: re.Pattern[str] | None = None
        class_re_raw = cfg.get("class_pattern")
        if isinstance(class_re_raw, str):
            try:
                self.class_re = re.compile(class_re_raw)
            except re.error as exc:
                logger.warning("rails_models: bad class regex: %s", exc)

        assoc_cfg = cfg.get("association_patterns") or {}
        self.assoc_res: dict[str, re.Pattern[str]] = {}
        if isinstance(assoc_cfg, dict):
            for key, raw in assoc_cfg.items():
                if isinstance(raw, str):
                    try:
                        self.assoc_res[key] = re.compile(raw)
                    except re.error:
                        continue


class RailsModelsExtractor(RailsPatternExtractor):
    """ActiveRecord model files → feature anchors."""

    name = "rails-models"

    def compile_patterns(self, config: dict) -> _Compiled:
        return _Compiled(config)

    def collect(
        self, ctx: "ScanContext", compiled: _Compiled,
    ) -> dict[str, dict]:
        class_re = compiled.class_re
        if class_re is None:
            return {}

        # Walk tracked files under app/models/
        files = [
            posix(f) for f in ctx.tracked_files
            if posix(f).startswith(_MODEL_PREFIX) and f.endswith(".rb")
        ]
        # Skip the concerns base + base class file — they're support code,
        # not user-facing models.
        files = [f for f in files if not f.endswith("/application_record.rb")]

        # Group anchors by canonical slug (multiple files might define
        # the same conceptual model — e.g. concerns/users/* alongside
        # users.rb). Each unique class declaration adds one path.
        # slug → {paths: set, classnames: set, assoc_targets: set}
        buckets: dict[str, dict] = {}

        for path in files:
            text = read_text(ctx.repo_path / path)
            if not text:
                continue
            class_matches = class_re.findall(text)
            if not class_matches:
                # Could be a concern (module) — skip silently.
                continue
            # The regex's first capture group is the class name.
            for classname in class_matches:
                slug = slugify(classname)
                if not slug or is_noise(slug):
                    continue
                bucket = buckets.setdefault(
                    slug,
                    {"paths": set(), "classnames": set(), "assoc": set()},
                )
                bucket["paths"].add(path)
                bucket["classnames"].add(classname)

            # Capture associations once per file (not per class) — they
            # contribute provenance to whichever model the file declared.
            file_assoc: set[str] = set()
            for key, pattern in compiled.assoc_res.items():
                for m in pattern.findall(text):
                    if isinstance(m, str) and m:
                        file_assoc.add(f"{key}:{m}")
            if file_assoc and class_matches:
                # Attribute to the FIRST class declared in the file
                # (Rails convention: one class per file).
                slug = slugify(class_matches[0])
                if slug in buckets:
                    buckets[slug]["assoc"].update(file_assoc)

        return buckets

    def emit(
        self,
        ctx: "ScanContext",
        key: str,
        bucket: dict,
        compiled: _Compiled,
    ) -> AnchorCandidate | None:
        paths = tuple(sorted(bucket["paths"]))
        if not paths:
            return None
        classnames = sorted(bucket["classnames"])
        assoc_summary = (
            f" associations={sorted(bucket['assoc'])[:5]}"
            if bucket["assoc"] else ""
        )
        return AnchorCandidate(
            name=key,
            paths=paths,
            source=self.name,
            confidence_self=compiled.confidence,
            rationale=(
                f"Rails model {classnames[0]!r} "
                f"from {len(paths)} file(s){assoc_summary}"
            ),
        )


__all__ = ["RailsModelsExtractor"]
