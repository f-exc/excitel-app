# set-ssid.py — change this ONU's Wi-Fi name/password through Excitel's own API

A single-file [uv](https://docs.astral.sh/uv/) script that talks to the same provisioning
endpoint the My Excitel app uses, so you can set the Wi-Fi SSID and passphrase without the
app's cosmetic `Excitel_` prefix — or set it *with* the prefix, or just read the current state.

Read-only unless you pass `--apply`. Endpoints, field names and the auth model were recovered
from the app (see `app/APP-API.md`); nothing here is guesswork.

```bash
./app/set-ssid.py --help
```

---


## Requirements

- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) — the shebang runs the script
  with its inline dependencies (`httpx`, `rich`); nothing to install by hand.
- An Excitel account, and one of:
  - the account password (for `login`), or
  - the `selfcare` session cookie (for `--cookie`, e.g. taken from a device where the app is
    already signed in), or
  - the phone number, for `otp` (only works if OTP login is enabled for your account —
    many accounts only have username + password).

No root, no app patching, no proxy. One HTTPS client and one cookie.

---

## Quick start

```bash
# 1. get a session (pick one)
./app/set-ssid.py login --user <subscriber-id>            # prompts for the password, no echo
pbpaste | ./app/set-ssid.py login --user <subscriber-id> --password-stdin
./app/set-ssid.py --cookie '<32 hex chars>' whoami

# 2. read the current state (never writes)
./app/set-ssid.py show

# 3. preview a change (prints the exact request body, sends nothing)
./app/set-ssid.py set-ssid --radio 2.4 --ssid MyNet

# 4. apply it
./app/set-ssid.py set-ssid --radio 2.4 --ssid MyNet --apply
```

`show` output looks like:

```
vendor 'netsense'
idx  radio  ssid          security  mode  type   en    bcast  vendor
  5  2.4    MyNet                         b,g,n  True  True   netsense
  1  5      <SSID-5>                      ac     True  True   netsense
   connected devices (3)
  …  rssi=-56 band=2.4GHz host='laptop'
```

Note the real values: `idx` is **5** for 2.4 GHz and **1** for 5 GHz, `radioType` is `2.4`/`5`,
and `security`/`mode` come back **empty** — the backend does not populate them.

---

## Commands

| command | what it does |
|---|---|
| `otp --phone N` | `otp/requestByPhone` → `otp/verify/login`, stores the cookie (if OTP login is enabled for the account) |
| `login --user U` | `POST login`, stores the cookie |
| `forgot --user U --phone P [--apply]` | the app's "Forgot password" call (`password/resend`); does not run unless `--apply` |
| `whoami` | reads the dashboard with the stored cookie → subscriberId, plan, `canDoOnuRemoteManagement` |
| `show [--json]` | `provision/getWlanInfo`: SSIDs, radios, connected devices, traffic frames |
| `set-ssid --ssid S [--apply]` | changes one SSID (`skipKey=true`, passphrase untouched) |
| `set-key --password P [--apply]` | changes one passphrase (SSID untouched) |
| `task ID` | polls `provision/netSense/checkTaskStatus` for an existing job |

**Global flags:** `--base-url` (or `$EXCITEL_API_BASE`), `--session`, `--cookie`,
`--subscriber-id`, `--timeout`, `--insecure` (skip TLS verification, for proxy debugging), `-v`.

**Picking the radio:** `--radio 2.4`, `--radio 5`, or `--idx 5` / `--idx 1`. If you pass
neither and there are two radios, it lists them and refuses rather than guessing.

---

## What it actually sends

Everything is `application/x-www-form-urlencoded` (the app uses Retrofit `@Field`; sending
JSON makes the backend see empty fields — that mistake looks exactly like a wrong password).

```
POST https://selfcare.north.excitel.in/api/index_dev.php/provision/setWlanInfo
Content-Type: application/x-www-form-urlencoded
Cookie: selfcare=<32 hex chars>

subscriberId=<subscriber-id>&idx=5&ssid=MyNet&broadCastSsid=true&radioType=2.4
&enable=true&mode=&security=&type=b,g,n&vendor=netsense&skipKey=true
```

`radioType`, `type`, `enable`, `mode`, `security`, `idx` and `vendor` are **re-read from
`getWlanInfo` immediately before the write and echoed back verbatim** — never invented. That
matters: those fields are *applied* by the backend, so a wrong value can change your encryption
mode or disable the radio. The response is a queued job:

```
{"success":true,"result":{"message":"Configuration changes are applied","taskId":5896504}}
→ checkTaskStatus: pending "configure_wlan" → completed "configure_wlan"
```

The script then polls like the app does (30 s interval, 360 s cap — the values Excitel's own
Remote Config uses for `wlan_status_retry`).

---

## Safety model

- **Dry run by default.** A write needs `--apply`; an interactive `yes` is asked for unless
  `--yes` is passed, and a non-interactive shell refuses to write without `--yes`.
- **One field per invocation.** SSID and passphrase are never changed in the same request.
- **No automatic retries on writes** — the backend queues a device job, so a retry can race it.
  A rejection prints the server's own message and exits 4 with the ONU untouched.
- **Local limits that are hardware limits:** SSID 1..32 bytes, WPA2 passphrase 8..63 characters,
  no control characters, no leading/trailing whitespace, and a no-op SSID is refused (`--force`).
- **Secrets:** the passphrase is redacted in every printed body; the cookie lives in
  `.excitel-session.json` with mode `0600` (add it to `.gitignore` — it is a live credential).

Exit codes: `0` ok / `1` aborted / `2` task failed / `3` poll timeout / `4` API error.

---

## About the `Excitel_` prefix

The app forces every SSID to start with `Excitel_` — one string concatenation in its UI layer.
The API field is free-form and **the backend does not enforce it**: setting `MyNet` on the 2.4 GHz
radio (2026-11-07) queued job `5896504`, which reached `completed`, and the ONU now broadcasts
`MyNet`. The passphrase was untouched because `skipKey=true`.

Revert with:

```bash
./app/set-ssid.py set-ssid --radio 2.4 --ssid <SSID-2.4> --apply
```

---

## Troubleshooting

| symptom | cause / fix |
|---|---|
| `this host answered with an HTML page, not the API` | base URL is wrong — Excitel's API lives under `/api/index_dev.php/`, not at the host root (the root is a ticketing portal) |
| `success=false … Invalid username or password` | wrong password *or* a JSON-encoded client; this script posts form data, so check the password itself |
| `no accounts associated with this phone number` | your phone is on the account but is not an OTP login identity — use `login` with the username shown in the app under *Account Info* |
| `success=false … Not logged in!` (401) | cookie missing/expired — `login` again |
| `show` returns `data: []` | `subscriberId` wrong or empty — run `whoami` first; the write path depends on it |
| `task ended failed` | the backend refused the change; nothing reached the ONU. Re-run `show` to confirm state |

---

## Scope

Your account, your CPE. This writes to your own ONT through an API your own app already uses.
There is no admin-password endpoint, no reboot, no port-forwarding, no TR-069 surface here — the
provisioning namespace is WLAN-only (see `APP-API.md`). Don't point it at an account or a device
that isn't yours.
