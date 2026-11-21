# APP-API.md — what the Excitel app actually calls

**Date:** 2026-11-07
**Analysed artifact:** `app/app/apk/myexcitel-3.41.0-base.apk`
**Package:** `com.scaleforce.mobile.myexcitel`
**Version:** `versionName=3.41.0`, `versionCode=556`, minSdk 23, targetSdk 37, single `base.apk` (no splits), installer `com.android.vending`
**SHA-256:** `bd79aee28f16b1beafb0694eceef22da1c2674b693c37b08497afa3924e833bc` (15,952,814 bytes)
**Pull:** `adb pull "/data/app/~~DOeCJbGpTUTsvDxupINv8g==/com.scaleforce.mobile.myexcitel-pVqYrZn_fgZn3EYq8GjZXQ==/base.apk"`
**Method:** static only — `jadx 1.5.6` full decompile (`/tmp/.../jadx-out`), plus `strings` on dex/`resources.arsc`. **No live traffic capture yet** (see §9).

Native Kotlin app (not React Native/Flutter): 3 dex files, Retrofit + OkHttp + Moshi, Hilt, Zendesk SDK, Firebase (RC/FCM/Analytics/Crashlytics), OpenTelemetry.

---

## 1. Verdict (answers the handoff's six questions)

1. **What does the app call?** A plain REST/JSON call to Excitel's own selfcare backend:
   `POST https://<selfcare-host>/provision/setWlanInfo` — **one endpoint for both SSID and Wi-Fi key**,
   distinguished by whether the `key` field is present. It is *not* SOAP, not TR-069/CWMP from the
   phone, not a command queue. The backend does the ACS write.
2. **Request body?** Excitel-invented field names (`subscriberId`, `idx`, `ssid`, `key`, `broadCastSsid`,
   `radioType`, `enable`, `mode`, `security`, `type`, `skipKey`, `vendor`) — **no TR-069 parameter paths
   anywhere in the app**. But several of the fields are clearly ONU-side values round-tripped from
   `getWlanInfo` (`radioType`, `type`, `mode`, `security`, `vendor`, `idx`), i.e. the backend is a
   parameter-level passthrough to the CPE.
3. **Device identifier in payload?** Only `subscriberId` (integer) — the ONU is resolved server-side from
   the subscriber record. No serial, no MAC, no account code in the WLAN calls. The one endpoint that does
   take hardware IDs is `provision/checkOnuDeviceClientTraffic` (`serialNumber` + `macAddress`).
4. **Admin password visible anywhere?** **No.** Nothing in the API surface, any request body, any response
   model, or any resource string references the ONT web-UI / `root` account. The app never talks to
   `192.168.1.1`, `boaform`, TR-069, or port 7547. `provision/getWlanInfo` does **not** return the Wi-Fi
   key either (no `key`/`password` field in `WlanInfo`).
5. **Other settable parameters?** Almost none — see §7. The provision namespace is four endpoints, all
   WLAN-read/status; there is **no** reboot, factory reset, port forwarding, UPnP, TR-069 or user-management
   endpoint. Everything else is billing/tickets/OTP/CAF.
6. **Auth model?** Session **cookie** named `selfcare`, no bearer token, no `Authorization` header anywhere.
   `POST /login {username, password, isMobile:1, isMyExcitel:1}`. Authorization per-device is server-side
   keyed on `subscriberId`; the ID is a small sequential integer sent in the body of every call — that is an
   IDOR *risk surface*, reported in §8 and **not tested**.

---

## 2. Transport, hosts, auth

### Base URL is remote-config-driven — and the real one is NOT the bare host

**Resolved 2026-11-07** by reading the app's own Firebase Remote Config (see §2.1). Production base:

```
https://selfcare.north.excitel.in/api/index_dev.php/
```

i.e. the API lives under an **`api/index_dev.php/`** path prefix (PHP front controller), while
`https://selfcare.north.excitel.in/` on its own — a CNAME to `del-ticketing-lb.excitel.in` — serves an Excitel
HTML **ticketing/portal** page ("Excitel", `campaigns/login/login.css`). Because the interceptor also rewrites
the *path prefix*, using the bare host yields HTML, not JSON. Confirmed live: an unauthenticated
`POST …/api/index_dev.php/provision/getWlanInfo` returns the proper envelope
`{"status":{"message":"Unauthorized","code":401},"success":false,…,"messages":[{"id":0,"type":"error","text":"Not logged in!",…}]}`.

All three environments, from the same RC key `procare_config`:

| env | base | 
|---|---|
| dev / dev_preview | `https://selfcare-qa.north.excitel.in/api/index_dev.php/` |
| staging / staging_preview | `https://selfcare-prerelease.north.excitel.in/api/index_dev.php/` |
| production / production_preview | `https://selfcare.north.excitel.in/api/index_dev.php/` |

Retrofit is built with the bare host `https://selfcare.north.excitel.in/` as a placeholder, and an application
interceptor rewrites scheme/host/port **and prepends the configured path prefix** on every request (which is
how the `/api/index_dev.php` part is applied):

