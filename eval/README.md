# eval/

Runtime pattern configs and golden-free tooling that ship with the
engine (stack patterns, system-flow patterns, dependency anchors,
DI patterns, the structural audit harness).

Curated ground-truth corpora, scorers and score baselines are **not**
part of this repository — accuracy evaluation runs in a private
pipeline. `run_structural_corpus.sh --baseline` writes a local
baseline file if you want to track your own fork's numbers.
