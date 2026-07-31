# Private single-server deployment

Zhixu is deployed as isolated local services. The administration API listens only on
`127.0.0.1:8840`; expose it only through an existing private VPN, an SSH tunnel, or a private
HTTPS ingress. Do not add a public listener or public webhook.

The repository contains no deployment target, account identifier, private hostname, or
credential. Deployment secrets travel only in an authenticated encrypted bundle and are
installed without appearing in command arguments, environment variables, or release files.

## 1. Bootstrap operating-system boundaries

Review the script, then run it once as root:

```bash
sudo scripts/deploy/00_bootstrap_root.sh
```

It creates local no-login accounts `zhixu`, `zhixu-vault`, `zhixu-integration`, and
`zhixu-deploy`, plus the minimal `zhixu-vault-client` socket group. The integration account
can call the fixed GitHub API but cannot read either SQLite database or the vault directory.
It does not create a cloud account or connect to an external vault service.

## 2. Create the encrypted deployment bundle

On a trusted administration machine, from an installed Zhixu environment, create one
complete recovery bundle:

```bash
zhixu create-deployment-bundle --output /secure/offline/zhixu-deployment.zxe
```

The command reads the QQ application identifier, QQ client secret, and bundle passphrase
from the terminal. It generates the remaining independent credentials itself. It refuses
to overwrite its output and creates it with mode `0600`. The encrypted bundle contains:

```text
app_field_key                 32 random bytes, base64 encoded
qq_field_key                  different 32 random bytes, base64 encoded
outbound_field_key            different 32 random bytes, base64 encoded
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

Keep the bundle and its passphrase in separate offline locations. Neither belongs in the
repository, a release archive, an issue, a log, or an online backup. Copy the encrypted
bundle to the target through an authenticated private path and keep it mode `0600`.

Copy `deploy/runtime.conf.example` to `/etc/zhixu/runtime.conf`, replace only synthetic
values, and set mode `0644`. This file is non-secret. A QQ-first deployment without private
HTTPS must set `ZHIXU_ADMIN_WEB_ENABLED=false` and omit the Passkey values. In that mode the
loopback API exposes only health probes and authenticated `/internal/` channel routes; all
browser administration routes are unavailable and the vault has no Passkey handler.

After activation, send one private QQ message. Stop the application writers and run the
one-time owner bootstrap through a transient `zhixu` service with the application field key
loaded as a systemd credential:

```bash
sudo systemctl stop zhixu-qq.service zhixu-worker.service zhixu-api.service
sudo systemd-run --wait --pipe --collect \
  --uid=zhixu --gid=zhixu \
  --property=LoadCredential=app_field_key:/etc/zhixu/credentials/app_field_key \
  /opt/zhixu/current/venv/bin/zhixu bootstrap-qq-owner \
  --database /var/lib/zhixu/zhixu.sqlite3
sudo systemctl start zhixu-api.service zhixu-worker.service zhixu-qq.service
```

The command accepts only a fresh, uniquely observed, unbound private QQ route. It closes
permanently after creating the first user or identity, stores only the opaque route in the
application database, and never prints the QQ identifier.

To enable browser administration later, set `ZHIXU_ADMIN_WEB_ENABLED=true` and configure
both Passkey values. The Passkey origin must exactly match the private HTTPS origin.

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

Before the first activation, install the encrypted bundle interactively as root:

```bash
sudo /opt/zhixu/releases/RELEASE_ID/venv/bin/zhixu \
  install-deployment-bundle --bundle /private/staging/zhixu-deployment.zxe
```

The installer accepts only the fixed `/etc/zhixu` destination, validates root ownership and
bootstrap permissions, stages every credential, and atomically replaces the empty
credential directory. It creates the Ed25519 public half separately for the vault and never
prints a secret value. It refuses an already provisioned destination.

The original QQ-only `zhixu-deployment-secrets-v1` bundle remains supported. Because it does
not contain the generated encryption and backup keys, its first installation must also
produce a complete v2 recovery bundle:

```bash
sudo /opt/zhixu/releases/RELEASE_ID/venv/bin/zhixu \
  install-deployment-bundle \
  --bundle /private/staging/legacy-qq-only.zxe \
  --recovery-output /secure/offline/zhixu-deployment-v2.zxe