```
defpackage/jr0.java   (SelfCareModule, Retrofit providers)
  case 10/11:  baseUrl("https://selfcare.north.excitel.in/")
               .addInterceptor(new a62(moshi, firebaseDataSource))     // host rewrite
               .addNetworkInterceptor(new ly4(moshi, prefs))           // error-body parsing only
               .cookieJar(new hk4(prefs))
defpackage/a62.java
  host = SelfCareConfig.devUrl | stagingUrl | productionUrl    (per app env)
```

`SelfCareConfig` comes from **Firebase Remote Config key `procare_config`**
(`FirebaseDataSourceImpl.kt` → `data/datasource/a.java#d()`), fields
`dev_url, dev_preview_url, staging_url, staging_preview_url, production_url, production_preview_url`.
Fallback when RC is empty is the hardcoded `selfcare.north.excitel.in`.

Three API hosts, same pattern:

| Host (placeholder) | RC key | Interface | Purpose |
|---|---|---|---|
| `https://selfcare.north.excitel.in/` | `procare_config` | `defpackage/mi4.java` | everything in §3–§5 |
| `https://wfboapi.azurewebsites.net/` | `workforce_config` | `defpackage/pj5.java` | install workflow / CAF / plans (partner API) |
| `https://loctrack-api.apps.excitel.tech/` | `locTrack_config` | `defpackage/gx2.java` | installer location ping |
| `https://internalcheck.excitel.com` | — | `defpackage/fy1.java` | bare `GET /` connectivity probe |
| `https://myexcitel-android[-qa|-prerelease].otel.excitel.tech/` | — | `n51.java` | OTel OTLP exporter |
| `https://links-my.excitel.com` | — | manifest | App Link (`autoVerify=true`, verified) |

TLS: `OkHttpClient` uses `connectionSpecs(COMPATIBLE_TLS, CLEARTEXT)`, **no `CertificatePinner`**, no
`networkSecurityConfig`, `android:usesCleartextTraffic="false"`. So: system trust anchors only.

### 2.1 How the base URL was recovered (read-only)

The APK carries the Firebase client identity in `res/values/strings.xml`, so the app's own Remote Config can
be fetched over the public RC REST API — no device, no proxy, no account:

```
google_api_key   AIzaSy…(strings.xml)
google_app_id    1:747821811677:android:5d6554fdede00822
project_id       my-excitel
```

```bash
CERT=E1B76C8AAA4940DE0CC90F274EC9AE6E1040A0FB        # keytool -printcert -jarfile base.apk (SHA1)
KEY=AIzaSy…

curl -s -X POST \
  'https://firebaseremoteconfig.googleapis.com/v1/projects/my-excitel/namespaces/firebase:fetch' \
  -H "x-goog-api-key: $KEY" -H 'X-Android-Package: com.scaleforce.mobile.myexcitel' \
  -H "X-Android-Cert: $CERT" -H 'Content-Type: application/json' \
  -d '{"app_instance_id":"<any-uuid4>","app_id":"1:747821811677:android:5d6554fdede00822",
       "language_code":"en-US","country_code":"IN","platform_version":"Android 15",
       "time_zone":"Asia/Kolkata","app_version":"3.41.0","app_build":"556",
       "package_name":"com.scaleforce.mobile.myexcitel","sdk_version":"22.1.2"}'
```

