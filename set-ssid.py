#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27,<1",
#   "rich>=13.7,<15",
# ]
# ///
"""
set-ssid.py — change this ONU's Wi-Fi SSID / passphrase through Excitel's own
provisioning API, bypassing the app's cosmetic "Excitel_" prefix.

Run it with uv (the shebang does this for you):

    ./set-ssid.py --help
    uv run set-ssid.py --help

Everything here was derived from static analysis of the Excitel app 3.41.0
(see APP-API.md in this folder). Endpoints, auth model and field names:

    base = https://selfcare.north.excitel.in/api/index_dev.php/   (Remote Config: procare_config)

    POST <base>/login                              {username,password,isMobile,isMyExcitel}
        -> Set-Cookie: selfcare=...  (the only credential; there is no bearer token)
        -> result.subscriberData.subscriberId (int)

    POST <base>/otp/requestByPhone                  {phone, isMobile:1}
    POST <base>/otp/verify/login                    {phone, otpCode, isMobile:1}
        -> also sets the selfcare cookie; no subscriberId in the body, so we then
           GET <base>/subscriber/showDashboard to learn subscriberData.subscriberId

    POST <base>/provision/getWlanInfo               {subscriberId}
        -> result: {data:[WlanInfo...], connectedDevices:[...], metrics:[...], vendor}

    POST <base>/provision/setWlanInfo               SSID change:
        {subscriberId, idx, ssid, broadCastSsid, radioType, enable, mode,
         security, type, skipKey:true, vendor}
                                                    key change: same + key, minus skipKey
        -> result: {message, taskId}

    POST <base>/provision/netSense/checkTaskStatus  {taskId}
        -> result: {id, status: pending|in-progress|completed|failed|canceled,
                    title, error, retryTimeout, serialNumber}

The app's UI is the only thing that prepends "Excitel_" (a string concatenation
in ChangeSSIDView before the request is built); the API field is free-form.

DESIGN RULES
  * read-only by default — nothing is written without --apply
  * exactly one field per invocation (ssid XOR key); the ONU's own values for
    security/mode/type/radioType/enable/vendor are always echoed back verbatim
    from a fresh getWlanInfo, never invented
  * a write is never retried automatically: setWlanInfo queues a device job, so
    a retry can race the first one
  * SSID <= 32 bytes and WPA2 key 8..63 chars are enforced locally because they
    are hardware limits, not guesses

NOTHING HERE IS SANCTIONED BY THE APP. It writes your own CPE through an API the
UI already uses; the prefix rule may also be enforced server-side, in which case
the job fails and the device is untouched. Change one thing, then stop.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import typing as t
import urllib.parse

import httpx
from rich import box as rich_box
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

# Real production base, read out of Firebase Remote Config key `procare_config`
# (the APK only holds a bare host; the API lives under an `api/index_dev.php/` prefix).
DEFAULT_BASE = os.environ.get("EXCITEL_API_BASE",
                              "https://selfcare.north.excitel.in/api/index_dev.php/")
COOKIE_NAME = "selfcare"
DEFAULT_SESSION = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".excitel-session.json")
UA = "okhttp/5.4.0"  # what the app actually sends (captured 2026-11-07)
TASK_DONE = {"completed", "failed", "canceled"}  # TaskStatus terminal states
# defaults straight from Remote Config `wlan_status_retry`
REMOTE_RETRY_INTERVAL = 30.0
REMOTE_TOTAL_TIMEOUT = 360.0

console = Console(stderr=False)
err = Console(stderr=True)


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
class ApiError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, body: str = ""):
        super().__init__(msg)
        self.status = status
        self.body = body


class Api:
    """Thin wrapper over httpx. Auth is the `selfcare` cookie and nothing else."""

    def __init__(self, base: str, *, insecure: bool = False, timeout: float = 45.0,
                 verbose: bool = False):
        self.base = base.rstrip("/")
        self.verbose = verbose
        self.cookie: str | None = None
        self.client = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 20.0)),
            verify=not insecure,
            headers={"User-Agent": UA, "Accept": "application/json"},
        )

    def close(self) -> None:
        self.client.close()

    # the app replays the cookie on every path except ones containing "login"
    def _headers(self, path: str, *, json_body: bool) -> dict[str, str]:
        h = {}
        if json_body:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        if self.cookie and "login" not in path:
            h["Cookie"] = f"{COOKIE_NAME}={self.cookie}"
        return h

    @staticmethod
    def _form(payload: dict[str, t.Any]) -> dict[str, str]:
        out = {}
        for k, v in payload.items():
            if v is None:
                continue
            if isinstance(v, bool):
                out[k] = "true" if v else "false"
            else:
                out[k] = str(v)
        return out

    def _send(self, method: str, path: str, payload: dict[str, t.Any] | None) -> httpx.Response:
        url = f"{self.base}/{path.lstrip('/')}"
        if self.verbose:
            shown = redact(payload) if payload else None
            err.print(f"  → {method} {url} {json.dumps(shown, ensure_ascii=False) if shown else ''}".rstrip(),
                      style="dim")
        try:
            kw = {}
            if payload is not None:
                # the app posts application/x-www-form-urlencoded (Retrofit @FormUrlEncoded),
                # NOT JSON — sending JSON makes the server see empty fields
                kw["data"] = self._form(payload)
            resp = self.client.request(method, url, headers=self._headers(path, json_body=payload is not None), **kw)
        except httpx.HTTPError as e:
            raise ApiError(f"transport error talking to {url}: {e!r}") from e
        if self.verbose:
            err.print(f"  ← {resp.status_code} {trim(resp.text)}", style="dim")
        return resp

    def _result(self, path: str, resp: httpx.Response) -> t.Any:
        raw = resp.text
        try:
            doc = json.loads(raw)
        except ValueError:
            head = raw.lstrip()[:200].lower()
            if head.startswith("<!doctype html") or head.startswith("<html"):
                raise ApiError(
                    f"{path}: this host answered with an HTML page, not the API (HTTP "
                    f"{resp.status_code}) — the base URL is wrong. Excitel's API lives under "
                    f"…/api/index_dev.php/ : try --base-url "
                    f"{DEFAULT_BASE}", resp.status_code, trim(raw, 300)) from None
            raise ApiError(f"{path}: non-JSON response (HTTP {resp.status_code})",
                           resp.status_code, trim(raw, 400)) from None
        if not isinstance(doc, dict) or "success" not in doc:
            raise ApiError(f"{path}: unexpected envelope (HTTP {resp.status_code})",
                           resp.status_code, trim(raw, 400))
        if not doc.get("success"):
            st = doc.get("status") or {}
            msgs = [(m.get("text") or m.get("message")) if isinstance(m, dict) else str(m)
                    for m in (doc.get("messages") or [])]
            detail = "; ".join(x for x in [st.get("message"), *msgs] if x)
            raise ApiError(f"{path}: success=false code={st.get('code')} {detail}".strip(),
                           resp.status_code, trim(raw, 400))
        return doc.get("result")

    def post(self, path: str, payload: dict[str, t.Any]) -> t.Any:
        return self._result(path, self._send("POST", path, payload))

    def get(self, path: str) -> t.Any:
        return self._result(path, self._send("GET", path, None))

    def raw_post(self, path: str, payload: dict[str, t.Any]) -> httpx.Response:
        """For login/OTP: we need the Set-Cookie header before the envelope is judged."""
        return self._send("POST", path, payload)


def redact(payload: dict[str, t.Any] | None) -> dict[str, t.Any] | None:
    if not payload:
        return payload
    out = dict(payload)
    if "key" in out:
        out["key"] = f"<redacted {len(str(out['key']))} chars>"
    if "password" in out:
        out["password"] = "<redacted>"
    return out


def trim(s: str, n: int = 300) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"


def set_cookie_from(resp: httpx.Response) -> str | None:
    for h in resp.headers.get_list("set-cookie"):
        for part in h.split(";"):
            k, _, v = part.strip().partition("=")
            if k.strip() == COOKIE_NAME:
                return v.strip()
    return None


# --------------------------------------------------------------------------- #
# session file (cookie + subscriberId + base url), mode 600
# --------------------------------------------------------------------------- #
def load_session(path: str) -> dict[str, t.Any]:
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except ValueError:
        raise SystemExit(f"session file {path} is not valid JSON") from None


def save_session(path: str, sess: dict[str, t.Any]) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(sess, fh, indent=2)
    os.replace(tmp, path)
    console.print(f"[dim]session saved to {path} (mode 600 — it holds your {COOKIE_NAME} cookie)[/dim]")


def read_password(args: argparse.Namespace, prompt: str) -> str:
    """--password, or --password-stdin (no shell history), or an interactive prompt."""
    if getattr(args, "password", None):
        return args.password
    if getattr(args, "password_stdin", False):
        pw = sys.stdin.readline().rstrip("\n")
        if not pw:
            raise SystemExit("--password-stdin given but stdin was empty")
        return pw
    if not sys.stdin.isatty():
        raise SystemExit("no password: pass --password, --password-stdin, or run interactively")
    return Prompt.ask(prompt, password=True, console=console)


def normalise_cookie(raw: str | None) -> str | None:
    """Accept `abc`, `selfcare=abc`, or a full `selfcare=abc; path=/; expires=...` string."""
    if not raw:
        return raw
    v = raw.strip().strip('"')
    if COOKIE_NAME + "=" in v:
        v = v.split(COOKIE_NAME + "=", 1)[1]
    return v.split(";", 1)[0].strip()


def make_api(args: argparse.Namespace, sess: dict[str, t.Any]) -> Api:
    base = args.base_url or sess.get("base_url") or DEFAULT_BASE
    api = Api(base, insecure=args.insecure, timeout=args.timeout, verbose=args.verbose)
    raw = args.cookie.strip() if args.cookie else sess.get("cookie")
    api.cookie = normalise_cookie(raw)
    return api


def subscriber_id(args: argparse.Namespace, sess: dict[str, t.Any]) -> int:
    val = args.subscriber_id if args.subscriber_id is not None else sess.get("subscriber_id")
    if val is None:
        raise SystemExit("no subscriberId — run `otp` or `login` first, or pass --subscriber-id")
    return int(val)


def adopt_session(api: Api, token: str, args: argparse.Namespace) -> int:
    """Cookie in hand → resolve subscriberId via the dashboard, then persist."""
    api.cookie = token
    sid = code = username = can_rm = None
    try:
        sd = (api.get("subscriber/showDashboard") or {}).get("subscriberData") or {}
        sid = sd.get("subscriberId")
        code = sd.get("subscriberCode")
        username = sd.get("username")
        can_rm = (sd.get("accesses") or {}).get("canDoOnuRemoteManagement")
    except ApiError as e:
        err.print(f"[yellow]warning:[/yellow] dashboard unreadable ({e}); "
                  f"pass --subscriber-id on later runs")
    acl = {True: "[green]yes[/green]", False: "[red]no[/red]", None: "[dim]unknown[/dim]"}[can_rm]
    console.print(f"logged in — subscriberId [bold]{sid}[/bold] "
                  f"code {code} canDoOnuRemoteManagement={acl}")
    if can_rm is False:
        console.print("[yellow]the backend says this account may not manage the ONU — "
                      "expect the Wi-Fi screen (and these calls) to be refused[/yellow]")
    save_session(args.session, {
        "base_url": api.base, "cookie": token, "subscriber_id": sid,
        "subscriber_code": code, "username": username, "logged_in_at": int(time.time()),
    })
    return 0


# --------------------------------------------------------------------------- #
# commands: auth
# --------------------------------------------------------------------------- #
def cmd_otp(args: argparse.Namespace, sess: dict[str, t.Any]) -> int:
    api = make_api(args, {})
    # the app validates "10 digits" client-side and sends the field verbatim, no +91
    digits = args.phone.lstrip("+")
    if not (digits.isdigit() and len(digits) == 10):
        console.print(f"[yellow]note:[/yellow] sending {args.phone!r} as-is — the app itself only "
                      f"accepts 10 digits, so anything else probably has no account behind it")
    api.post("otp/requestByPhone", {"phone": args.phone, "isMobile": 1})
    console.print(f"OTP requested for [bold]{args.phone}[/bold]")
    code = args.code or Prompt.ask("enter the OTP", password=True, console=console)
    resp = api.raw_post("otp/verify/login",
                        {"phone": args.phone, "otpCode": code.strip(), "isMobile": 1})
    token = set_cookie_from(resp)
    if token is None:
        try:
            d = json.loads(resp.text)
            txt = " ".join(str(m.get("text") or "") for m in (d.get("messages") or [])
                           if isinstance(m, dict))
        except ValueError:
            txt = trim(resp.text, 200)
        raise SystemExit(f"otp/verify/login set no {COOKIE_NAME} cookie "
                         f"(HTTP {resp.status_code}) — {txt or 'no message'}")
    if resp.is_success:
        try:
            if json.loads(resp.text).get("success") is False:
                raise SystemExit(f"OTP rejected: {trim(resp.text, 200)}")
        except ValueError:
            pass
    return adopt_session(api, token, args)


def cmd_forgot(args: argparse.Namespace, sess: dict[str, t.Any]) -> int:
    """The app's "Forgot password" flow: POST password/resend {username, phone}."""
    api = make_api(args, {})
    console.print(f"POST /password/resend  username=[bold]{args.user}[/bold] phone=[bold]{args.phone}[/bold]")
    console.print("[yellow]this is the app's password-reset call[/yellow] — it acts on the account "
                  "(SMS/email with a reset).")
    if not args.apply:
        console.print("[yellow]DRY RUN[/yellow] — nothing sent. Re-run with --apply to send it.")
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("refusing to send a password reset without confirmation — pass --yes")
        if not Confirm.ask("send the password reset now?", default=False, console=console):
            return 1
    result = api.post("password/resend", {"username": args.user, "phone": args.phone})
    console.print(JSON(json.dumps(result)) if result else "sent (empty result)")
    return 0


