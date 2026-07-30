#!/usr/bin/env bash
set -euo pipefail
umask 027

python -m compileall -q src
ruff check .
python -m pytest -q
PYTHONPATH=src lint-imports
python scripts/privacy_scan.py
python scripts/history_privacy_scan.py

secret_report=$(mktemp)
trap 'rm -f "${secret_report}"' EXIT
detect-secrets scan > "${secret_report}"
python - "${secret_report}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    report = json.load(source)
results = report.get("results", {})
if any(results.values()):
    print("Secret scan failed; inspect the local detector report.")
    raise SystemExit(1)
print("Secret scan passed.")
PY

mkdir -p dist
pip-audit --strict --requirement requirements.lock
pip-audit \
  --strict \
  --requirement requirements.lock \
  --format cyclonedx-json \
  --output dist/sbom.cdx.json
python -m build --wheel
echo "Local release verification and SBOM generation completed."
