"""Line-grain coordinate reverse-lookup (B11 — the coordinates contract).

Operator doctrine (2026-07-09): *line ranges are the COORDINATES of a
feature/flow.* Two consumers need them:

  1. **Analytics** — an error/event on ``(file, line)`` must resolve to the
     affected dev flow / dev feature / user flow / product feature.
  2. **PR comments** — a diff touching lines must highlight which product
     features / user flows were affected.

The emitted ``path_index`` is FILE-grain (``path -> {feature_uuid,
flow_uuids}``) — it cannot tell two flows sharing one file apart. This module
adds the LINE-grain reverse lookup on top of the already-emitted flow span
coordinates (``flows[].line_ranges`` — one ``{path, start_line, end_line}``
per covered span), and the forward direction (an entity's coordinate set) at
EVERY level. It is a pure read-only helper over the emitted scan shape — it
mutates nothing and is not wired into the scan pipeline (consumers build on
it), so it is strictly additive.

The mapping is BIDIRECTIONAL and mutual (one graph, two entry points):

  * REVERSE — :meth:`LineCoordinateIndex.lookup` ``(path, line)`` → the full
    chain ``{flows, dev_features, user_flows, product_features}`` covering it.
  * FORWARD — :meth:`coordinate_set` returns the ``(path, start, end)`` spans
    of any flow / user_flow / dev_feature / product_feature (a UF's set is the
    union of its member flows' spans; a PF's set is the union of the spans of
    the flows its member dev features own).

Works on both emitted dicts (the common consumer case) and pydantic objects
(via attribute fallback), mirroring ``stage_6_97b_uf_loc.flow_owned_spans``.
Deterministic: every returned collection is sorted. $0, no LLM, no network.
"""

from __future__ import annotations

from typing import Any

__all__ = ["LineCoordinateIndex", "build_line_coordinate_index"]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _flow_spans(flow: Any) -> list[tuple[str, int, int]]:
    """A flow's coordinate spans ``(path, start, end)`` from ``line_ranges``.

    Falls back to the raw ``nodes`` (``file`` + 2-int ``lines``) when
    ``line_ranges`` is empty (a flow produced before Phase-5 span projection).
    """
    out: list[tuple[str, int, int]] = []
    for lr in (_get(flow, "line_ranges") or []):
        path = _get(lr, "path")
        s = _get(lr, "start_line")
        e = _get(lr, "end_line")
        if path and isinstance(s, int) and isinstance(e, int):
            out.append((str(path), s, e) if s <= e else (str(path), e, s))
    if out:
        return out
    for nd in (_get(flow, "nodes") or []):
        path = _get(nd, "file")
        ln = _get(nd, "lines")
        if (
            path
            and isinstance(ln, (list, tuple))
            and len(ln) == 2
            and all(isinstance(x, int) for x in ln)
        ):
            s, e = int(ln[0]), int(ln[1])
            out.append((str(path), s, e) if s <= e else (str(path), e, s))
    return out