def cmd_login(args: argparse.Namespace, sess: dict[str, t.Any]) -> int:
    api = make_api(args, {})
    password = read_password(args, "Excitel account password")
    resp = api.raw_post("login", {"username": args.user, "password": password,
                                  "isMobile": 1, "isMyExcitel": 1})
    token = set_cookie_from(resp)
    try:
        doc = json.loads(resp.text)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise SystemExit(f"login: HTTP {resp.status_code}, non-JSON response — {trim(resp.text, 200)}")
    if doc.get("success") is not True:
        txt = " ".join(str(m.get("text") or m.get("message") or "")
                       for m in (doc.get("messages") or []) if isinstance(m, dict))
        if "invalid username or password" in txt.lower():
            raise SystemExit(
                "login rejected: invalid username or password.\n"
                "The app sends the password verbatim (no hashing, no trimming — verified in\n"
                "UserApiDataSource.loginUsername), so the string must match character for character.\n"
                "Re-run without --password to be prompted (no echo, no shell history), or pipe it\n"
                "with --password-stdin. Do not retry in a loop — these portals lock accounts.")
        raise SystemExit(f"login rejected: {txt or trim(resp.text, 200)}")
    if token is None:
        raise SystemExit(f"login succeeded but no {COOKIE_NAME} cookie came back "
                         f"(HTTP {resp.status_code}) — nothing to replay")
    sd = ((doc.get("result") or {}).get("subscriberData")) or {}
    if sd.get("subscriberId"):
        api.cookie = token
        save_session(args.session, {
            "base_url": api.base, "cookie": token, "subscriber_id": int(sd["subscriberId"]),
            "subscriber_code": sd.get("subscriberCode"), "username": sd.get("username"),
            "logged_in_at": int(time.time()),
        })
        console.print(f"logged in — subscriberId [bold]{sd['subscriberId']}[/bold] "
                      f"code {sd.get('subscriberCode')}")
        return 0
    return adopt_session(api, token, args)


