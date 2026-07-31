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
GET|POST|DELETE      /admin/acl
GET                  /admin/channels
GET|PATCH            /admin/channel-routes
GET                  /admin/outbox
GET                  /admin/audit
GET                  /admin/llm-usage
GET                  /admin/status
```

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

Creation accepts `kind` equal to `human` or `machine`; L4 values are not accepted. Human
secrets can be revealed only with current Passkey step-up. Machine secrets use an
allowlisted executor. The vault passes the value only to the fixed local
`/run/zhixu/integration/pat-executor.sock` boundary; it never accepts an executor address from an API
request. The executor response is rejected if it contains the credential.
Exports are additionally encrypted with the supplied export passphrase.

Never send a vault value, export passphrase, session token, raw external identifier, or
production response body to an issue tracker or public log.
