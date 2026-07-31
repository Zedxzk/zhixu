#!/usr/bin/env bash
set -euo pipefail
umask 027

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 RELEASE_ID WHEELHOUSE_DIRECTORY" >&2
  exit 2
fi

release_id=$1
wheelhouse=$(realpath "$2")
release_root=/opt/zhixu/releases
release_dir=${release_root}/${release_id}
source_dir=${release_dir}/source

if [[ ! ${release_id} =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]]; then
  echo "Invalid release id." >&2
  exit 2
fi
if [[ ! -f ${wheelhouse}/SHA256SUMS || ! -f ${source_dir}/requirements.lock ]]; then
  echo "Wheelhouse manifest or runtime lock is missing." >&2
  exit 2
fi
if [[ -e ${release_dir}/venv ]]; then
  echo "Release virtual environment already exists." >&2
  exit 3
fi

(
  cd "${wheelhouse}"
  sha256sum --check --strict SHA256SUMS
  find . -maxdepth 1 -type f -name '*.whl' -printf '%f\0' |
    sort -z |
    xargs -0 sha256sum |
    cmp --silent - SHA256SUMS
)

mapfile -t project_wheels < <(find "${wheelhouse}" -maxdepth 1 -type f -name 'zhixu-*.whl')
if [[ ${#project_wheels[@]} -ne 1 ]]; then
  echo "Wheelhouse must contain exactly one Zhixu wheel." >&2
  exit 3
fi

python3 -m venv "${release_dir}/venv"
"${release_dir}/venv/bin/python" -m pip install \
  --no-index \
  --no-deps \
  --find-links "${wheelhouse}" \
  --requirement "${source_dir}/requirements.lock"
"${release_dir}/venv/bin/python" -m pip install \
  --no-index \
  --no-deps \
  "${project_wheels[0]}"
"${release_dir}/venv/bin/python" -m compileall -q \
  "${release_dir}/venv/lib"
chmod -R u=rwX,g=rX,o=rX "${release_dir}"
echo "Offline release installation completed."