class LineCoordinateIndex:
    """Line-grain reverse/forward coordinate index over one scan.

    Construct via :func:`build_line_coordinate_index`. All lookups are O(spans
    on the file); the corpus's per-file span counts are small.
    """

    def __init__(
        self,
        flows: list[Any],
        dev_features: list[Any],
        product_features: list[Any],
        user_flows: list[Any],
    ) -> None:
        self._flows = list(flows or [])
        self._dev_features = list(dev_features or [])
        self._product_features = list(product_features or [])
        self._user_flows = list(user_flows or [])

        # flow key (uuid preferred, else name) → flow
        self._flow_by_key: dict[str, Any] = {}
        for fl in self._flows:
            for k in (_get(fl, "uuid"), _get(fl, "name")):
                if k and str(k) not in self._flow_by_key:
                    self._flow_by_key[str(k)] = fl
        # dev feature name → dev (primary_feature points at the dev NAME)
        self._dev_by_name: dict[str, Any] = {}
        for d in self._dev_features:
            n = _get(d, "name")
            if n:
                self._dev_by_name.setdefault(str(n), d)
        # per-file span table: path → list of (start, end, flow)
        self._by_file: dict[str, list[tuple[int, int, Any]]] = {}
        for fl in self._flows:
            for path, s, e in _flow_spans(fl):
                self._by_file.setdefault(path, []).append((s, e, fl))

    # ── reverse: (path, line) → chain ────────────────────────────────
    def lookup(self, path: str, line: int) -> dict[str, list[str]]:
        """Return the full entity chain covering ``(path, line)``.

        Keys ``flows`` / ``dev_features`` / ``user_flows`` /
        ``product_features`` each map to a SORTED list of identifiers (flow
        names; dev-feature names; user-flow uuids-or-names; product-feature
        keys). Empty lists when nothing covers the coordinate.
        """
        hit_flows: list[Any] = [
            fl for (s, e, fl) in self._by_file.get(path, [])
            if s <= line <= e
        ]
        flow_names: set[str] = set()
        dev_names: set[str] = set()
        uf_ids: set[str] = set()
        pf_keys: set[str] = set()
        for fl in hit_flows:
            nm = _get(fl, "name")
            if nm:
                flow_names.add(str(nm))
            dev = _get(fl, "primary_feature")
            if dev:
                dev_names.add(str(dev))
            uf = _get(fl, "user_flow_id")
            if uf:
                uf_ids.add(str(uf))
        # dev → product feature (climb the two-layer link)
        for dn in list(dev_names):
            dev = self._dev_by_name.get(dn)
            pf = _get(dev, "product_feature_id") if dev is not None else None
            if pf:
                pf_keys.add(str(pf))
        return {
            "flows": sorted(flow_names),
            "dev_features": sorted(dev_names),
            "user_flows": sorted(uf_ids),
            "product_features": sorted(pf_keys),
        }

    # ── forward: entity → coordinate set ─────────────────────────────
    def flow_coordinates(self, flow_key: str) -> list[tuple[str, int, int]]:
        """The ``(path, start, end)`` spans of one flow (by uuid or name)."""
        fl = self._flow_by_key.get(str(flow_key))
        return sorted(_flow_spans(fl)) if fl is not None else []

    def user_flow_coordinates(self, uf: Any) -> list[tuple[str, int, int]]:
        """A user flow's coordinate set = the UNION (sorted, de-duplicated) of
        its member flows' spans. Accepts a UserFlow object/dict."""
        spans: set[tuple[str, int, int]] = set()
        for mid in (_get(uf, "member_flow_ids") or []):
            for sp in self.flow_coordinates(str(mid)):
                spans.add(sp)
        return sorted(spans)

    def product_feature_coordinates(
        self, pf_key: str,
    ) -> list[tuple[str, int, int]]:
        """A product feature's coordinate set = the UNION of the spans of every
        flow owned by a dev feature whose ``product_feature_id`` is ``pf_key``
        (dev features carry the two-layer link; flows carry
        ``primary_feature`` → dev name)."""
        dev_names = {
            str(_get(d, "name"))
            for d in self._dev_features
            if str(_get(d, "product_feature_id") or "") == str(pf_key)
            and _get(d, "name")
        }
        spans: set[tuple[str, int, int]] = set()
        for fl in self._flows:
            if str(_get(fl, "primary_feature") or "") in dev_names:
                for sp in _flow_spans(fl):
                    spans.add(sp)
        return sorted(spans)

    def dev_feature_coordinates(
        self, dev_name: str,
    ) -> list[tuple[str, int, int]]:
        """A dev feature's coordinate set = the UNION of the spans of the flows
        it owns (``primary_feature`` == ``dev_name``)."""
        spans: set[tuple[str, int, int]] = set()
        for fl in self._flows:
            if str(_get(fl, "primary_feature") or "") == str(dev_name):
                for sp in _flow_spans(fl):
                    spans.add(sp)
        return sorted(spans)


def build_line_coordinate_index(scan: Any) -> LineCoordinateIndex:
    """Build a :class:`LineCoordinateIndex` from an emitted scan (dict or
    FeatureMap). Reads ``flows`` / ``developer_features`` / ``product_features``
    / ``user_flows`` — the top-level emitted arrays."""
    flows = _get(scan, "flows") or []
    devs = _get(scan, "developer_features")
    if devs is None:
        # FeatureMap / layered shape: derive from features[] by layer.
        feats = _get(scan, "features") or []
        devs = [f for f in feats if _get(f, "layer", "developer") == "developer"]
    prods = _get(scan, "product_features")
    if prods is None:
        feats = _get(scan, "features") or []
        prods = [f for f in feats if _get(f, "layer") == "product"]
    ufs = _get(scan, "user_flows") or []
    return LineCoordinateIndex(flows, devs, prods, ufs)
