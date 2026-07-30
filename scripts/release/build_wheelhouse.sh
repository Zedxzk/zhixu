#!/usr/bin/env bash
set -euo pipefail
umask 027

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 OUTPUT_DIRECTORY" >&2
  exit 2
fi

output=$1
mkdir -p "${output}"
if find "${output}" -mindepth 1 -print -quit | grep -q .; then
  echo "Output directory must be empty." >&2
  exit 3
fi
python -m build --wheel --outdir "${output}"
python -m pip download \
  --dest "${output}" \
  --only-binary=:all: \
  --requirement requirements.lock
(
  cd "${output}"
  find . -maxdepth 1 -type f -name '*.whl' -printf '%f\0' |
    sort -z |
    xargs -0 sha256sum > SHA256SUMS
)
echo "Wheelhouse and SHA256 manifest created."