The `X-Android-Package`/`X-Android-Cert` headers are mandatory: the API key is restricted to that package +
signing certificate, and without them the call fails `API_KEY_ANDROID_APP_BLOCKED` ("Requests from this Android
client application <empty> are blocked"). No Firebase Installation/auth token was needed for the fetch.

Dump saved as `remoteconfig.json` (templateVersion in file). Keys of interest:

| RC key | value | why it matters |
|---|---|---|
| `procare_config` | bases above | the real API host + `/api/index_dev.php/` prefix |
| `wlan_status_retry` | `{"retry_timeout":30,"total_timeout":360}` | how long the app polls `checkTaskStatus` (script defaults match) |
| `minimal_version_code_android` | `554` | app 3.41.0 / code 556 is current-forced |
| `net_bot_config` | `{qa,staging,prod}` → `{baseUrl, isActive, keyName:"myapp", key:"…"}` | the `?keyname=&key=` Azure-Function keys used by `pj5` (see §8.5) |
| `workforce_config` | `…azurewebsites.net/api/` | workforce host, also with an `/api/` prefix |
| `locTrack_config` | `…apps.excitel.tech/api/v2.0/` | installer tracking |
| `rssi_ranges`, `city_codes`, `pre_tenants`, `pro_tenants` | — | client-side display/feature gates |

### Auth

* **No** `Authorization`, `X-Api-Key`, or bearer token is ever added by the app. Only `OkHttp`'s own
  `Accept-Encoding` / cache headers exist.
* Login: `POST login`, body `{"username","password","isMobile":1,"isMyExcitel":1}`; the response also carries
  the cookie in `subscriberData.cookie`. Phone+OTP path: `POST loginPhone {username, phone, otpCode, isMobile, isMyExcitel}`
  and `POST otp/verify/login {phone, otpCode, isMobile}`.
* Cookie handling (`defpackage/hk4.java`, `CookieJar`): stores the cookie named **`selfcare`**,
  persisted verbatim in SharedPreferences under key **`excitel-cookie`**; replayed on every request except
  paths containing `login`. There is **no CSRF token** on any API call.
* `subscriberId` (int) — from `login` response `subscriberData.subscriberId` — is the only "who am I" element
  and it travels in the JSON body of essentially every call. Cached in prefs (`excitel-userid`).

---

## 3. Endpoint inventory — selfcare (`defpackage/mi4.java`)

`@dq1` = POST, `@fo3` = path, `@ow1` = GET, `@bi1` = `@Body`-style JSON field, `@qy3` = query param.

> **Encoding correction (verified live 2026-11-07, see §12).** Every `POST` is
> `application/x-www-form-urlencoded` — Retrofit `@Field`/`@FormUrlEncoded`, **not** JSON. In the
> decompiled signatures `@bi1` is `retrofit2.http.Field` and `@qy3` is `retrofit2.http.Query`, so the
> "fields" below are form keys and the `@ow1` GETs take query parameters. Sending JSON makes the
> backend see every field as empty (that is what an "Invalid username or password" on a correct
> password looks like).

### 3.1 Provisioning namespace (the interesting part — 4 endpoints)

| # | Method + path | Body / query fields | Response |
|---|---|---|---|
| P1 | **POST `provision/getWlanInfo`** | `subscriberId` | `ApiResponse<WlanInfoData>` |
| P2 | **POST `provision/setWlanInfo`** (SSID change) | `subscriberId, idx, ssid, broadCastSsid, radioType, enable, mode, security, type, skipKey, vendor` | `ApiResponse<SetWlanInfoResponse>` → `{message, taskId}` |
| P3 | **POST `provision/setWlanInfo`** (key change) | same **+ `key`**, **no `skipKey`** | `ApiResponse<SetWlanInfoResponse>` → `{message, taskId}` |
| P4 | **POST `provision/netSense/checkTaskStatus`** | `taskId` | `ApiResponse<TaskStatusResult>` |
| P5 | POST `provision/checkOnuDeviceClientTraffic` | `serialNumber, macAddress` | `ApiResponse<ConnectedDeviceMetricsResponse>` (read-only: `rssi[]`, `traffic[]` frames) |

P2 and P3 are **the same route**; the app calls the `skipKey=true` overload when changing the name and the
`key=` overload when changing the passphrase.

### 3.2 Everything else (context: what the API *could* do, for Q5)

| Method + path | Fields | Purpose |
|---|---|---|
| POST `login` | username, password, isMobile, isMyExcitel | session |
| POST `loginPhone` | username, phone, otpCode, isMobile, isMyExcitel | session by OTP |
| POST `otp/request` / `otp/requestByPhone` / `otp/verify/confirm` / `otp/verify/login` | userId, userType, notificationChannel(+value), otpValidationType, isMobile / phone, otpCode | OTP |
| POST `password/resend` | username, phone | account password reset |
| POST `subscriber/changePassword` | subscriberId, oldPassword, newPassword, confirmNewPassword | **portal** password (not the ONT) |
| GET `subscriber/showDashboard` | — | dashboard, incl. `accesses.canDoOnuRemoteManagement` |
| GET `subscriber/showSessions` | isMobile / monthId, isMobile | data sessions |
| GET `subscriber/getExtendedFiberData` | subscriberId, partnerId, isMobile | FTTH/ONU info (`installedOnuData{type,serialNumber,macAddress}`) |
| GET `subscriber/getFtthStatus`, `subscriber/ftthSubscriberRequest`, `subscriber/getGstNumber`, `subscriber/changeGstNumber`, `subscriber/addRating`, `subscriber/getShowRating`, `subscriber/showSubscriberOttServices` | — | account ops |
| GET `subscriber/showInvoiceDetails` / `showInvoiceHtml` / `showReceiptHtml` | year, isMobile / voucherNo, paymentMode, yearId / receiptNo | billing docs |
| POST `payment/initiatePaymentRequest`, `payment/isLastTransactionPending`, GET `payment/getPaymentsData`, `payment/getDepositPaymentData` | serviceName, amounts, provider ids, merchantTxnId… | payments (Zendesk/OTP not involved) |
| POST `ticket/insert` | isMobile, phoneNumber, email, description, title, attachments[], ticketTypeId, sourceChannelId, reasonId | **create support ticket** |
| POST `ticket/preInsertConnectivityIssuesTicket` | username | pre-flight for a connectivity complaint |
| POST `ticket/isTechnicalIssueTicketAllowed` | subscriberId, reasonId | gate |
| GET `ticket/list` / `ticket/showTicket` / `ticket/getPreInsertData`, POST `ticket/saveChanges` / `ticket/transitionStatus` | … | ticket lifecycle |
| POST x-ms blob upload (`pj5.i`, `@a12("x-ms-date")`) + `wfboapi…/v2.0/{tenant}/CollectDocumentsMyApp/Document` | keyname, key | docs/CAF upload |

Workforce (`pj5`, `wfboapi.azurewebsites.net`) also exposes `NewLeadMyApp/Create|Cities|Subareas|PlansByCityId|Status`,
`RequestInstallationMyApp/{workflowId}/CAFDetails`, `CollectDocuments/ApprovedDocuments/{workflowId}` — all with
`?keyname=<>&key=<>` **API keys passed as query parameters**.

---

## 4. The Wi-Fi change flow, exactly

Screen chain: `ManageWiFiFragment` (per band: `RemoteManagementType.TYPE_2_4GHZ | TYPE_5GHZ`) →
`ChangeSSIDFragment` / `ChangeWiFiPasswordFragment`.

### 4.1 Read current state — P1

`GetWlanInfo` use case → `UserRepository` → `UserApiDataSource.getWlanInfo` (`UserApiDataSource.kt`):

```kotlin
// com/…/data/datasource/UserApiDataSource$getWlanInfo$2$1.class
api.getWlanInfo(subscriberId)            // POST provision/getWlanInfo  {"subscriberId":<int>}
```

### 4.2 Change the SSID — P2

`ChangeSSIDViewModel` (`defpackage/c80.java`): client prepends a **forced prefix** —
`ChangeSSIDView` submits `res/string.ssid_prefix` (= **`Excitel_`**) + user text (`defpackage/ng.java:293`):

```kotlin
// UserApiDataSource$setWlanSSID$2$1
api.setWlanInfoSSID(
    subscriberId, idx = info.idx, ssid = ssid,
    broadCastSsid = true,                 // hardcoded true
    radioType = info.radioType, enable = info.enable,
    mode = info.mode, security = info.security, type = info.type,
    skipKey = true,                       // <-- "don't touch the passphrase"
    vendor = info.vendor ?: ""
)
```

Wire format (form-encoded). Field names are exact; the `radioType`/`mode`/`security`/`type`/
`vendor` **string values** are server-supplied and unknown until captured, so `<?>` marks a placeholder,
not a finding:

```
Content-Type: application/x-www-form-urlencoded

subscriberId=<subscriberId>&idx=5&ssid=<SSID-2.4>&broadCastSsid=true&radioType=2.4
&enable=true&mode=&security=&type=b,g,n&skipKey=true&vendor=netsense
```
(values above are this subscriber's real ones, captured 2026-11-07; `mode` and `security` really are empty)

(`radioType`, `mode`, `security`, `type`, `enable`, `idx`, `vendor` are **copied verbatim from the
`getWlanInfo` response** — the client is echoing the ONU's own values so the backend can rewrite only the SSID.
None of these literals exist in the APK; they only appear in a live response.

One mechanical detail matters for the ticket wording, from `UserRepository` (`data/repository/e.java`, ~line 553):
`vendor` arrives **once per response** at the top level (`WlanInfoData.vendor`) and the repository copies it
into every `WlanInfo` item before the UI uses it —

```java
for (WlanInfo it : wlanInfoData.getData()) it.n(wlanInfoData.getVendor());   // WlanInfo.setVendor(...)
```

so `vendor` in `setWlanInfo` is whatever string the backend reports for this ONU (a model/ODM identifier, not a
constant, and not something the app invented).

### 4.3 Change the key — P3

`ChangeWiFiPasswordViewModel` (`defpackage/j80.java`) validation, client-side only:
`new == confirm`, `length >= 8` (strings: *"New password must be at least 8 symbols!"*).

```kotlin
// UserApiDataSource$setWlanPassword$2$1
api.setWlanInfoKey(
    subscriberId, idx = info.idx, ssid = info.ssid, key = password,   // <-- plaintext Wi-Fi key
    broadCastSsid = true, radioType, enable, mode, security, type, vendor
)
```

Body = same as 4.2 but `"key": "<new passphrase>"` and **`skipKey` absent**. The key travels in cleartext
JSON over TLS (no app-level field encryption anywhere in these models).

### 4.4 Confirm — P4

Response `{message, taskId}`; the app then polls `provision/netSense/checkTaskStatus {"taskId": n}` until
`TaskStatusResult.status` ∈ `completed | failed | canceled`, with `retryTimeout` from
Remote Config `wlan_status_retry` (`{retry_timeout, total_timeout}`). While polling it shows
*"The Change WiFi name process has been triggered. Please wait for %d seconds…"*.

Sequence in one line: `getWlanInfo` → `setWlanInfo` → `checkTaskStatus` (loop) — i.e. a queued
provisioning job on Excitel's side, not a synchronous CPE write.

---

## 5. Response schemas (Moshi `@Json(name=…)`)

```jsonc
// ApiResponse envelope
{ "status": {"message": "...", "code": 0}, "success": true, "result": <T|null>, "messages": [...] }

// WlanInfoData (P1 result)
{ "data": [ WlanInfo, ... ],            // one per radio/SSID instance
  "connectedDevices": [ ConnectedDevice, ... ],
  "metrics": [ TrafficFrame, ... ],
  "vendor": "<?>" }

// WlanInfo  — note: NO passphrase field
{ "idx": 0, "ssid": "<SSID-2.4>", "broadCastSsid": true,
  "security": "<?>", "type": "<?>", "channel": "<?>",
  "radioType": "<?>", "enable": true, "bssid": "<mac>",
  "wepIdx": 0, "mode": "<?>", "vendor": "<?>" }

// SetWlanInfoResponse (P2/P3 result)
{ "message": "...", "taskId": 987654 }

// TaskStatusResult (P4 result)
{ "id": 987654, "status": "pending|in-progress|completed|failed|canceled",
  "title": "...", "error": "...", "retryTimeout": 5, "serialNumber": "…" }

// ConnectedDevice ← much richer than the ONT web UI
{ "macAddress","hostname","ip","macVendor","type","baseType","active","lastSeen",
  "ssid","band","radioType","deviceName","rssi" }
// plus ConnectedDeviceMetrics { rssi:[DeviceRSSIMetricFrame], traffic:[DeviceTrafficMetricFrame] }
```

---

## 6. What the parameter set implies about the ACS write

`idx` + `radioType` + `security` + `type` + `mode` + `enable` + `vendor` + (`ssid`|`key`) is a
per-WLANConfiguration record, i.e. the backend is writing the ONU's WLAN object:

```
InternetGatewayDevice.LANDevice.1.WLANConfiguration.{idx+1}.SSID
InternetGatewayDevice.LANDevice.1.WLANConfiguration.{idx+1}.KeyPassphrase
InternetGatewayDevice.LANDevice.1.WLANConfiguration.{idx+1}.PreSharedKey.1.KeyPassphrase
InternetGatewayDevice.LANDevice.1.WLANConfiguration.{idx+1}.BeaconType
InternetGatewayDevice.LANDevice.1.WLANConfiguration.{idx+1}.SSIDAdvertisementEnabled  (broadCastSsid)
InternetGatewayDevice.LANDevice.1.WLANConfiguration.{idx+1}.Enable                    (enable)
```

We cannot see the ACS-side names (they are server-side), but the app proves **which ONU attributes are under
remote control and that the write path is a parameter-level patch, not a template reload**: `skipKey=true`
exists precisely so the backend can update `SSID` without touching the key — that only makes sense against a
per-parameter setter.

Same verdict as `NOTES.md`, now with the mechanism: app → `selfcare/provision/setWlanInfo` → ACS →
`SetParameterValues`.

---

## 7. Is the web-UI admin password reachable? No.

Searched: every Retrofit interface, every request/response model (`lh2` JSON names), all string resources,
all UI strings.

* No `adminPassword`, `routerPassword`, `webPassword`, `devicePassword`, `userInterface` field anywhere.
* No reference to `192.168.1.1`, `boaform`, `/admin/login.asp`, TR-069, CWMP, or port 7547 in the app.
* `provision/getWlanInfo` returns SSID/security/channel — **not** the Wi-Fi key, and certainly no admin credential.
* `subscriber/changePassword` is the **portal** password (`subscriberId, oldPassword, newPassword, confirmNewPassword`)
  — a different credential from the ONT `root` account.
* The only per-subscriber capability flag is `accesses.canDoOnuRemoteManagement` (Boolean) — the server-side
  gate that turns the whole "Manage Wi-Fi" section on or off for an account.

So: the app can set the WLAN SSID/key of this ONU remotely, and nothing else on the CPE. **There is no
endpoint whose UI field maps to the web admin password** — as required by the handoff's scope note, this is
"write it up and ask Excitel", not "try it".

---

## 8. Security observations (informational — nothing here was exercised)

1. **Cookie-only auth, no CSRF token.** `selfcare` cookie + JSON body, no anti-CSRF header, and the cookie is
   stored (unencrypted) in SharedPreferences (`excitel-cookie`); the app declares `ALLOW_BACKUP`. A stolen
   cookie = full account API access.
2. **`subscriberId` is a small sequential integer sent in the request body**, and every provision call trusts
   the client's `subscriberId`/`idx`. If the backend does not bind it to the cookie, `provision/getWlanInfo`
   and `setWlanInfo` are textbook IDOR. **Report, do not test.**
3. **No certificate pinning** and no `networkSecurityConfig` → the app's API surface is inspectable by anyone.
4. **Wi-Fi passphrase sent as plaintext JSON field** (`key`) — accepted practice over TLS, but it is also
   echoed into the backend/ACS logs by construction.
5. **Partner/Workforce API keys travel as `?keyname=&key=` query strings** to `wfboapi.azurewebsites.net`
   (Azure Functions). Query strings land in proxies/logs. Values are runtime (Remote Config) supplied.
6. Forced SSID prefix `Excitel_` is a **client-side** rule — meaning "Excitel_…" on a neighbour's AP is not
   evidence that Excitel set it. It also explains why the ONU's live SSIDs are `<SSID-2.4>` / `<SSID-5>`
   while the label says `FTTH` / `FTTH-5G`: those names can only have been written through this API.

---

## 9. Live capture — deliberately skipped

Static analysis answers Q1–Q6 without touching the account, so **no app capture was performed** (decision
recorded 2026-11-07). The host question was closed separately by reading Remote Config (§2.1), so the only
remaining unknown is:

1. the concrete literals the ONU supplies for `radioType`, `mode`, `security`, `type`, `vendor`, and whether
   `idx` is 0/1 or 1/2 — visible from a single authenticated `getWlanInfo`.

Neither changes any conclusion below. If a capture is ever wanted, the constraints found in the APK are:

* No pinning ⇒ a proxy *would* work, **but** `usesCleartextTraffic=false` and no `<network-security-config>`
  means the app trusts **system CAs only** (Android 7+). A mitmproxy CA in the user store is ignored.
* Therefore, in order of effort:
  1. **`apk-mitm` on a copy, uninstall + `adb install`** the patched APK (same package name), install the
     mitm CA, log in again (phone + OTP — the account login, not the ONT). Expect to lose nothing but the
     local session.
  2. **Rooted device / Frida** pinning-bypass (not needed — nothing to bypass) or a system-store CA.
  3. **`adb backup`-style extraction of `excitel-cookie`** on a lab device, then replay
     `provision/getWlanInfo` from a laptop — read-only, and enough to record the exact response schema.
* Rate-limit: one SSID change, one password change, then stop. The poll loop multiplies requests.
* Everything needed for the replay, redacted, is already in §3–§5.

---

## 10. Sentence for the support ticket (replaces the vague ask)

> My ONT (UBIQCOM UB5021 GVW-D, serial `<from label>`) is managed by your ACS — I can change its Wi-Fi SSID
> and passphrase from the Excitel app, which calls `POST /provision/setWlanInfo` and then polls
> `/provision/netSense/checkTaskStatus` until the job completes. Please apply one more parameter change to the
> same device through the same provisioning path, and tell me the value: the **web-UI administrator password**
> (the firmware's only admin account is `root`; the `user` account printed on the label answers "Forbid user
> login"). This is the same class of operation as the SSID write you already perform on this CPE — a
> provisioning action, not a credential disclosure.

Two supporting points worth including, both established here or in `NOTES.md`:

* `accesses.canDoOnuRemoteManagement` is per-account — support can check it, and if it were off the Wi-Fi
  screen would not exist in my app at all.
* Ports 22/23 are ACL-filtered on IPv4 but the same daemons are reachable over IPv6 link-local
  (`<ONU-LINK-LOCAL>`), so "management is disabled on the LAN" is not an accurate statement.

---

## 11. Reproducing this analysis

```bash
cd ~/excitel
adb shell pm path com.scaleforce.mobile.myexcitel
adb pull "<that path>" app/app/apk/myexcitel-3.41.0-base.apk
brew install jadx
SCRATCH=${TMPDIR:-/tmp}/excitel
jadx -d "$SCRATCH/jadx-out" --no-res --no-debug-info app/app/apk/myexcitel-3.41.0-base.apk
jadx -d "$SCRATCH/jadx-res" --no-src app/app/apk/myexcitel-3.41.0-base.apk     # manifest + resources

grep -rn 'provision/' "$SCRATCH/jadx-out/sources"            # → defpackage/mi4.java
sed -n '1,200p' "$SCRATCH/jadx-out/sources/defpackage/mi4.java"
cat "$SCRATCH/jadx-out/sources/defpackage/jr0.java"          # Retrofit/base-url wiring, cases 10/11
cat "$SCRATCH/jadx-out/sources/defpackage/a62.java"          # host rewrite from RC
cat "$SCRATCH/jadx-out/sources/defpackage/hk4.java"          # cookie jar (selfcare)
cat "$SCRATCH/jadx-out/sources/com/scaleforce/mobile/myexcitel/data/datasource/"'UserApiDataSource$setWlan*'
```

Key reference files (decompiled names → original Kotlin):

| decompiled | original |
|---|---|
| `defpackage/mi4.java` | Retrofit `SelfCareApi` interface |
| `defpackage/jr0.java` | `SelfCareModule` (Retrofit/OkHttp providers) |
| `defpackage/a62.java` | env/base-url interceptor |
| `defpackage/hk4.java` | cookie jar |
| `data/datasource/d.java`, `UserApiDataSource$*` | `UserApiDataSource.kt` |
| `usecase/SetSSIDRequest.java`, `SetWlanPasswordRequest.java` | `SetWlanSSID.kt`, `SetWlanPassword.kt` |
| `defpackage/c80.java`, `defpackage/j80.java` | `ChangeSSIDViewModel`, `ChangeWiFiPasswordViewModel` |
| `data/model/api/WlanInfo*.java`, `SetWlanInfoResponse.java`, `TaskStatusResult.java` | response models |
| `data/datasource/a.java`, `data/model/SelfCareConfig.java` | `FirebaseDataSourceImpl.kt` (RC `procare_config`) |

### Decompilation gaps (checked; they do not affect the conclusions)

`jadx` ends with `finished with errors, count: 89` — 89 **methods**, not classes (128 files carry a warning;
only 7 are under `com.scaleforce`):

| file | method left as bytecode | what it is |
|---|---|---|
| `data/repository/e.java` | `S0`, `U0`, `w0` (String-arg) | ticket / pre-insert / username-based calls |
| `data/repository/d.java` | `p0(String, …)` | string-arg repository call |
| `data/repository/c.java` | `m0(String, …)` | string-arg repository call |
| `usecase/z0.java` | `a(VerificationRequest, …)` | OTP verification |
| `usecase/r0.java` | `a(VerificationType, …)` | OTP resend |
| `base/a.java` | `c(RetryStrategy, int, …)` | generic retry wrapper (the `SELFCARE, 3` retry count is visible at the call sites) |
| `controller/c.java` | `h(Object)` | ad/analytics controller |

Every **WLAN/provision** path method decompiled cleanly: `mi4`, `UserApiDataSource$setWlan{SSID,Password}$2`,
`UserApiDataSource$getWlanInfo$2`, `SetSSIDRequest`, `SetWlanPasswordRequest`, `WlanInfo`/`WlanInfoData`/
`SetWlanInfoResponse`/`TaskStatusResult`, `UserRepository.getWlanInfo`/`setWlan*`/`get*TaskStatus`, `hk4`,
`a62`, `jr0` cases 10/11.

### Tooling: `set-ssid.py`

Companion script written against this document. Single file, **uv inline dependencies** (PEP 723) — `httpx`
for the transport, `rich` for output; no virtualenv to manage, uv resolves and caches on first run:

```python
#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27,<1", "rich>=13.7,<15"]
# ///
```

```bash
./set-ssid.py otp --phone 9xxxxxxxxx       # phone + OTP (what the app does); or login --user <name>
./set-ssid.py show                         # getWlanInfo → idx, radio, ssid, security, mode, type, vendor
./set-ssid.py show --json                  # raw response
./set-ssid.py set-ssid --radio 2.4 --ssid MyNet             # dry run: prints the exact body
./set-ssid.py set-ssid --radio 2.4 --ssid MyNet --apply     # sends, then polls checkTaskStatus
./set-ssid.py set-key  --radio 5 --password '<new>' --apply
./set-ssid.py task 4242                    # poll an existing job
```

`uv run set-ssid.py …` is equivalent if the shebang is not honoured. The default base URL is the real one from
§2.1 (`https://selfcare.north.excitel.in/api/index_dev.php/`); override with `--base-url` or
`$EXCITEL_API_BASE`. Pointing it at the bare host gets an HTML portal page, and the script says so explicitly
instead of dumping HTML at you. Poll defaults (30 s interval / 360 s total) come from RC `wlan_status_retry`.

Guard rails built in: one field per invocation; `security`/`mode`/`type`/`radioType`/`enable`/`broadCastSsid`/
`vendor` are always re-read from `getWlanInfo` and echoed verbatim (never invented — those fields are *applied*,
so a wrong value can drop the Wi-Fi); SSID limited to 32 bytes and keys to 8..63 chars (802.11 limits, not
guesses); writes are never auto-retried because the backend queues a device job; the Wi-Fi key is redacted in
all output; a non-interactive shell refuses to write without `--yes`; a rejected write reports the server
message and exits 4 without touching the CPE.

The session file (`.excitel-session.json`, mode 600) holds the `selfcare` cookie — **never commit it**.

## 12. Live capture — 2026-11-07 (patched app + mitmproxy)

The app was re-packaged (frida-gadget + `debuggable` + a `network-security-config` trusting user CAs,
signed with a lab key) and its own traffic recorded through mitmproxy over `adb reverse`. Nothing was
written to the ONU; only reads were triggered.

### 12.1 What the app sends (verbatim)

```
GET  /api/index_dev.php/subscriber/showDashboard
GET  /api/index_dev.php/subscriber/showSubscriberOttServices?subscriberId=…&limit=100
GET  /api/index_dev.php/subscriber/getFtthStatus
GET  /api/index_dev.php/subscriber/getExtendedFiberData?subscriberId=…&partnerId=…&isMobile=1
GET  /api/index_dev.php/ticket/list?sortBy=…&currentPage=1&itemsPerPage=50&where=…&isDefaultTab=true
POST /api/index_dev.php/payment/isLastTransactionPending      subscriberId=…&isDeposit=1
POST /api/index_dev.php/provision/getWlanInfo                 subscriberId=<subscriberId>
```

Headers on every call:

```
content-type: application/x-www-form-urlencoded     # POSTs only
cookie: selfcare=<32 hex chars>                     # the whole auth model
user-agent: okhttp/5.4.0
traceparent: 00-<trace>-<span>-03                   # OpenTelemetry okhttp instrumentation
```

> The earlier "Invalid username or password" was an artefact of posting JSON: with form encoding the
> same credentials are accepted. Nothing about the password was ever wrong.

### 12.2 `provision/getWlanInfo` — real response

```jsonc
{"status":{"message":"OK","code":200},"success":true,
 "result":{"vendor":"netsense",
  "data":[
   {"idx":5,"ssid":"<SSID-2.4>","broadCastSsid":true,"security":"","channel":"6",
    "type":"b,g,n","radioType":"2.4","enable":true,"bssid":"","wepIdx":0,"mode":""},
   {"idx":1,"ssid":"<SSID-5>","broadCastSsid":true,"security":"","channel":"52",
    "type":"ac","radioType":"5","enable":true,"bssid":"","wepIdx":0,"mode":""}],
  "connectedDevices":[{"macAddress":"…","hostname":"…","ip":"192.168.1.46","type":"ethernet",
                       "baseType":2,"radioType":0,"active":true,"lastSeen":"…"}],
  "metrics":[{"time":"2026-11-07T04:50:00Z","packetsSent":…,"bitsReceived":…}]}}
```

Facts that differ from the static-only reading:

| field | reality |
|---|---|
| `idx` | **5** = 2.4 GHz, **1** = 5 GHz (not 0/1) |
| `radioType` | `"2.4"` / `"5"` — bare numbers, not `"2.4GHz"` |
| `type` | radio mode string: `"b,g,n"` / `"ac"` |
| `security`, `mode`, `bssid` | **empty strings** — the backend does not populate them; the app echoes them back empty |
| `vendor` | `"netsense"` — netSense is the ONU management platform behind the `provision/*` namespace |
| `connection` | HTTP/2, `selfcare` cookie = 32 hex chars |

`installedOnuData` on this line: `{type:"ONU+WiFi-ac", serialNumber:"<device serial>", macAddress:"<device MAC>"}`.

### 12.3 Consequences for the tooling

* `set-ssid.py` now posts **form-encoded** (JSON was the bug) with UA `okhttp/5.4.0`; `show` returns the
  same rows the app shows.
* A write therefore needs `skipKey=true` + `idx` + the echoed `radioType/type/enable/mode/security/vendor`
  exactly as returned — all of which are now known, so no value has to be invented.
* Android interconnect note: the phone sat behind a second NAT (192.168.0.0/24) and could not reach the
  Mac's 8080, so the capture used `adb reverse tcp:8080 tcp:8080` + `http_proxy 127.0.0.1:8080` instead of
  a LAN proxy.

### 12.4 Write accepted by the API — but NOT applied to the CPE (2026-11-07, later)

Two writes were executed with the captured cookie, both answered `success:true`:

| # | request | job | API result | ONU (Wi-Fi scan) |
|---|---|---|---|---|
| 1 | `idx=5 ssid=MyNet skipKey=true` | `5896504` | pending → **completed** `configure_wlan` | BSSID `<ONU-MAC>` still `<SSID-2.4>` |
| 2 | `idx=1 ssid=MyNet5 skipKey=true` | `5896606` | pending → **completed** `configure_wlan` | BSSID `<ONU-MAC>` still `<SSID-5>` |

Verified with `adb shell cmd wifi list-scan-results` (ONU BSSIDs share `<ONU-MAC>x:xx`).

Conclusions:

* **There is no server-side `Excitel_` prefix policy.** The backend accepts and stores an arbitrary
  SSID; the app's prefix is purely cosmetic, as the static analysis said.
* **`provision/getWlanInfo` returns the netsense database, not a live CPE read.** After the write it
  reported `ssid:"<id>-main"` while the radio was still broadcasting `<SSID-2.4>` — so the platform
  cannot notice that the change never landed.
* **The job status is not a delivery receipt.** `completed`/`configure_wlan` means "we tried", and a
  TR-069 `SetParameterValues` fault from the CPE is not surfaced. Two consecutive writes to two
  different radios both produced `completed` with no effect on the device.
* Therefore the *provisioning chain* (netsense → ACS → CPE) is not currently applying WLAN changes to
  this ONU. That is an ISP-side fault, and it is exactly the kind of thing a ticket can be built on:
  task ids, timestamps, and a Wi-Fi scan showing the old SSID.

Third test — does *any* WLAN parameter apply? `broadCastSsid=false` on idx 1 (task `5896641`) also
returned `success:true`, and the ONU kept advertising that BSSID with the `[ESS]` flag. So no WLAN
parameter is applied at all, while telemetry is live: `connectedDevices.lastSeen` and the `metrics`
series advance to the current minute. The device is online; only the write path is inert.

The response body is worth quoting in a ticket — it claims delivery in words:

```json
{"success":true,"result":{"message":"Configuration changes are applied","taskId":5896641},
 "messages":[{"type":"success","text":"You have successfully sent subscribers credentials!"}]}
```

The same change made *in the app* (user-confirmed, 2026-11-07) has the same silent outcome, so this is
not a client artefact: `setWlanInfo` and the app are byte-identical in effect — accepted, recorded,
never applied.

What would distinguish "ACS cannot reach the CPE" from "CPE rejects the parameters" is not observable
from the client side. A power-cycle of the ONU (forcing a fresh CWMP Inform / parameter re-sync) is the
cheapest way to tell them apart.

**Credentials:** none in this file.**Credentials:** none in this file. The only identifiers are public app constants (paths, field names, the
`Excitel_` prefix).
