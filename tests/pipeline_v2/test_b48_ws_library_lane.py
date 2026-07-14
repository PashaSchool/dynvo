"""B48 — ws-library / name-dep transport lane (FAULTLINE_WS_LIBRARY_LANE).

Mechanism: the corroboration-free extension of the S2 prong. A
broadly-imported (inf>=5, inu>=3) zero-surface (no route/page, not
nav-confirmed) ws-package lanes when it imports <=1 in-repo unit
(S2 library) OR is NAMED after its own external dependency family
(S1 name-dep transport, dou fan-out waived). Candidates ride the
B19/B22 transport-handoff channel (``transport_candidates``) so
journeys re-home before the PF lanes — never mint-time laning.

Fixtures are neutral mini-monorepos on tmp_path: the SHAPE (import
direction + surface), not the name, carries the signal — the whole
point of B48 is that the name vocabulary does NOT decide. The
end-to-end journey-conservation is exercised by the real-repo keyless
census + the shared transport_handoff_b22 suite (B48 reuses that
machinery verbatim).
"""

from __future__ import annotations

import json
from pathlib import Path

from faultline.pipeline_v2.technology_instruments import (
    WS_LIBRARY_LANE_ENV,
    detect_technology_instruments,
    ws_library_lane_enabled,
)

ENV = WS_LIBRARY_LANE_ENV


def _write(repo: Path, rel: str, text: str = "") -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return rel


def _manifest(repo: Path, rel_dir: str, name: str, *,
              deps: dict | None = None, private: bool | None = None,
              bin_entry: str | None = None) -> str:
    doc: dict = {"name": name}
    if deps:
        doc["dependencies"] = deps
    if private is not None:
        doc["private"] = private
    if bin_entry:
        doc["bin"] = {name.split("/")[-1]: bin_entry}
    rel = f"{rel_dir}/package.json" if rel_dir else "package.json"
    return _write(repo, rel, json.dumps(doc))


def _consumers(repo: Path, spec: str) -> list[str]:
    """5 importer files across 3 app units → inf=5, inu=3."""
    out = []
    for unit, files in (("web", ("a", "b", "c")),
                        ("admin", ("x",)), ("mobile", ("y",))):
        out.append(_manifest(repo, f"apps/{unit}", f"@acme/{unit}",
                             deps={"react": "18"}, private=True))
        for fn in files:
            out.append(_write(repo, f"apps/{unit}/src/{fn}.ts",
                              f'import {{ Thing }} from "{spec}";\n'))
    return out


def _library_repo(repo: Path, *, extra: list[str] | None = None) -> list[str]:
    """``packages/widgetkit`` — a generically-named UI library imported by
    3 apps, importing NOTHING in-repo (dou=0). Matches no dep token / infra
    noun / UI-vocab key, so the existing S2 prong misses it; only B48 lanes.
    """
    tracked = [
        _manifest(repo, "", "root", private=True),
        _manifest(repo, "packages/widgetkit", "widgetkit"),
        _write(repo, "packages/widgetkit/src/index.ts",
               'export * from "./button";\nexport * from "./input";\n'),
        _write(repo, "packages/widgetkit/src/button.ts",
               "export const Button = () => null;\n"),
        _write(repo, "packages/widgetkit/src/input.ts",
               "export const Input = () => null;\n"),
    ]
    tracked += _consumers(repo, "widgetkit")
    tracked += (extra or [])
    return tracked


def _detect(repo, tracked, **kw):
    return detect_technology_instruments(repo, tracked, kw.pop("routes", []),
                                         **kw)


# ── flag wiring ──────────────────────────────────────────────────────────


def test_flag_default_on(monkeypatch):
    # B62 flip: default ON (KEY_SCHEMA 29). Unset ⇒ enabled; X=0 disables.
    monkeypatch.delenv(ENV, raising=False)
    assert ws_library_lane_enabled() is True
    monkeypatch.setenv(ENV, "0")
    assert ws_library_lane_enabled() is False


def test_flag_off_explicit(monkeypatch):
    for v in ("0", "false", "False", ""):
        monkeypatch.setenv(ENV, v)
        assert ws_library_lane_enabled() is False


def test_flag_on(monkeypatch):
    for v in ("1", "true", "True"):
        monkeypatch.setenv(ENV, v)
        assert ws_library_lane_enabled() is True


# ── S2 library prong (the core B48 win) ──────────────────────────────────