# --------------------------------------------------------------------------- #
# commands: read
# --------------------------------------------------------------------------- #
def fetch_wlan(api: Api, sid: int) -> dict[str, t.Any]:
    result = api.post("provision/getWlanInfo", {"subscriberId": sid})
    if not isinstance(result, dict):
        raise SystemExit("getWlanInfo returned an unexpected result")
    # the app's UserRepository copies the envelope `vendor` into every WLAN item
    # (see APP-API.md §4.2) — do the same, so the write echoes what the app echoes
    env_vendor = result.get("vendor") or ""
    for item in result.get("data") or []:
        if not item.get("vendor"):
            item["vendor"] = env_vendor
    return result


def wlan_table(items: list[dict[str, t.Any]]) -> Table:
    tbl = Table(box=None, pad_edge=False, header_style="bold")
    for col, justify in (("idx", "right"), ("radio", "left"), ("ssid", "left"),
                         ("security", "left"), ("mode", "left"), ("type", "left"),
                         ("en", "left"), ("bcast", "left"), ("vendor", "left")):
        tbl.add_column(col, justify=justify)  # type: ignore[arg-type]
    for it in items:
        tbl.add_row(str(it.get("idx", "?")), str(it.get("radioType", "?")),
                    f"[bold]{it.get('ssid', '?')}[/bold]", str(it.get("security", "?")),
                    str(it.get("mode", "?")), str(it.get("type", "?")),
                    str(it.get("enable", "?")), str(it.get("broadCastSsid", "?")),
                    str(it.get("vendor") or "-"))
    return tbl


