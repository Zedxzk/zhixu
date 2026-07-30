#!/usr/bin/env bash
set -euo pipefail
umask 027

if [[ ${EUID} -ne 0 || $# -ne 1 ]]; then
  echo "Usage as root: $0 RELEASE_ID" >&2
  exit 2
fi

release_id=$1
release_root=/opt/zhixu/releases
release_dir=${release_root}/${release_id}
if [[ ! ${release_id} =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]]; then
  echo "Invalid release id." >&2
  exit 2
fi
if [[ ! -x ${release_dir}/venv/bin/zhixu || ! -d ${release_dir}/source/deploy/systemd ]]; then
  echo "Release is incomplete." >&2
  exit 3
fi

previous=
if [[ -L /opt/zhixu/current ]]; then
  previous=$(readlink -f /opt/zhixu/current)
fi
ln -sfn "${release_dir}" /opt/zhixu/current.next
mv -Tf /opt/zhixu/current.next /opt/zhixu/current
if [[ -n ${previous} ]]; then
  ln -sfn "${previous}" /opt/zhixu/previous
fi

install -o root -g root -m 0644 \
  "${release_dir}"/source/deploy/systemd/*.service \
  "${release_dir}"/source/deploy/systemd/*.timer \
  /etc/systemd/system/
install -d -o root -g root -m 0755 /etc/systemd/journald@zhixu.conf.d
install -o root -g root -m 0644 \
  "${release_dir}/source/deploy/journald/retention.conf" \
  /etc/systemd/journald@zhixu.conf.d/retention.conf
systemctl daemon-reload
systemctl restart systemd-journald@zhixu.service
systemctl enable \
  zhixu-api.service \
  zhixu-worker.service \
  zhixu-qq.service \
  zhixu-pat-executor.service \
  zhixu-vault.service \
  zhixu-backup.timer \
  zhixu-vault-backup.timer
systemctl restart zhixu-pat-executor.service
systemctl restart zhixu-vault.service
systemctl restart zhixu-api.service zhixu-worker.service zhixu-qq.service
systemctl start zhixu-backup.timer zhixu-vault-backup.timer
echo "Activated release ${release_id}; vault remains sealed after restart."
