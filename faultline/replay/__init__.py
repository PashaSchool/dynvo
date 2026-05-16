"""Stage replay system (Sprint 9f).

Lets the engineer re-execute a single pipeline stage (or a chain of
stages) on a cached artifact instead of re-running the full scan.
Saves 30-70× on iteration cost when tweaking a single stage.

Public surface:

    list_stages()                        → list[str]
    load_artifact(path)                  → FeatureMap
    save_artifact(fm, path)              → None
    run_stage(name, fm, *, ctx)          → FeatureMap
    run_chain(names, fm, *, ctx)         → FeatureMap

CLI wrapper: ``faultline replay <stage> --input <art>.json``.

Per ``rule-cold-scan``: replay reads artifacts as INPUT but writes
fresh output that doesn't persist into the next scan. Replay is a
diagnostic tool, not a way to short-cut the cold-scan contract.
"""

from faultline.replay.registry import (
    StageContext,
    list_stages,
    load_artifact,
    run_chain,
    run_stage,
    save_artifact,
)

__all__ = [
    "StageContext",
    "list_stages",
    "load_artifact",
    "run_chain",
    "run_stage",
    "save_artifact",
]