def cmd_whoami(args: argparse.Namespace, sess: dict[str, t.Any]) -> int:
    """GET /subscriber/showDashboard with the cookie we already have — resolves subscriberId."""
    api = make_api(args, sess)
    if not api.cookie:
        raise SystemExit("no cookie — run `otp`, `login`, or pass --cookie")
    sd = (api.get("subscriber/showDashboard") or {}).get("subscriberData") or {}
    if not sd:
        raise SystemExit("dashboard returned no subscriberData")
    sid = sd.get("subscriberId")
    can_rm = (sd.get("accesses") or {}).get("canDoOnuRemoteManagement")
    acl = {True: "[green]yes[/green]", False: "[red]no[/red]", None: "[dim]unknown[/dim]"}[can_rm]
    console.print(f"subscriberId [bold]{sid}[/bold]  code {sd.get('subscriberCode')}  "
                  f"tenant {sd.get('tenantName')}  username {sd.get('username')}")
    console.print(f"name {sd.get('name')!r}  city {sd.get('city')!r}  "
                  f"service {sd.get('serviceName')!r}  status {sd.get('status')!r}")
    console.print(f"canDoOnuRemoteManagement = {acl}")
    if args.json:
        console.print(JSON(json.dumps(sd)))
    if sid:
        sess.update({"base_url": api.base, "cookie": api.cookie, "subscriber_id": int(sid),
                     "subscriber_code": sd.get("subscriberCode"),
                     "username": sd.get("username")})
        save_session(args.session, sess)
    return 0


