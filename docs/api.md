# Private administration API

The administration API is a JSON API intended only for the private HTTPS origin described
in [deployment.md](deployment.md). It does not use cookies, does not enable CORS, and never
needs a public callback. Responses containing authentication or reveal material include
`Cache-Control: no-store`.

`/internal/channel/*` is reserved for authenticated local channel processes and must remain
blocked by the reverse proxy.

## Sessions and Passkey step-up

Create an ordinary administration session:

```http
POST /admin/session
Content-Type: application/json

{"user_id":"INTERNAL_USER_ID","password":"..."}
```

Use the returned value as `Authorization: Bearer …`. Enroll and verify a Passkey through
these WebAuthn ceremony endpoints:

```text
POST /admin/passkeys/registration/options
POST /admin/passkeys/registration/verify
POST /admin/passkeys/authentication/options
POST /admin/passkeys/authentication/verify
```

The two `options` responses contain a `publicKey` object for `navigator.credentials`.
The two `verify` requests accept `{"credential": WEB_AUTHN_CREDENTIAL_OBJECT}`. A successful
authentication returns a five-minute `step_up` session. Confidential ordinary data, ACL
changes, and human-secret operations require that session.

## Cross-channel identity binding

`POST /admin/identity-challenges` accepts a channel and channel account. For a conversational
QQ account, first let the user contact the bot, read its `private` route from
`GET /admin/channel-routes`, and submit that route as `opaque_ref`. Raw QQ OpenIDs are rejected
at this boundary and remain only in the QQ process's encrypted database. For an outbound-only
account such as Email or enterprise WeChat, submit the delivery target as `external_subject`;
it is encrypted immediately in the isolated target database.

The verification code is queued to the selected target and is deliberately absent from the
API response. QQ identity challenges require an observed private route; group actor routes
cannot receive a private binding code.

Submit the received code to `POST /admin/identities`. Codes expire, allow five attempts, and
can be consumed only once. `DELETE /admin/identities/{id}` requires
`X-Zhixu-Confirm: true`, revokes active channel sessions, and leaves domain data intact.

## Ordinary data and operations

```text
GET|POST             /admin/agenda
PUT|DELETE           /admin/agenda/{id}
POST                 /admin/agenda/{id}/exceptions
GET|POST             /admin/tasks
PUT|DELETE           /admin/tasks/{id}
POST                 /admin/tasks/{id}/transition
POST                 /admin/tasks/{id}/postpone
GET|POST             /admin/notes
PUT|DELETE           /admin/notes/{id}
GET|POST             /admin/reminders
DELETE               /admin/reminders/{id}
GET                  /admin/workspaces
GET|POST|DELETE      /admin/acl
GET                  /admin/channels
GET|PATCH            /admin/channel-routes
GET                  /admin/outbox
GET                  /admin/audit
GET                  /admin/llm-usage
GET                  /admin/status
```

`GET /admin/workspaces` returns the authenticated user's private workspace plus only the
enabled internal-group workspaces where that user is an active member. Ordinary list
responses include a redacted `workspace` object (`id`, `kind`, and display `label`) and
combine those authorized workspaces. Raw platform group identifiers and internal owner IDs
are never returned. The administration UI uses this metadata to filter between all data,
private data, and each authorized internal group.

`GET /admin/channel-routes` returns opaque observed routes, group mode, and internal member
IDs. Group commands are disabled by default. Configure a synthetic observed group with a
request shaped like:

```json
{
  "channel": "qq",
  "channel_account": "qq_example",
  "opaque_ref": "opaque_group_reference",
  "commands_enabled": true,
  "group_mode": "internal",
  "member_user_ids": ["user_example"]
}
```

`group_mode` is `disabled`, `public`, or `internal`. Member IDs are accepted only for an
internal group, must name active internal users, and are replaced atomically when supplied.
The configuring user becomes the route owner and an internal member. Only that owner may
subsequently alter the route. Public mode has no database membership; switching away from
internal mode removes its active member ACL while retaining existing shared records for a
future authorized re-enable.

An agenda exception can cancel one occurrence or replace its start/end without changing
the recurring series. Note attachments are metadata-only objects (`id`, `filename`,
`media_type`, `size_bytes`, and opaque `content_ref`); this API never accepts attachment
binary content. A reminder target must be an opaque reference already bound to the current
user; arbitrary external identifiers and another user's target are rejected. Creating
several reminder records for the same `related_kind` and `related_id` provides multiple
notification times. Reminder cancellation requires the confirmation header.

Deletion requires the confirmation header. External identities and delivery targets are
represented by opaque references; status and audit endpoints do not return raw platform
identifiers, credentials, message bodies, or database paths.

The LLM usage view contains only timestamp, model reference, fixed invocation reason,
success/failure outcome, and unit counts. It never stores or returns a prompt, response,
note body, question, or provider credential.

## Isolated high-sensitivity vault

The vault must first be unlocked interactively on the server. These API operations use
signed, one-time, 60-second capability grants over the local Unix socket:

```text
GET                  /admin/vault/secrets
POST                 /admin/vault/secrets
PUT|DELETE           /admin/vault/secrets/{id}
POST                 /admin/vault/secrets/{id}/reveal
POST                 /admin/vault/secrets/{id}/use
POST                 /admin/vault/secrets/{id}/export
POST                 /admin/vault/secrets/{id}/grant
```

Creation accepts `kind` equal to `human` or `machine`. L4 values are rejected by default.
An owner may explicitly override that default only by using a current Passkey step-up,
the confirmation header, `kind=human`, `classification=l4_prohibited`, and
`policy_override=owner_explicit_human_storage`. The vault records that decision as
`l4_human_override`; it cannot be shared, granted to an executor, or used by automation.
Human secrets can be revealed only with current Passkey step-up. Machine secrets use an
allowlisted executor. The vault passes the value only to the fixed local
`/run/zhixu/integration/pat-executor.sock` boundary; it never accepts an executor address from an API
request. The executor response is rejected if it contains the credential.
Exports are additionally encrypted with the supplied export passphrase.

Never send a vault value, export passphrase, session token, raw external identifier, or
production response body to an issue tracker or public log.
