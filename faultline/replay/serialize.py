"""Tagged JSON serializer for pipeline-v2 stage-input artifacts.

Replay v2 (deterministic-foundation WS1) persists a deep copy of every
stage's exact input as ``NN-stage-<name>-input.json`` next to the
existing output artifact. Those inputs are heterogeneous — pydantic
models (:class:`faultline.models.types.Feature`), plain dataclasses
(:class:`~faultline.pipeline_v2.stage_0_intake.ScanContext`,
``DeveloperFeature``, ``FeatureWithFlows``), tuples, sets, ``Path`` and
``datetime`` values, and ordinary JSON dicts. This module provides ONE
loss-aware round-trip:

    ``to_jsonable(obj)``   → JSON-safe tree with ``__type__`` tags
    ``from_jsonable(tree)`` → reconstructed Python objects

Design rules
============

* **Allowlist, not pickle.** ``__type__`` tags name a class as
  ``pydantic:<module>:<qualname>`` / ``dataclass:<module>:<qualname>``;
  the decoder only imports modules under the prefixes in
  ``_ALLOWED_MODULE_PREFIXES``. No arbitrary code paths, no pickle.
* **Determinism.** ``set`` / ``frozenset`` serialize as SORTED lists
  (sort key = the canonical JSON of each encoded element) so two runs
  of the same pipeline state produce byte-identical input artifacts.
  No wall-clock, no uuid4 — the encoder adds nothing that isn't in the
  object graph.
* **Service objects are excluded, never encoded.** Per-dataclass field
  exclusions (e.g. ``ScanContext.cache_backend``) drop live handles;
  the replay runner reconstructs them (see
  :mod:`faultline.replay.runner`). What is NOT captured is documented
  in ``faultline/replay/README.md`` — the identity-replay gate defines "good
  enough".
* **Pydantic round-trip** uses ``model_dump(mode="json")`` +
  ``model_validate`` — the same serialization the final FeatureMap
  writer uses, so anything that survives a scan JSON survives here.

No LLM. No network. Pure in-memory transforms.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
from datetime import datetime
from pathlib import Path, PosixPath, WindowsPath  # noqa: F401 (isinstance)
from typing import Any

from pydantic import BaseModel

__all__ = ["to_jsonable", "from_jsonable", "SerializationError"]


class SerializationError(TypeError):
    """An object in a stage input could not be encoded or decoded."""


# ── Allowlists ──────────────────────────────────────────────────────────

#: Modules the decoder is allowed to import classes from. Anything else
#: raises — stage inputs must never become a code-execution vector.
_ALLOWED_MODULE_PREFIXES: tuple[str, ...] = (
    "faultline.",
)

#: Dataclass fields that hold live service objects (open handles, lazy
#: git state, cache backends). They are DROPPED at encode time and the
#: decoder fills them with ``None``; the replay runner reconstructs
#: them explicitly. Keyed by ``<module>:<qualname>``.
_FIELD_EXCLUSIONS: dict[str, frozenset[str]] = {
    "faultline.pipeline_v2.stage_0_intake:ScanContext": frozenset(
        # ``shared_source`` (perf wave 2, R4) is a live per-run cache —
        # dropped like ``cache_backend`` so replayed stages get ``None``
        # and exercise their local-construction fallback.
        {"cache_backend", "shared_source"},
    ),
}


def _class_ref(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def _resolve_class(ref: str) -> type:
    module_name, _, qualname = ref.partition(":")
    if not any(module_name.startswith(p) for p in _ALLOWED_MODULE_PREFIXES):
        raise SerializationError(
            f"refusing to import {ref!r}: module not in the replay allowlist",
        )
    try:
        module = importlib.import_module(module_name)
        obj: Any = module
        for part in qualname.split("."):
            obj = getattr(obj, part)
    except (ImportError, AttributeError) as exc:
        raise SerializationError(f"cannot resolve {ref!r}: {exc}") from exc
    if not isinstance(obj, type):
        raise SerializationError(f"{ref!r} is not a class")
    return obj


# ── Encode ──────────────────────────────────────────────────────────────


def _canonical(value: Any) -> str:
    """Canonical JSON of an already-encoded element (set sort key)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def to_jsonable(obj: Any) -> Any:
    """Encode ``obj`` into a JSON-safe tree with ``__type__`` tags."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, Path):
        return {"__type__": "path", "value": str(obj)}
    if isinstance(obj, datetime):
        return {"__type__": "datetime", "value": obj.isoformat()}
    if isinstance(obj, BaseModel):
        return {
            "__type__": f"pydantic:{_class_ref(type(obj))}",
            "value": obj.model_dump(mode="json"),
        }
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        ref = _class_ref(type(obj))
        excluded = _FIELD_EXCLUSIONS.get(ref, frozenset())
        payload = {
            f.name: to_jsonable(getattr(obj, f.name))
            for f in dataclasses.fields(obj)
            if f.name not in excluded
        }
        return {"__type__": f"dataclass:{ref}", "value": payload}
    if isinstance(obj, tuple):
        return {"__type__": "tuple", "value": [to_jsonable(v) for v in obj]}
    if isinstance(obj, (set, frozenset)):
        encoded = sorted((to_jsonable(v) for v in obj), key=_canonical)
        kind = "frozenset" if isinstance(obj, frozenset) else "set"
        return {"__type__": kind, "value": encoded}
    if isinstance(obj, list):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        if all(isinstance(k, str) for k in obj):
            out = {k: to_jsonable(v) for k, v in obj.items()}
            # A plain dict that HAPPENS to carry a __type__ key would
            # collide with our tag protocol — wrap it.
            if "__type__" in out:
                return {"__type__": "dict-items",
                        "value": [[k, v] for k, v in out.items()]}
            return out
        return {
            "__type__": "dict-items",
            "value": [[to_jsonable(k), to_jsonable(v)] for k, v in obj.items()],
        }
    raise SerializationError(
        f"cannot encode {type(obj).__module__}.{type(obj).__qualname__} "
        f"for a stage-input artifact",
    )


# ── Decode ──────────────────────────────────────────────────────────────


def from_jsonable(tree: Any) -> Any:
    """Reconstruct the Python object graph encoded by :func:`to_jsonable`."""
    if tree is None or isinstance(tree, (bool, int, float, str)):
        return tree
    if isinstance(tree, list):
        return [from_jsonable(v) for v in tree]
    if isinstance(tree, dict):
        tag = tree.get("__type__")
        if tag is None:
            return {k: from_jsonable(v) for k, v in tree.items()}
        value = tree.get("value")
        if tag == "path":
            return Path(value)
        if tag == "datetime":
            return datetime.fromisoformat(value)
        if tag == "tuple":
            return tuple(from_jsonable(v) for v in value)
        if tag == "set":
            return {from_jsonable(v) for v in value}
        if tag == "frozenset":
            return frozenset(from_jsonable(v) for v in value)
        if tag == "dict-items":
            return {from_jsonable(k): from_jsonable(v) for k, v in value}
        if tag.startswith("pydantic:"):
            cls = _resolve_class(tag[len("pydantic:"):])
            if not issubclass(cls, BaseModel):
                raise SerializationError(f"{tag!r} is not a pydantic model")
            return cls.model_validate(value)
        if tag.startswith("dataclass:"):
            ref = tag[len("dataclass:"):]
            cls = _resolve_class(ref)
            if not dataclasses.is_dataclass(cls):
                raise SerializationError(f"{tag!r} is not a dataclass")
            kwargs = {k: from_jsonable(v) for k, v in value.items()}
            return cls(**kwargs)
        raise SerializationError(f"unknown __type__ tag {tag!r}")
    raise SerializationError(f"cannot decode node of type {type(tree)!r}")
