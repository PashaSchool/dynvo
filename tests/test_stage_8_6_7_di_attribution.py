"""Tests for Stage 8.6.7 — DI service attribution.

Builds a tiny on-disk fastify-like repo (the stage reads real files + the
packaged di-patterns.yaml). Verifies detection, the named-reference attribution,
the fan-in cap, anchor removal, the camel→kebab service map, and the env toggle.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from faultline.models.types import Feature
from faultline.pipeline_v2.stage_8_6_7_di_attribution import (
    _camel_to_kebab,
    attribute_di_services,
)

_WS = "[package] workspace anchor 'backend' from monorepo package 'backend'"
_ROUTE = "[route] route convention slug {0!r} derived from 1 routing file(s)"


def _feat(name, paths, *, description=None, layer="developer"):
    return Feature(
        name=name, description=description, paths=list(paths), authors=[],
        total_commits=0, bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc), health_score=100.0, layer=layer,
    )


def _write_repo(root, *, fastify=True, extra_routes=None):
    """A fastify backend: secret-router references server.services.secret;
    services/secret/* are the injected service files."""
    (root / "backend/src/server/routes/v1").mkdir(parents=True)
    (root / "backend/src/services/secret").mkdir(parents=True)
    (root / "backend").joinpath("package.json").write_text(json.dumps(
        {"name": "backend",
         "dependencies": {"fastify": "^4"} if fastify else {"express": "^4"}}))
    (root / "backend/src/server/routes/v1/secret-router.ts").write_text(
        "export const registerSecretRouter = async (server) => {\n"
        "  server.get('/secrets', () => server.services.secret.list());\n"
        "  server.post('/secrets', () => server.services.secret.create());\n"
        "};\n")
    (root / "backend/src/services/secret/secret-service.ts").write_text(
        "export const secretServiceFactory = () => ({ list(){}, create(){} });\n")
    (root / "backend/src/services/secret/secret-dal.ts").write_text("export const x = 1;\n")
    for rel, body in (extra_routes or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def _tracked(root):
    return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]


def _ctx(root):
    return SimpleNamespace(repo_path=str(root), tracked_files=_tracked(root))


def _fixture(root, **kw):
    _write_repo(root, **kw)
    anchor = _feat("backend", [
        "backend/src/services/secret/secret-service.ts",
        "backend/src/services/secret/secret-dal.ts",
    ], description=_WS)
    secret = _feat("secret", ["backend/src/server/routes/v1/secret-router.ts"],
                   description=_ROUTE.format("secret"))
    return _ctx(root), [anchor, secret], anchor, secret


def test_camel_to_kebab():
    assert _camel_to_kebab("auditLog") == "audit-log"
    assert _camel_to_kebab("secret") == "secret"
    assert _camel_to_kebab("identityAccessToken") == "identity-access-token"


def test_attributes_di_service_to_referencing_feature(tmp_path):
    ctx, feats, anchor, secret = _fixture(tmp_path)
    res = attribute_di_services(ctx, feats)
    p = res.as_telemetry()["patterns"][0]
    assert p["detected"] is True
    # the secret service files move to the 'secret' feature ...
    assert "backend/src/services/secret/secret-service.ts" in secret.paths
    assert "backend/src/services/secret/secret-dal.ts" in secret.paths
    # ... and off the platform anchor
    assert "backend/src/services/secret/secret-service.ts" not in anchor.paths
    assert "backend/src/services/secret/secret-dal.ts" not in anchor.paths
    assert res.files_moved == 2


def test_no_op_on_non_fastify(tmp_path):
    ctx, feats, anchor, secret = _fixture(tmp_path, fastify=False)
    res = attribute_di_services(ctx, feats)
    assert res.patterns[0].detected is False
    assert res.files_moved == 0
    # anchor keeps the services; feature unchanged
    assert "backend/src/services/secret/secret-service.ts" in anchor.paths


def test_fan_in_cap_keeps_shared_service_on_platform(tmp_path):
    # Realistic distribution: 9 single-owner services (fan-in 1) push P90 to the
    # floor (3); one SHARED service referenced by 5 distinct features (> 3) is
    # shared infra and must stay on the platform anchor.
    rd = tmp_path / "backend/src/server/routes/v1"; rd.mkdir(parents=True)
    (tmp_path / "backend/package.json").write_text(
        json.dumps({"name": "backend", "dependencies": {"fastify": "^4"}}))
    anchor_paths, feats = [], []
    # 9 single-owner: svcN referenced only by rN
    for i in range(9):
        (tmp_path / f"backend/src/services/svc{i}").mkdir(parents=True)
        sf = f"backend/src/services/svc{i}/svc{i}-service.ts"
        (tmp_path / sf).write_text("export const x = 1;\n")
        anchor_paths.append(sf)
        (rd / f"r{i}-router.ts").write_text(f"export const r{i} = (server) => server.services.svc{i}.go();\n")
        feats.append(_feat(f"r{i}", [f"backend/src/server/routes/v1/r{i}-router.ts"],
                           description=_ROUTE.format(f"r{i}")))
    # 1 shared: 'common' referenced by 5 distinct features
    (tmp_path / "backend/src/services/common").mkdir(parents=True)
    sf = "backend/src/services/common/common-service.ts"
    (tmp_path / sf).write_text("export const x = 1;\n")
    anchor_paths.append(sf)
    for i in range(5):
        (rd / f"c{i}-router.ts").write_text(f"export const c{i} = (server) => server.services.common.go();\n")
        feats.append(_feat(f"c{i}", [f"backend/src/server/routes/v1/c{i}-router.ts"],
                           description=_ROUTE.format(f"c{i}")))
    feats.insert(0, _feat("backend", anchor_paths, description=_WS))

    res = attribute_di_services(_ctx(tmp_path), feats)
    p = res.patterns[0]
    assert p.fan_in_threshold == 3  # P90 of mostly-1 distribution → floor
    # the shared 'common' service (fan-in 5 > 3) stays on the anchor ...
    assert sf in feats[0].paths
    # ... while each single-owner svcN moved to its rN feature
    moved_single = sum(
        1 for i in range(9)
        if f"backend/src/services/svc{i}/svc{i}-service.ts"
        in next(f.paths for f in feats if f.name == f"r{i}")
    )
    assert moved_single == 9


def test_disabled_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAULTLINE_STAGE_8_6_7_DI_ATTRIBUTION", "0")
    ctx, feats, anchor, secret = _fixture(tmp_path)
    res = attribute_di_services(ctx, feats)
    assert res.enabled is False
    assert res.files_moved == 0
    assert "backend/src/services/secret/secret-service.ts" in anchor.paths


def test_no_anchor_is_noop(tmp_path):
    _write_repo(tmp_path)
    secret = _feat("secret", ["backend/src/server/routes/v1/secret-router.ts"],
                   description=_ROUTE.format("secret"))
    res = attribute_di_services(_ctx(tmp_path), [secret])
    assert res.files_moved == 0
