"""Phase 1: Research Agent — select repos for validation via Claude."""

import os

import anthropic
from pydantic import BaseModel

from ..config import FALLBACK_REPOS, RESEARCH_MODEL
from ..models import RepoTarget

_SYSTEM_PROMPT = """\
You are a research agent selecting open-source GitHub repositories for validating \
a feature detection CLI tool. The tool analyses git history to identify business \
features (auth, payments, dashboard, etc.) in codebases.

Select repositories that:
1. Are well-known, actively maintained, public on GitHub
2. Have clear, identifiable business feature domains (not utility libraries)
3. Have 200-3000 source files in the main app directory
4. Are primarily TypeScript, JavaScript, or Python
5. Have a clear --src filter path (e.g. apps/web/src/, src/, app/)

For each repo, list 4-8 expected business features that a detection tool should find.
"""

_USER_PROMPT = """\
Select {count} open-source repositories for validating a feature detection tool.

Return a JSON list. For each repo include:
- name: "owner/repo" format
- url: git clone URL
- expected_features: list of 4-8 business feature names the tool should detect
- src_filter: subdirectory path to focus analysis on (e.g. "apps/web/src/")
- reason: one sentence why this repo is good for validation

Focus on web applications with clear business domains, not libraries or frameworks.
"""


class _RepoListResponse(BaseModel):
    repos: list[RepoTarget]


def select_repos(
    count: int = 8,
    api_key: str | None = None,
) -> list[RepoTarget]:
    """Selects repos via Claude. Falls back to hardcoded list on failure."""
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return _fallback(count)

    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.parse(
            model=RESEARCH_MODEL,
            max_tokens=4096,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _USER_PROMPT.format(count=count)}],
            response_model=_RepoListResponse,
        )
        repos = response.parsed_output.repos
        if len(repos) >= 3:
            return repos[:count]
    except Exception:
        pass

    return _fallback(count)


def _fallback(count: int) -> list[RepoTarget]:
    return [RepoTarget(**r) for r in FALLBACK_REPOS[:count]]
