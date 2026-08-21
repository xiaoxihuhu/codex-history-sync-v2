# Auth and Devices

Phase 5 implements an independent Codex History Sync account. It does not use
or read ChatGPT login tokens, OpenAI credentials, Codex `auth.json`, provider
secrets, or service-role keys.

## Public Project Configuration

The desktop client accepts only:

- Supabase project URL.
- Supabase publishable key or legacy client-safe `anon` key.

The configuration is stored at:

```text
%LOCALAPPDATA%\CodexHistorySync\config.json
```

Secret keys and JWTs with a `service_role` claim are rejected.

Configuration command:

```powershell
py -3 .\sync_backend.py --json cloud-configure --url https://PROJECT.supabase.co
```

The key is requested through a hidden interactive prompt. It is not accepted
as a command argument.

The same public configuration can be supplied without a file:

```text
CODEX_SYNC_SUPABASE_URL
CODEX_SYNC_SUPABASE_KEY
```

Both environment variables must be present together.

## Account Commands

Passwords are read from a hidden interactive prompt and are never returned in
JSON output.

```powershell
py -3 .\sync_backend.py --json auth-sign-up --email user@example.com
py -3 .\sync_backend.py --json auth-sign-in --email user@example.com
py -3 .\sync_backend.py --json auth-status
py -3 .\sync_backend.py --json auth-sign-out
```

Supported behavior:

- Registration.
- Password login.
- Email-confirmation-required registration result.
- Automatic access-token refresh.
- Login restoration after application restart.
- Current account lookup.
- Remote sign-out attempt.
- Guaranteed removal of the local session even when remote sign-out fails.

## Session Storage

The local state database is:

```text
%LOCALAPPDATA%\CodexHistorySync\sync_state.sqlite
```

The access and refresh tokens are serialized together, encrypted with Windows
DPAPI for the current Windows user, and stored only as an encrypted blob.
Tokens are excluded from public result dictionaries and redacted from
Supabase error messages if a remote response echoes them.

No plaintext token file is created.

## Device Identity

The first device operation creates a UUID and stores it in
`sync_state.sqlite`. The ID remains stable across application restarts.

```powershell
py -3 .\sync_backend.py --json device-info
py -3 .\sync_backend.py --json device-register
py -3 .\sync_backend.py --json device-list
```

The device record contains:

- Device ID.
- Device/host name.
- Operating system name and version.
- Client version.
- First registration time.
- Last seen time.
- Last backup time.

`device-register` and `device-list` target the Phase 4 V2 `devices` schema.
They are not run against the connected old prototype schema until a tested
compatibility migration is available.

## Verification

Tests cover:

- Rejecting service-role and secret keys.
- Public configuration file and environment loading.
- Encrypted SQLite session storage.
- Real Windows DPAPI protect/unprotect.
- Login, refresh, restore, current account, and logout.
- Token/password redaction.
- Stable local device ID.
- V2 device upsert and list request contracts.

```powershell
py -3 -m unittest tests.test_cloud_auth_devices -v
py -3 -m unittest discover -s tests -v
```
