"""Acceptance criterion (b): the built wheel is self-contained.

Builds the wheel, installs it into a FRESH throwaway venv (so there is
NO repo-root ``eval/`` sibling on the path), then — inside that venv —
imports each Stage-1 extractor, asserts it loads its packaged YAML, and
runs ``GoRouterExtractor`` over a tiny in-memory chi route fixture to
prove a real anchor is emitted from packaged data alone.

This test is slow (builds + pip install) and is marked ``slow`` so fast
unit runs can deselect it with ``-m 'not slow'``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Probe script run INSIDE the fresh venv. It must succeed only if the
# packaged data is reachable via importlib.resources with no eval/ sibling.
_PROBE = r'''
import sys, tempfile, os
# Move cwd somewhere with no eval/ sibling, belt-and-suspenders.
os.chdir(tempfile.gettempdir())

from faultline.pipeline_v2.data import load_stack_yaml, load_data_text

stacks = ["fastapi","go-http-router","js-library",
          "python-library","rails-app","rust-workspace"]
for s in stacks:
    d = load_stack_yaml(s)
    assert isinstance(d, dict) and d, f"{s}.yaml empty in wheel"
assert load_data_text("dependency-anchors.yaml").strip(), "dep-anchors empty"

# Real extraction from packaged data: a tiny chi route fixture.
from faultline.pipeline_v2 import ScanContext
from faultline.pipeline_v2.extractors.go_router import GoRouterExtractor

repo = tempfile.mkdtemp()
src = (
    "package main\n"
    'import (\n  "net/http"\n  "github.com/go-chi/chi/v5"\n)\n'
    "func main() {\n"
    "  r := chi.NewRouter()\n"
    '  r.Get("/widgets", handleWidgets)\n'
    '  http.ListenAndServe(":8080", r)\n'
    "}\n"
)
with open(os.path.join(repo, "main.go"), "w") as fh:
    fh.write(src)

ctx = ScanContext(
    repo_path=__import__("pathlib").Path(repo),
    stack=None, monorepo=False, workspaces=None,
    tracked_files=["main.go"], commits=[], stack_signals=[],
    workspace_manager=None, audited_stack="go-server",
    secondary_stacks=(), extractor_hints=(), auditor_confidence=0.9,
)
cands = GoRouterExtractor().extract(ctx)
names = {c.name for c in cands}
assert "widgets" in names, f"go_router emitted no widget anchor: {names}"
print("WHEEL_HERMETIC_OK", sorted(names))
'''


@pytest.mark.slow
def test_wheel_is_self_contained(tmp_path: Path) -> None:
    if shutil.which("python") is None and not sys.executable:
        pytest.skip("no python interpreter available")

    # 1) Build the wheel into tmp_path/dist (no --no-isolation: clean build).
    dist = tmp_path / "dist"
    build = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        if "No module named build" in build.stderr or "No module named build" in build.stdout:
            pytest.skip("`build` package not installed in dev env")
        pytest.fail(f"wheel build failed:\n{build.stdout}\n{build.stderr}")

    wheels = list(dist.glob("*.whl"))
    assert wheels, f"no wheel produced in {dist}"
    wheel = wheels[0]

    # 2) Fresh throwaway venv — NOT the dev repo, so NO sibling eval/.
    venv_dir = tmp_path / "fresh-venv"
    venv.create(venv_dir, with_pip=True)
    bindir = "Scripts" if os.name == "nt" else "bin"
    py = venv_dir / bindir / ("python.exe" if os.name == "nt" else "python")

    install = subprocess.run(
        [str(py), "-m", "pip", "install", str(wheel)],
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, (
        f"pip install of wheel failed:\n{install.stdout}\n{install.stderr}"
    )

    # Sanity: site-packages of the fresh venv must NOT contain an eval/ dir.
    # (Proves we are not secretly relying on the old force-include hack.)
    purelib = subprocess.run(
        [str(py), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert not (Path(purelib) / "eval").exists(), (
        "wheel still ships site-packages/eval/ — band-aid not removed"
    )

    # 3) Run the probe inside the fresh venv.
    probe = subprocess.run(
        [str(py), "-c", _PROBE],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, (
        f"hermetic probe failed:\n{probe.stdout}\n{probe.stderr}"
    )
    assert "WHEEL_HERMETIC_OK" in probe.stdout, probe.stdout
