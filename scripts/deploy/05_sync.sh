#!/usr/bin/env bash
set -euo pipefail
umask 027

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 SOURCE_DIRECTORY RELEASE_ID" >&2
  exit 2
fi

source_dir=$(realpath "$1")
release_id=$2
release_root=/opt/zhixu/releases

if [[ ! ${release_id} =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]]; then
  echo "Invalid release id." >&2
  exit 2
fi
if [[ ! -f ${source_dir}/pyproject.toml || ! -d ${source_dir}/src ]]; then
  echo "Source directory is not a Zhixu checkout." >&2
  exit 2
fi
if [[ -e ${release_root}/${release_id} ]]; then
  echo "Release id already exists; releases are immutable." >&2
  exit 3
fi

destination=${release_root}/${release_id}/source
install -d -m 0750 "${release_root}/${release_id}"
install -d -m 0750 "${destination}"

rsync \
  --archive \
  --delete \
  --prune-empty-dirs \
  --include='/pyproject.toml' \
  --include='/requirements.lock' \
  --include='/README.md' \
  --include='/LICENSE' \
  --include='/.env.example' \
  --include='/src/' \
  --include='/src/***' \
  --include='/scripts/' \
  --include='/scripts/deploy/***' \
  --include='/deploy/' \
  --include='/deploy/***' \
  --include='/docs/' \
  --include='/docs/api.md' \
  --include='/docs/commands.md' \
  --include='/docs/deployment.md' \
  --include='/docs/operations/***' \
  --include='/docs/security/***' \
  --exclude='*' \
  "${source_dir}/" "${destination}/"

if find "${destination}" -type f \
  \( -name '*.sqlite*' -o -name '*.db' -o -name '*.env' -o -name '*.key' -o -name '*.pem' \) \
  -print -quit | grep -q .; then
  echo "Release whitelist unexpectedly included a private/runtime file." >&2
  exit 3
fi

(
  cd "${destination}"
  find . -type f -print0 |
    sort -z |
    xargs -0 sha256sum > ../SOURCE_SHA256SUMS
)
chmod -R u=rwX,g=rX,o= "${release_root:?}/${release_id}"
echo "Whitelisted source synchronized to release ${release_id}."
