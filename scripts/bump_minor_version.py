#!/usr/bin/env python3
"""Bump the minor version of a pyproject.toml in place.

Usage: python scripts/bump_minor_version.py <path/to/pyproject.toml>

Reads the static `version = "X.Y.Z"` field, increments Y, resets Z to 0,
and writes the file back. Prints the new version to stdout so a CI step can
capture it (e.g. for tagging and the commit message).

Kept dependency-free (stdlib only) so it runs on a bare CI Python without
installing the package first.
"""

import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r'^(version\s*=\s*")(\d+)\.(\d+)\.(\d+)(")', re.MULTILINE)


def bump_minor(pyproject_path: Path) -> str:
    text = pyproject_path.read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if match is None:
        raise SystemExit(f"No `version = \"X.Y.Z\"` field found in {pyproject_path}")

    major, minor = int(match.group(2)), int(match.group(3))
    new_version = f"{major}.{minor + 1}.0"
    new_text = text[: match.start()] + f'{match.group(1)}{new_version}{match.group(5)}' + text[match.end() :]
    pyproject_path.write_text(new_text, encoding="utf-8")
    return new_version


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/bump_minor_version.py <path/to/pyproject.toml>")
    print(bump_minor(Path(sys.argv[1])))


if __name__ == "__main__":
    main()