def test_off_is_byte_noop(tmp_path: Path, monkeypatch):
    """Flag forced OFF (X=0; default ON post-B62): the generic library
    MINTS — no transport_candidates key, no b48 telemetry (kill-switch)."""
    monkeypatch.setenv(ENV, "0")
    tele = _detect(tmp_path, _library_repo(tmp_path))
    assert "packages/widgetkit" not in tele.get("instruments", {})
    assert "transport_candidates" not in tele
    assert "b48_library_candidates" not in tele


def test_generic_library_lanes_on(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    tele = _detect(tmp_path, _library_repo(tmp_path))
    tc = tele.get("transport_candidates") or {}
    assert tc.get("packages/widgetkit") == "B48:library"
    # marked, NOT laned at mint (must ride the handoff for conservation)
    assert "packages/widgetkit" not in tele.get("instruments", {})
    assert "packages/widgetkit" not in tele.get("dirs", [])


# ── S1 name-dep transport prong (dou waived) ─────────────────────────────


def _namedep_repo(tmp_path: Path) -> list[str]:
    """``packages/trpc`` — named after its own ``@trpc/*`` dep, broadly
    imported, and fanning OUT into two domain packages (dou=2). The S2
    library prong fails (dou>1); only the name-dep prong (waived) lanes."""
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/domain-a", "@acme/domain-a"),
        _write(tmp_path, "packages/domain-a/src/i.ts", "export const A = 1;\n"),
        _manifest(tmp_path, "packages/domain-b", "@acme/domain-b"),
        _write(tmp_path, "packages/domain-b/src/i.ts", "export const B = 1;\n"),
        _manifest(tmp_path, "packages/trpc", "trpc",
                  deps={"@trpc/server": "10"}),
        _write(tmp_path, "packages/trpc/src/index.ts",
               'import { A } from "@acme/domain-a";\n'
               'import { B } from "@acme/domain-b";\n'
               'import { initTRPC } from "@trpc/server";\n'
               "export const router = { A, B, initTRPC };\n"),
        _write(tmp_path, "packages/trpc/src/util.ts",
               "export const u = 1;\n"),
    ]
    tracked += _consumers(tmp_path, "trpc")
    return tracked


