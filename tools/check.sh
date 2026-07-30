#!/usr/bin/env bash
# The pre-merge gate. There is no CI wired up here, so this is a discipline gate
# an operator has to remember to run - not an enforced one. Promoting it to a
# workflow is a two-line change.
#
# No staging step first: tools/leakcheck.py scans `git ls-files` *and*
# `git ls-files --others --exclude-standard`, so a brand-new unstaged file is
# already covered. What it cannot see is anything `.gitignore` covers - and
# `git add -A` would not stage those either.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m ruff check .
python3 -m ruff format --check .
python3 -m pytest -q tests/
python3 -m coverage run --source=linkedin_cli.transport -m pytest -q tests/
python3 -m coverage report --include="*transport.py" --fail-under=100
python3 tools/leakcheck.py