```

The recovery output must be a new path, preferably on encrypted removable storage. Verify
that it is stored offline before deleting the legacy staging copy. All later restores use
the v2 bundle without `--recovery-output`.

Activate only after reviewing the source and wheel manifests:

```bash
sudo /opt/zhixu/releases/RELEASE_ID/venv/bin/zhixu preflight
sudo scripts/deploy/20_activate_root.sh RELEASE_ID
```

The preflight reads fixed `/etc/zhixu` paths, requires root ownership and exact restrictive
permissions, validates key lengths, the Ed25519 public key, Passkey origin, LLM endpoint,
outbound declarations, and every outbound credential schema. It prints only a result code
and aggregate counts. Activation runs the same preflight again before changing the current
release symlink.

The activation is an atomic symlink switch. It restarts the vault sealed and never copies,
replaces, or rolls back any SQLite database.

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
  unlock --socket /run/zhixu/vault/vault.sock
```

The vault passphrase is read from the TTY. It must never be supplied through argv, an
environment variable, a unit file, or shell history.

## 5. Optional outbound-only accounts

Email, enterprise WeChat, and generic HTTPS webhook accounts are outbound-only. Declare
their non-secret identities in `/etc/zhixu/outbound-accounts.json`; the API rejects identity
verification requests for undeclared accounts:

```json
[
  {"channel": "email", "channel_account": "email_synthetic"}
]
```

For each account, create `/etc/zhixu/outbound/INSTANCE.json` as root with mode `0600`.
The strict credential object schemas are:

```text
email:
  channel, channel_account, host, port, sender, username, password, implicit_tls
wecom:
  channel, channel_account, corp_id, agent_id, secret
webhook:
  channel, channel_account, signing_key_base64, allowed_hosts, allowed_ip_networks
```

The `channel` and `channel_account` values must match the declaration. Webhook targets must
use HTTPS and must pass both the host and resolved-IP allowlists. Use a non-identifying
instance name and start only the configured instance:

```bash
sudo systemctl enable --now zhixu-outbound@email-synthetic.service
```

Recipient addresses, enterprise user IDs, and webhook URLs are submitted through the
private administration API. They are stored only as authenticated ciphertext in
`/var/lib/zhixu/outbound/targets.sqlite3`; the ordinary outbox contains an opaque reference.
The outbound worker receives the dedicated target key, but not the application field key,
application reference key, QQ credentials, LLM credential, grant signer, or vault data.

## 6. Private HTTPS

This section is required only when `ZHIXU_ADMIN_WEB_ENABLED=true`.

Render `deploy/reverse-proxy/nginx-private.conf.template` with a private interface address
and a private DNS name. Verify the rendered listener is not a wildcard address. TLS keys
remain under `/etc/zhixu/tls/` and are not shared with the application services.

The proxy forwards only to `http://127.0.0.1:8840`. Firewall and VPN policy remain an
independent outer boundary.

## 7. Verification

The repository intentionally has no hosted CI workflow. Run the release gate locally on a
trusted development machine, then run these checks on the private server:

```bash
curl --fail http://127.0.0.1:8840/health/live
curl --fail http://127.0.0.1:8840/health/ready
systemctl is-active \
  zhixu-api zhixu-worker zhixu-qq zhixu-pat-executor zhixu-vault
/opt/zhixu/current/venv/bin/zhixu doctor
sudo -u zhixu-vault /opt/zhixu/current/venv/bin/zhixu-vault status
sudo -u zhixu-vault /opt/zhixu/current/venv/bin/zhixu-vault \
  verify-audit --database /var/lib/zhixu-vault/vault.sqlite3 \
  --checkpoint-directory /var/backups/zhixu/vault-audit
```

`ready=true` means deterministic storage is usable. LLM and vault availability are reported
as optional degraded components and do not stop ordinary agenda or reminder processing.

Before accepting a deployment, confirm from a network outside the VPN that port `8840` and
the private HTTPS endpoint are unreachable.

For each configured outbound-only account, also verify its templated unit:

```bash
systemctl is-active zhixu-outbound@email-synthetic.service
```

After the first activation and after every upgrade, run the aggregate root verifier:

```bash
sudo scripts/deploy/40_verify_root.sh
```

It re-runs preflight, requires all core services and backup timers to be enabled and active,
checks the service-owned Unix socket directories, rejects wildcard port `8840`, and probes
the loopback health endpoints. It prints no credential, database path, external identity,
or response body. The required check from a network outside the VPN remains separate.

The optional `pat` vault executor accepts only three read operations:
`github.get_authenticated_user`, `github.list_repositories`, and
`github.get_repository`. The provider origin, HTTP method, and current GitHub REST API
version are fixed in code. An API caller cannot supply a URL, redirect target, or shell
command.

## 8. Rollback

```bash
sudo scripts/deploy/30_rollback_root.sh
```

Rollback changes only `/opt/zhixu/current`. If a release introduced an irreversible
migration, restore into a new database path from an encrypted backup, validate it, stop the
affected service, and then atomically replace the path. Never run an older binary against a
schema it does not support.
