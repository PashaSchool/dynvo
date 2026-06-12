"""Guard the file-membership benchmark scorer.

Runs the membership SCORER (eval/membership/score_membership.py) on a
tiny synthetic fixture — NOT on any live repo — so CI catches
regressions in the harness without external state. The hand-curated
ground-truth files live in eval/membership/<slug>/ground-truth.yaml
and are validated for shape here too.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
import yaml

MEMBERSHIP_DIR = (
    Path(__file__).resolve().parents[2] / "eval" / "membership"
)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"membership_{name}", MEMBERSHIP_DIR / f"{name}.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


score_mod = _load("score_membership")


TRUTH = {
    "features": [
        {
            "name": "cases",
            "files": [
                "backend/routers/cases.py",
                "backend/services/case_autocreator.py",
                "frontend/src/api/cases.ts",
            ],
        },
        {
            "name": "webhooks",
            "files": [
                "backend/services/webhook_dispatcher.py",
                "backend/models/webhook.py",
            ],
        },
        {"name": "ghost", "files": ["backend/services/nowhere.py"]},
    ]
}

SCAN = {
    "developer_features": [
        {
            # 2/3 cases files + 1 stray -> P=2/3, R=2/3
            "name": "case-management",
            "paths": [
                "backend/routers/cases.py",
                "backend/services/case_autocreator.py",
                "backend/routers/other.py",
            ],
        },
        {
            # junk drawer overlapping both truths; Jaccard must prefer
            # the focused webhooks match over absorbing cases too
            "name": "webhooks",
            "paths": [
                "backend/services/webhook_dispatcher.py",
                "backend/models/webhook.py",
            ],
        },
    ],
    "product_features": [],
}


def test_scorer_end_to_end(tmp_path: Path) -> None:
    truth_path = tmp_path / "truth.yaml"
    truth_path.write_text(yaml.safe_dump(TRUTH))
    scan_path = tmp_path / "scan.json"
    scan_path.write_text(json.dumps(SCAN))

    truth = score_mod.load_truth(truth_path)
    raw = score_mod.load_scan_features(SCAN, "dev")
    universe = {f for fs in truth.values() for f in fs}
    feats = [
        (n, score_mod.expand_feature_paths(p, None, universe))
        for n, p in raw
    ]
    result = score_mod.score(truth, feats)

    assert result["n_truth_features"] == 3
    assert result["n_matched"] == 2
    assert result["unmatched_truth_features"] == ["ghost"]

    rows = {r["truth_feature"]: r for r in result["per_feature"]}
    assert rows["webhooks"]["matched_scan_feature"] == "webhooks"
    assert rows["webhooks"]["precision"] == 1.0
    assert rows["webhooks"]["recall"] == 1.0
    assert rows["cases"]["matched_scan_feature"] == "case-management"
    assert rows["cases"]["intersection"] == 2
    assert rows["cases"]["recall"] == pytest.approx(2 / 3, abs=1e-4)

    # micro: inter=4, scan=5, truth=6 ; macro over 3 incl. ghost=0
    assert result["micro"]["precision"] == pytest.approx(4 / 5, abs=1e-4)
    assert result["micro"]["recall"] == pytest.approx(4 / 6, abs=1e-4)
    assert result["macro"]["recall"] == pytest.approx(
        (2 / 3 + 1.0 + 0.0) / 3, abs=1e-4
    )


def test_scorer_directory_expansion_against_repo(tmp_path: Path) -> None:
    """A directory path entry expands to tracked files under it."""
    tracked = [
        "backend/routers/cases.py",
        "backend/services/case_autocreator.py",
        "backend/services/case_lifecycle.py",
    ]
    files = score_mod.expand_feature_paths(
        ["backend/services", "backend/routers/cases.py"], tracked, set()
    )
    assert files == {
        "backend/routers/cases.py",
        "backend/services/case_autocreator.py",
        "backend/services/case_lifecycle.py",
    }


def test_scorer_cli_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    truth_path = tmp_path / "truth.yaml"
    truth_path.write_text(yaml.safe_dump(TRUTH))
    scan_path = tmp_path / "scan.json"
    scan_path.write_text(json.dumps(SCAN))

    rc = score_mod.main([str(scan_path), str(truth_path), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["micro"]["recall"] == pytest.approx(4 / 6, abs=1e-4)
    assert out["unmatched_truth_features"] == ["ghost"]


@pytest.mark.parametrize("slug", ["documenso", "inbox-zero"])
def test_curated_ground_truth_shape(slug: str) -> None:
    """Committed hand-curated truth files parse and are well-formed."""
    path = MEMBERSHIP_DIR / slug / "ground-truth.yaml"
    truth = score_mod.load_truth(path)
    assert 8 <= len(truth) <= 12
    doc = yaml.safe_load(path.read_text())
    for feat in doc["features"]:
        assert feat["notes"], f"{feat['name']} missing curation evidence"
        files = feat["files"]
        assert files == sorted(files), f"{feat['name']} files not sorted"
        assert len(files) == len(set(files))
        assert len(files) >= 4, f"{feat['name']} suspiciously small"
        # name-quality reference labels (see score_names.py)
        display = feat["display_name"]
        assert isinstance(display, str) and display.strip(), (
            f"{feat['name']} missing display_name"
        )
        aliases = feat.get("aliases") or []
        assert isinstance(aliases, list)
        assert len(aliases) == len(set(aliases)), f"{feat['name']} dup aliases"
        for a in aliases:
            assert isinstance(a, str) and a.strip()
            assert a != display, f"{feat['name']} alias duplicates display_name"
