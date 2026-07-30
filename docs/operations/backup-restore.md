# Encrypted backup and restore drills

Ordinary-state and vault backups use different service accounts, directory trees, and
passphrases. The ordinary-state job covers the domain database, QQ encrypted contact/session
database, and outbound encrypted target database. The daily timers create an authenticated
encrypted envelope for each database and immediately restore it into an isolated temporary
directory for an integrity check.

```bash
systemctl list-timers 'zhixu*backup*'
sudo systemctl start zhixu-backup.service
sudo systemctl start zhixu-vault-backup.service
journalctl --namespace=zhixu -u zhixu-backup.service
journalctl --namespace=zhixu -u zhixu-vault-backup.service
```

Success is not proof that a future operator knows the recovery passphrase. On a scheduled
basis, copy one encrypted artifact to an isolated recovery machine and run the interactive
restore command there:

```bash
zhixu restore --input APPLICATION_BACKUP --database NEW_APPLICATION_DATABASE
zhixu restore --input QQ_BACKUP --database NEW_QQ_DATABASE
zhixu restore --input OUTBOUND_BACKUP --database NEW_OUTBOUND_DATABASE
zhixu-vault restore --input VAULT_BACKUP --database NEW_VAULT_DATABASE
```

Both commands refuse to overwrite an existing destination. After restoration:

1. run SQLite `PRAGMA integrity_check`;
2. start services against temporary paths with all external network access disabled;
3. verify the vault audit chain and that QQ/outbound targets still resolve only through
   opaque references;
4. verify that a wrong passphrase fails without creating a plaintext destination;
5. securely remove the temporary restored databases after the drill.

Do not place backup artifacts, passphrases, recovery screenshots, or drill logs in the
public repository.

Database backups alone are not a complete off-host recovery set. Retain the v2 encrypted
deployment bundle described in the deployment guide: it contains the field-encryption keys,
reference keys, grant key, QQ credential, and both backup passphrases needed to reproduce
the credential directory. Keep the bundle separately from its passphrase and test
installation only into a disposable, isolated configuration tree. A legacy QQ-only v1
bundle cannot reproduce an installed system and must be upgraded during its first install.

## Key rotation

Stop the vault so its in-memory key set cannot become stale, rotate from an interactive
terminal as the vault service account, then restart it. The restart remains sealed:

```bash
sudo systemctl stop zhixu-vault.service
sudo -u zhixu-vault /opt/zhixu/current/venv/bin/zhixu-vault \
  rotate-keys --database /var/lib/zhixu-vault/vault.sqlite3
sudo systemctl start zhixu-vault.service
```

Use `change-passphrase` with the same stop/start procedure when rotating the unlock
passphrase. Neither command accepts a passphrase through argv or an environment variable.
