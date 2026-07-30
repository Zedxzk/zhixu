# Private single-server deployment

Zhixu is deployed as four isolated local services. The administration API listens only on
`127.0.0.1:8840`; expose it only through an existing private VPN, an SSH tunnel, or a private
HTTPS ingress. Do not add a public listener or public webhook.

The repository contains no deployment target, account identifier, private hostname, or
credential. Values below are generated directly on the target host.

## 1. Bootstrap operating-system boundaries

Review the script, then run it once as root:

```bash
sudo scripts/deploy/00_bootstrap_root.sh
```

It creates local no-login accounts `zhixu`, `zhixu-vault`, and `zhixu-deploy`, plus the
minimal `zhixu-vault-client` socket group. It does not create a cloud account or connect to
an external vault service.

## 2. Create credentials locally

Create each file under `/etc/zhixu/credentials/` with mode `0600` and root ownership.
Do not copy these files into a release directory.

```text
app_field_key                 32 random bytes, base64 encoded
qq_field_key                  different 32 random bytes, base64 encoded
app_reference_key             at least 32 random bytes, base64 encoded
identity_challenge_key        at least 32 random bytes, base64 encoded
channel_service_token         at least 32 random characters
grant_issuer_private_key      Ed25519 raw private key, base64 encoded
llm_api_key                   optional; keep the bootstrap placeholder empty for local/keyless
qq_app_id                     QQ official-bot application identifier
qq_client_secret              QQ official-bot client secret
application_backup_passphrase independent random backup passphrase
vault_backup_passphrase       different independent backup passphrase
```

Copy `deploy/runtime.conf.example` to `/etc/zhixu/runtime.conf`, replace only synthetic
values, and set mode `0644`. This file is non-secret. The Passkey origin must exactly match
the private HTTPS origin.

## 3. Build and install an offline release

On a clean build machine:

```bash
python -m venv .release-venv
.release-venv/bin/pip install -e ".[dev]"
.release-venv/bin/python -m compileall -q src
PATH="$PWD/.release-venv/bin:$PATH" bash scripts/release/verify_release.sh
PATH="$PWD/.release-venv/bin:$PATH" bash scripts/release/build_wheelhouse.sh /path/to/wheelhouse
```

On the server, as `zhixu-deploy`, use an identifier that contains no host or user data:

```bash
scripts/deploy/05_sync.sh /path/to/clean/checkout RELEASE_ID
scripts/deploy/10_install.sh RELEASE_ID /path/to/wheelhouse
```

Before the first activation, generate the Ed25519 grant issuer pair directly on the target.
The private half is delivered only to the administration API; the vault receives only the
public half:

```bash
sudo /opt/zhixu/releases/RELEASE_ID/venv/bin/zhixu generate-grant-key \
  --private-output /etc/zhixu/credentials/grant_issuer_private_key \
  --public-output /etc/zhixu/grant_issuer_public.pem
```

Never make the private key readable by the vault process.

Activate only after reviewing the source and wheel manifests:

```bash
sudo scripts/deploy/20_activate_root.sh RELEASE_ID
```

The activation is an atomic symlink switch. It restarts the vault sealed and never copies,
replaces, or rolls back either SQLite database.

## 4. Initialize local data

Create the first ordinary admin interactively:

```bash
sudo -u zhixu /opt/zhixu/current/venv/bin/zhixu \
  bootstrap-admin --database /var/lib/zhixu/zhixu.sqlite3
```

Initialize and unlock the vault only from an interactive terminal:

```bash
sudo -u zhixu-vault /opt/zhixu/current/venv/bin/zhixu-vault \
  initialize --database /var/lib/zhixu-vault/vault.sqlite3
sudo systemctl restart zhixu-vault
sudo -u zhixu-vault /opt/zhixu/current/venv/bin/zhixu-vault \
  unlock --socket /run/zhixu/vault.sock
```

The vault passphrase is read from the TTY. It must never be supplied through argv, an
environment variable, a unit file, or shell history.

## 5. Private HTTPS

Render `deploy/reverse-proxy/nginx-private.conf.template` with a private interface address
and a private DNS name. Verify the rendered listener is not a wildcard address. TLS keys
remain under `/etc/zhixu/tls/` and are not shared with the application services.

The proxy forwards only to `http://127.0.0.1:8840`. Firewall and VPN policy remain an
independent outer boundary.

## 6. Verification

The repository intentionally has no hosted CI workflow. Run the release gate locally on a
trusted development machine, then run these checks on the private server:

```bash
curl --fail http://127.0.0.1:8840/health/live
curl --fail http://127.0.0.1:8840/health/ready
systemctl is-active zhixu-api zhixu-worker zhixu-qq zhixu-vault
/opt/zhixu/current/venv/bin/zhixu doctor
sudo -u zhixu-vault /opt/zhixu/current/venv/bin/zhixu-vault status
```

`ready=true` means deterministic storage is usable. LLM and vault availability are reported
as optional degraded components and do not stop ordinary agenda or reminder processing.

Before accepting a deployment, confirm from a network outside the VPN that port `8840` and
the private HTTPS endpoint are unreachable.

## 7. Rollback

```bash
sudo scripts/deploy/30_rollback_root.sh
```

Rollback changes only `/opt/zhixu/current`. If a release introduced an irreversible
migration, restore into a new database path from an encrypted backup, validate it, stop the
affected service, and then atomically replace the path. Never run an older binary against a
schema it does not support.