def cmd_show(args: argparse.Namespace, sess: dict[str, t.Any]) -> int:
    api = make_api(args, sess)
    wlan = fetch_wlan(api, subscriber_id(args, sess))
    if args.json:
        console.print(JSON(json.dumps(wlan)))
        return 0
    console.print(f"vendor [cyan]{wlan.get('vendor')!r}[/cyan]")
    console.print(wlan_table(wlan.get("data") or []))
    cd = wlan.get("connectedDevices") or []
    if cd:
        lines = [f"{str(d.get('macAddress', '?')):<18} {str(d.get('ip', '?')):<16} "
                 f"rssi={d.get('rssi')} band={d.get('band')} host={d.get('hostname')!r}"
                 for d in cd]
        console.print(Panel("\n".join(lines), title=f"connected devices ({len(cd)})",
                        box=rich_box.SIMPLE, title_align="left"))
    return 0


# --------------------------------------------------------------------------- #
# commands: write
# --------------------------------------------------------------------------- #
def pick_radio(wlan: dict[str, t.Any], idx: int | None, radio: str | None) -> dict[str, t.Any]:
    items = wlan.get("data") or []
    if not items:
        raise SystemExit("getWlanInfo returned no WLAN entries")
    if idx is None and radio is None:
        if len(items) == 1:
            return items[0]
        console.print(wlan_table(items))
        raise SystemExit("several radios — pick one with --idx N or --radio 2.4|5")
    chosen = [it for it in items
              if (idx is None or it.get("idx") == idx)
              and (radio is None or radio.lower().strip("ghz.") in str(it.get("radioType", "")).lower())]
    if len(chosen) != 1:
        console.print(wlan_table(items))
        raise SystemExit(f"selection matched {len(chosen)} entries — narrow it with --idx")
    return chosen[0]


