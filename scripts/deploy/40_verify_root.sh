#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "Deployment verification must run as root." >&2
  exit 1
fi

release=/opt/zhixu/current
if [[ ! -L ${release} || ! -x ${release}/venv/bin/zhixu ]]; then
  echo "deployment=failed code=release_missing" >&2
  exit 2
fi

"${release}/venv/bin/zhixu" preflight

services=(
  zhixu-api.service
  zhixu-worker.service
  zhixu-qq.service
  zhixu-llm-proxy.service
  zhixu-pat-executor.service
  zhixu-vault.service
)
timers=(
  zhixu-backup.timer
  zhixu-vault-backup.timer
)
for unit in "${services[@]}" "${timers[@]}"; do
  if ! systemctl is-enabled --quiet "${unit}"; then
    echo "deployment=failed code=unit_not_enabled" >&2
    exit 3
  fi
  if ! systemctl is-active --quiet "${unit}"; then
    echo "deployment=failed code=unit_not_active" >&2
    exit 3
  fi
done

require_metadata() {
  local path=$1
  local expected=$2
  if [[ ! -d ${path} || -L ${path} ]]; then
    echo "deployment=failed code=runtime_directory_insecure" >&2
    exit 4
  fi
  local actual
  actual=$(stat -c '%U:%G:%a' "${path}")
  if [[ ${actual} != "${expected}" ]]; then
    echo "deployment=failed code=runtime_directory_insecure" >&2
    exit 4
  fi
}

require_metadata /run/zhixu root:root:755
require_metadata /run/zhixu/vault zhixu-vault:zhixu-vault-client:750
require_metadata \
  /run/zhixu/integration \
  zhixu-integration:zhixu-vault-client:750
require_metadata /var/backups/zhixu/vault-audit zhixu-vault:zhixu-vault:700

listeners=$(ss -H -ltn 'sport = :8840')
if [[ -z ${listeners} ]] \
  || ! grep -Fq '127.0.0.1:8840' <<<"${listeners}" \
  || grep -Eq '(^|[[:space:]])(\*|0\.0\.0\.0|\[::\]):8840' <<<"${listeners}"; then
  echo "deployment=failed code=api_listener_insecure" >&2
  exit 5
fi

curl --noproxy '*' --fail --silent --show-error \
  http://127.0.0.1:8840/health/live >/dev/null
curl --noproxy '*' --fail --silent --show-error \
  http://127.0.0.1:8840/health/ready >/dev/null
"${release}/venv/bin/zhixu" doctor >/dev/null
runuser --user zhixu-vault -- "${release}/venv/bin/zhixu-vault" \
  status --socket /run/zhixu/vault/vault.sock >/dev/null

echo "deployment=ready services=${#services[@]} timers=${#timers[@]}"
