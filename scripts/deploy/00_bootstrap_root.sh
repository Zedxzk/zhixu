#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "This bootstrap must run as root." >&2
  exit 1
fi

ensure_group() {
  local group_name=$1
  if ! getent group "${group_name}" >/dev/null; then
    groupadd --system "${group_name}"
  fi
}

ensure_system_user() {
  local user_name=$1
  local primary_group=$2
  local home_dir=$3
  if ! getent passwd "${user_name}" >/dev/null; then
    useradd \
      --system \
      --gid "${primary_group}" \
      --home-dir "${home_dir}" \
      --shell /usr/sbin/nologin \
      --no-create-home \
      "${user_name}"
  fi
}

ensure_group zhixu
ensure_group zhixu-vault
ensure_group zhixu-vault-client
ensure_group zhixu-deploy
ensure_group zhixu-integration

ensure_system_user zhixu zhixu /var/lib/zhixu
ensure_system_user zhixu-vault zhixu-vault /var/lib/zhixu-vault
ensure_system_user zhixu-deploy zhixu-deploy /var/lib/zhixu-deploy
ensure_system_user zhixu-integration zhixu-integration /var/lib/zhixu-integration

usermod --append --groups zhixu-vault-client zhixu
usermod --append --groups zhixu-vault-client zhixu-vault
usermod --append --groups zhixu-vault-client zhixu-integration

install -d -o root -g zhixu-deploy -m 0750 /opt/zhixu
install -d -o zhixu-deploy -g zhixu-deploy -m 0750 /opt/zhixu/releases
install -d -o root -g root -m 0755 /etc/zhixu
install -d -o root -g root -m 0700 /etc/zhixu/credentials
install -d -o root -g root -m 0700 /etc/zhixu/outbound
if [[ ! -e /etc/zhixu/outbound-accounts.json ]]; then
  install -o root -g root -m 0644 /dev/stdin /etc/zhixu/outbound-accounts.json <<'EOF'
[]
EOF
fi
if [[ ! -e /etc/zhixu/credentials/llm_api_key ]]; then
  install -o root -g root -m 0600 /dev/null /etc/zhixu/credentials/llm_api_key
fi
install -d -o zhixu -g zhixu -m 0700 /var/lib/zhixu
install -d -o zhixu -g zhixu -m 0700 /var/lib/zhixu/qq
install -d -o zhixu -g zhixu -m 0700 /var/lib/zhixu/outbound
install -d -o zhixu-vault -g zhixu-vault -m 0700 /var/lib/zhixu-vault
install -d -o root -g root -m 0711 /var/backups/zhixu
install -d -o zhixu -g zhixu -m 0700 /var/backups/zhixu/application
install -d -o zhixu -g zhixu -m 0700 /var/backups/zhixu/qq
install -d -o zhixu -g zhixu -m 0700 /var/backups/zhixu/outbound
install -d -o zhixu-vault -g zhixu-vault -m 0700 /var/backups/zhixu/vault
install -d -o root -g zhixu-vault-client -m 0770 /run/zhixu

install -o root -g root -m 0644 /dev/stdin /etc/tmpfiles.d/zhixu.conf <<'EOF'
d /run/zhixu 0770 root zhixu-vault-client -
EOF

echo "Local Zhixu service accounts and isolated data directories are ready."