def build_body(target: dict[str, t.Any], sid: int) -> dict[str, t.Any]:
    """Echo the ONU's own values verbatim — the backend applies them, it does not just route by them."""
    return {
        "subscriberId": sid,
        "idx": target["idx"],
        "ssid": target["ssid"],
        "broadCastSsid": bool(target.get("broadCastSsid", True)),
        "radioType": target.get("radioType", ""),
        "enable": bool(target.get("enable", True)),
        "mode": target.get("mode", ""),
        "security": target.get("security", ""),
        "type": target.get("type", ""),
        "vendor": target.get("vendor") or "",
    }


def run_write(api: Api, body: dict[str, t.Any], *, apply: bool, yes: bool) -> int:
    console.print("\n[bold]POST /provision/setWlanInfo[/bold] "
                  "[dim](application/x-www-form-urlencoded)[/dim]")
    form = Api._form(body)
    if "key" in form:
        form["key"] = f"<redacted {len(form['key'])} chars>"
    console.print(urllib.parse.urlencode(form).replace("&", "&\n"))
    if not apply:
        console.print("\n[yellow]DRY RUN[/yellow] — nothing sent. Re-run with [bold]--apply[/bold] to write.")
        return 0
    if not yes:
        if not sys.stdin.isatty():
            raise SystemExit("refusing to write without a confirmation in a non-interactive shell — "
                             "either run interactively or pass --yes")
        if not Confirm.ask("send this to Excitel now?", default=False, console=console):
            console.print("aborted")
            return 1
    try:
        result = api.post("provision/setWlanInfo", body) or {}
    except ApiError as e:
        # the interesting case: rejected => the job was never queued, CPE untouched
        raise ApiError(f"write rejected — the ONU was not touched: {e}", e.status, e.body) from e
    task_id = result.get("taskId")
    console.print(f"queued: {result.get('message')!r} taskId={task_id}")
    if task_id is None:
        console.print("no taskId returned — check with `show` in a minute")
        return 0
    return poll_task(api, int(task_id), interval=REMOTE_RETRY_INTERVAL,
                     timeout=REMOTE_TOTAL_TIMEOUT)