def test_name_dep_transport_lanes_on(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(ENV, "1")
    tele = _detect(tmp_path, _namedep_repo(tmp_path))
    tc = tele.get("transport_candidates") or {}
    assert tc.get("packages/trpc") == "B48:name-dep"


def test_name_dep_off_is_noop(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(ENV, "0")  # default ON post-B62; pin OFF explicitly
    tele = _detect(tmp_path, _namedep_repo(tmp_path))
    assert "packages/trpc" not in (tele.get("transport_candidates") or {})
    assert "packages/trpc" not in tele.get("instruments", {})


# ── anti-cases (must stay product even with the flag ON) ─────────────────


def test_integration_leaf_survives(tmp_path: Path, monkeypatch):
    """An integration named after a vendor dep but imported by NOBODY
    (inf<5) never lanes — name-dep still requires import breadth (the
    binding integration=own-PF doctrine; twenty-zapier inf=0)."""
    monkeypatch.setenv(ENV, "1")
    # A real leaf integration carries a body (twenty-zapier: 15 src files) —
    # not config-only; the point is inf=0 (nobody imports it).
    tracked = _library_repo(tmp_path, extra=[
        _manifest(tmp_path, "packages/zapier", "zapier",
                  deps={"zapier-platform-core": "15"}),
        _write(tmp_path, "packages/zapier/src/index.ts",
               'import z from "zapier-platform-core";\n'
               'import { triggers } from "./triggers";\n'
               'import { creates } from "./creates";\n'
               "export default { z, triggers, creates };\n"),
        _write(tmp_path, "packages/zapier/src/triggers.ts",
               "export const triggers = [];\n"),
        _write(tmp_path, "packages/zapier/src/creates.ts",
               "export const creates = [];\n"),
    ])
    tele = _detect(tmp_path, tracked)
    tc = tele.get("transport_candidates") or {}
    assert "packages/zapier" not in tc
    assert "packages/zapier" not in tele.get("instruments", {})


def test_domain_core_survives(tmp_path: Path, monkeypatch):
    """A broadly-imported package that imports MANY in-repo units (dou>1)
    and matches no dep name is a domain core, not a library — protected by
    the dou guard (documenso packages/lib)."""
    monkeypatch.setenv(ENV, "1")
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/domain-a", "@acme/domain-a"),
        _write(tmp_path, "packages/domain-a/src/i.ts", "export const A = 1;\n"),
        _manifest(tmp_path, "packages/domain-b", "@acme/domain-b"),
        _write(tmp_path, "packages/domain-b/src/i.ts", "export const B = 1;\n"),
        _manifest(tmp_path, "packages/domain-c", "@acme/domain-c"),
        _write(tmp_path, "packages/domain-c/src/i.ts", "export const C = 1;\n"),
        _manifest(tmp_path, "packages/core", "core"),
        _write(tmp_path, "packages/core/src/index.ts",
               'import { A } from "@acme/domain-a";\n'
               'import { B } from "@acme/domain-b";\n'
               'import { C } from "@acme/domain-c";\n'
               "export const core = { A, B, C };\n"),
        _write(tmp_path, "packages/core/src/more.ts", "export const m = 1;\n"),
    ]
    tracked += _consumers(tmp_path, "core")
    tele = _detect(tmp_path, tracked)
    assert "packages/core" not in (tele.get("transport_candidates") or {})
    assert "packages/core" not in tele.get("instruments", {})


def test_route_surface_survives(tmp_path: Path, monkeypatch):
    """The HARD S3: a ws-pkg with any route/page surface is never a
    candidate (cal.com Event Types class)."""
    monkeypatch.setenv(ENV, "1")
    tracked = _library_repo(tmp_path)
    routes = [{"file": "packages/widgetkit/src/index.ts", "pattern": "/w",
               "method": "PAGE"}]
    tele = _detect(tmp_path, tracked, routes=routes)
    assert "packages/widgetkit" not in (tele.get("transport_candidates") or {})
    assert tele["vetoed"].get("packages/widgetkit") == "route_surface"


def test_published_cli_survives(tmp_path: Path, monkeypatch):
    """A published CLI (bin, not private) stays product even ON."""
    monkeypatch.setenv(ENV, "1")
    tracked = _library_repo(tmp_path)
    # give widgetkit a bin so it becomes a published CLI
    _manifest(tmp_path, "packages/widgetkit", "widgetkit",
              bin_entry="dist/cli.js")
    tele = _detect(tmp_path, tracked)
    assert "packages/widgetkit" not in (tele.get("transport_candidates") or {})
    assert tele["vetoed"].get("packages/widgetkit") == "published_cli"


def test_nav_confirmed_survives(tmp_path: Path, monkeypatch):
    """S3 (nav): a library that IS a nav-declared anchor prefix (or sits
    inside one) is the author's product area — never a lane."""
    monkeypatch.setenv(ENV, "1")
    tele = _detect(tmp_path, _library_repo(tmp_path),
                   nav_prefixes=["packages/widgetkit"])
    assert "packages/widgetkit" not in (tele.get("transport_candidates") or {})


def test_nav_descendant_echo_does_not_block(tmp_path: Path, monkeypatch):
    """A nav-confirmed anchor DEEP INSIDE the unit is the attribution echo
    (cal.com packages/trpc router dirs named after the nav features they
    serve) — it must NOT self-veto the lane (forensic 2026-07-12)."""
    monkeypatch.setenv(ENV, "1")
    tele = _detect(tmp_path, _library_repo(tmp_path),
                   nav_prefixes=["packages/widgetkit/src"])
    tc = tele.get("transport_candidates") or {}
    assert tc.get("packages/widgetkit") == "B48:library"


def test_nested_family_survives(tmp_path: Path, monkeypatch):
    """A nested ws-pkg (packages/embeds/embed-core class) keeps the
    nested_family veto — product SDK families are never laned."""
    monkeypatch.setenv(ENV, "1")
    tracked = [
        _manifest(tmp_path, "", "root", private=True),
        _manifest(tmp_path, "packages/embeds/embed-core", "@acme/embed-core"),
        _write(tmp_path, "packages/embeds/embed-core/src/index.ts",
               "export const embed = 1;\n"),
    ]
    tracked += _consumers(tmp_path, "@acme/embed-core")
    tele = _detect(tmp_path, tracked)
    assert "packages/embeds/embed-core" not in (
        tele.get("transport_candidates") or {})
    assert tele["vetoed"].get("packages/embeds/embed-core") == "nested_family"
