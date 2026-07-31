#!/usr/bin/env bash
set -euo pipefail
umask 027

if [[ ${EUID} -ne 0 ]]; then
  echo "Rollback must run as root." >&2
  exit 1
fi
if [[ ! -L /opt/zhixu/previous ]]; then
  echo "No previous release is recorded." >&2
  exit 2
fi
if [[ ! -L /opt/zhixu/current ]]; then
  echo "Current release link is missing." >&2
  exit 2
fi

previous=$(readlink -f /opt/zhixu/previous)
case "${previous}" in
  /opt/zhixu/releases/*) ;;
  *)
    echo "Previous release link is unsafe." >&2
    exit 3
    ;;
esac
if [[ ! -x ${previous}/venv/bin/zhixu ]]; then
  echo "Previous release is incomplete." >&2
  exit 3
fi

current=$(readlink -f /opt/zhixu/current)
ln -sfn "${previous}" /opt/zhixu/current.next
mv -Tf /opt/zhixu/current.next /opt/zhixu/current
ln -sfn "${current}" /opt/zhixu/previous
systemctl restart zhixu-pat-executor.service zhixu-vault.service
systemctl restart zhixu-llm-proxy.service
systemctl restart zhixu-api.service zhixu-worker.service zhixu-qq.service
echo "Rolled back code to $(basename "${previous}"); databases were not overwritten."