def poll_task(api: Api, task_id: int, *, interval: float, timeout: float,
              status: t.Any = None) -> int:
    """status: a rich Status spinner (optional) so polling looks alive."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            res = api.post("provision/netSense/checkTaskStatus", {"taskId": task_id}) or {}
        except ApiError as e:
            console.print(f"[yellow]poll error (retrying):[/yellow] {e}")
            time.sleep(interval)
            continue
        st = str(res.get("status", "")).lower()
        if (st, res.get("title")) != last:
            line = f"{st} {res.get('title') or ''} {res.get('error') or ''}".rstrip()
            (status.update(line) if status else console.print(f"  task {task_id}: {line}"))
            last = (st, res.get("title"))
        if st in TASK_DONE:
            ok = st == "completed"
            (status.stop() if status else None)
            console.print("[green]done[/green] — verify with `show`" if ok
                          else f"[red]task ended {st}[/red]")
            return 0 if ok else 2
        time.sleep(interval)
    (status.stop() if status else None)
    console.print(f"[yellow]gave up waiting after {timeout:.0f}s[/yellow] — "
                  f"the job may still finish; check with `show`")
    return 3


def cmd_set_ssid(args: argparse.Namespace, sess: dict[str, t.Any]) -> int:
    ssid = args.ssid
    if not ssid:
        raise SystemExit("--ssid is required")
    if ssid != ssid.strip():
        raise SystemExit("leading/trailing whitespace in --ssid is almost certainly a mistake")
    raw = ssid.encode()
    if not 1 <= len(raw) <= 32:
        raise SystemExit(f"SSID must be 1..32 bytes on the wire; yours is {len(raw)}")
    if any(b < 0x20 for b in raw):
        raise SystemExit("control characters in SSID")
    api = make_api(args, sess)
    sid = subscriber_id(args, sess)
    target = pick_radio(fetch_wlan(api, sid), args.idx, args.radio)
    if ssid == target.get("ssid") and not args.force:
        raise SystemExit(f"SSID on idx {target['idx']} is already {ssid!r} (use --force to re-send)")
    console.print(f"current: idx={target['idx']} radio={target.get('radioType')} "
                  f"ssid=[bold]{target.get('ssid')}[/bold]")
    prefixed = ssid.startswith("Excitel_")
    console.print(f"target : idx={target['idx']} ssid=[bold]{ssid}[/bold] "
                  f"(prefix: {'Excitel_ kept' if prefixed else 'none — sent verbatim, as the API allows'})")
    body = build_body(target, sid)
    body["ssid"] = ssid
    body["skipKey"] = True                      # leave the passphrase alone
    return run_write(api, body, apply=args.apply, yes=args.yes)


def cmd_set_key(args: argparse.Namespace, sess: dict[str, t.Any]) -> int:
    key = read_password(args, "new Wi-Fi passphrase")
    if args.confirm is not None and args.confirm != key:
        raise SystemExit("--password and --confirm differ")
    if not 8 <= len(key) <= 63:
        raise SystemExit(f"WPA2 passphrase must be 8..63 characters; yours is {len(key)}")
    if any(ord(c) < 0x20 or ord(c) > 0x7E for c in key):
        console.print("[yellow]note:[/yellow] non-ASCII/control characters in a PSK are legal "
                      "but some clients mishandle them")
    api = make_api(args, sess)
    sid = subscriber_id(args, sess)
    target = pick_radio(fetch_wlan(api, sid), args.idx, args.radio)
    console.print(f"current: idx={target['idx']} radio={target.get('radioType')} "
                  f"ssid=[bold]{target.get('ssid')}[/bold]")
    body = build_body(target, sid)
    body["key"] = key                            # no skipKey on this overload
    return run_write(api, body, apply=args.apply, yes=args.yes)


def cmd_task(args: argparse.Namespace, sess: dict[str, t.Any]) -> int:
    api = make_api(args, sess)
    return poll_task(api, args.task_id, interval=args.poll_interval, timeout=args.poll_timeout)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="set-ssid.py",
        description="Change this ONU's Wi-Fi SSID/passphrase via Excitel's provisioning API "
                    "(no 'Excitel_' prefix). Read-only unless --apply.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="endpoints: /login, /otp/*, /subscriber/showDashboard, "
               "/provision/getWlanInfo, /provision/setWlanInfo, /provision/netSense/checkTaskStatus")
    p.add_argument("--base-url", default=None,
                   help=f"selfcare host (default: $EXCITEL_API_BASE or {DEFAULT_BASE})")
    p.add_argument("--session", default=DEFAULT_SESSION, help="session file (default: %(default)s)")
    p.add_argument("--cookie", default=None, help=f"pass the {COOKIE_NAME} cookie value directly")
    p.add_argument("--subscriber-id", type=int, default=None, help="override stored subscriberId")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification (proxy debugging)")
    p.add_argument("--timeout", type=float, default=45.0, help="per-request timeout seconds")
    p.add_argument("-v", "--verbose", action="store_true", help="log requests/responses to stderr")
    sub = p.add_subparsers(dest="cmd", required=True)

    op = sub.add_parser("otp", help="phone + OTP login (what the app does)")
    op.add_argument("--phone", required=True, help="phone number on the account")
    op.add_argument("--code", default=None, help="OTP (prompted if omitted)")
    op.set_defaults(func=cmd_otp)

    fp = sub.add_parser("forgot", help="app's password-reset call (password/resend)")
    fp.add_argument("--user", required=True, help="username on the account")
    fp.add_argument("--phone", required=True, help="phone number on the account")
    fp.add_argument("--apply", action="store_true", help="actually send it")
    fp.add_argument("--yes", action="store_true", help="skip the confirmation")
    fp.set_defaults(func=cmd_forgot)

    lp = sub.add_parser("login", help="authenticate with the account password")
    lp.add_argument("--user", required=True, help="username used in the app")
    lp.add_argument("--password", default=None, help="password (prompted if omitted)")
    lp.add_argument("--password-stdin", action="store_true",
                    help="read the password from stdin (keeps it out of shell history)")
    lp.set_defaults(func=cmd_login)

    wp = sub.add_parser("whoami", help="resolve subscriberId from the dashboard (needs a cookie)")
    wp.add_argument("--json", action="store_true", help="dump subscriberData")
    wp.set_defaults(func=cmd_whoami)

    sp = sub.add_parser("show", help="read current WLAN config (no writes)")
    sp.add_argument("--json", action="store_true", help="dump the raw getWlanInfo result")
    sp.set_defaults(func=cmd_show)

    ss = sub.add_parser("set-ssid", help="change the SSID on one radio")
    ss.add_argument("--ssid", required=True, help="exact new SSID, sent verbatim (no prefix added)")
    ss.add_argument("--idx", type=int, default=None, help="WLAN idx from `show`")
    ss.add_argument("--radio", default=None, help="match radioType, e.g. 2.4 or 5")
    ss.add_argument("--apply", action="store_true", help="actually send the request")
    ss.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    ss.add_argument("--force", action="store_true", help="re-send even if the SSID is unchanged")
    ss.set_defaults(func=cmd_set_ssid)

    sk = sub.add_parser("set-key", help="change the Wi-Fi passphrase on one radio")
    sk.add_argument("--password", default=None, help="new passphrase (prompted if omitted)")
    sk.add_argument("--password-stdin", action="store_true", help="read the passphrase from stdin")
    sk.add_argument("--confirm", default=None, help="repeat the passphrase")
    sk.add_argument("--idx", type=int, default=None)
    sk.add_argument("--radio", default=None)
    sk.add_argument("--apply", action="store_true")
    sk.add_argument("--yes", action="store_true")
    sk.set_defaults(func=cmd_set_key)

    tp = sub.add_parser("task", help="poll an existing provisioning task")
    tp.add_argument("task_id", type=int)
    tp.add_argument("--poll-interval", type=float, default=REMOTE_RETRY_INTERVAL)
    tp.add_argument("--poll-timeout", type=float, default=REMOTE_TOTAL_TIMEOUT)
    tp.set_defaults(func=cmd_task)

    args = p.parse_args(argv)
    sess = load_session(args.session)
    try:
        return args.func(args, sess)
    except ApiError as e:
        err.print(f"[red]error:[/red] {e}")
        if e.body:
            err.print(f"[dim]body: {e.body}[/dim]")
        return 4
    except KeyboardInterrupt:
        err.print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
