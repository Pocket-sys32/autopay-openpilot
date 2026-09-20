# Comma parking demo backend

This service accepts one idempotent demo attempt per device parking episode, submits the allowlisted Google Form through Appium on Android, retains the result in SQLite, and sends an independent Gmail notification. It never handles a real payment.

QR decoding runs on this VM. The comma uploads authenticated TLS JPEG snapshots from the road cameras at a bounded rate; the API limits each image to 768 KiB, decodes it in memory, returns candidate text, and does not retain the image. The comma still performs exact allowlisting, repeated-observation consensus, parked-state validation, and the countdown.

## VM gate

Inspect the existing instance before installing anything:

```bash
gcloud compute ssh instance1 --project fieldscout-497018 --zone us-west1-b \
  --command 'bash -s' < parking_backend/deploy/inspect_vm.sh
```

The install may proceed only on Ubuntu x86-64 with at least four vCPUs, 16 GB memory, 50 GB available storage, virtualization flags, and a usable `/dev/kvm`. If the gate fails, create `parking-demo-vm` as an `n2-standard-4` Ubuntu 24.04 instance with nested virtualization. Do not modify `instance1` to force compatibility.

## Deployment

1. Reserve the instance's external IP and restrict the firewall to TCP 443. Do not expose ports 4723, 5554, 5555, or 8080.
2. Copy this repository to `/opt/parking-demo` on the VM and run `deploy/install_vm.sh` as root.
3. Generate a project-specific CA with `deploy/create_tls.sh EXTERNAL_IP`. Install only the server certificate and key on the VM. Copy the CA certificate to the comma at `/persist/parking/parking-ca.crt`.
4. Copy `deploy/nginx-parking.conf` to `/etc/nginx/sites-enabled/parking`, remove the default site, test with `nginx -t`, and reload Nginx.
5. Create a Secret Manager secret named `parking-demo-backend` whose value is a JSON object containing `PARKING_BEARER_TOKEN`, `PARKING_GMAIL_CLIENT_ID`, `PARKING_GMAIL_CLIENT_SECRET`, `PARKING_GMAIL_REFRESH_TOKEN`, and the four `PARKING_TEST_*` dummy values. Give only the VM service account `roles/secretmanager.secretAccessor` on this secret. The checked-in environment template contains nonsecret settings only.
6. Boot Android, confirm Chrome exists with `adb shell pm list packages | grep com.android.chrome`, and open the form once manually. If Chrome is absent, stop: do not download an APK from an unofficial source.
7. Start `parking-api` and `parking-worker`. Exercise `/healthz`, then a controlled attempt, before configuring the comma.

The comma parameters are:

```text
ParkingBackendBaseUrl=https://EXTERNAL_IP
ParkingBackendCaPath=/persist/parking/parking-ca.crt
ParkingLicensePlate=the test plate
ParkingPlateCountry=US
ParkingPlateRegion=CA
ParkingDefaultDuration=3600
ParkingAutoPayEnabled=1
```

Write the bearer token to `/persist/parking/device-token` on comma four, owned by the openpilot user with mode `0600`. It is deliberately stored outside Params. Set `PARKING_BACKEND_TOKEN_PATH` only for bench tests that use another protected path.

Keep `ParkingAutoPayEnabled` off until the VM's form and Gmail smoke tests succeed.

## Signing the emulator in to Google

The VM has no display, and the page is loaded by Chrome *inside* the emulator, so the account has to be added
on the emulator itself. Drive it by hand once; never let Appium perform the sign-in, because Google refuses a
WebDriver-controlled session with "this browser or app may not be secure".

Both scripts run on your workstation, not on the VM:

```bash
./deploy/workstation_setup.sh       # once: Google Cloud CLI and scrcpy, installed under $HOME, no root
gcloud auth login                   # only if gcloud is not signed in yet
./deploy/remote_emulator.sh --status   # services, emulator, and how many Google accounts are on the device
./deploy/remote_emulator.sh            # mirror the emulator
```

`remote_emulator.sh` stops `parking-worker` so no attempt runs mid-session, tunnels the emulator's loopback adb
port over SSH, mirrors the screen with scrcpy, and on exit disconnects, reports the account count and restarts
the worker if it had been running. In the mirrored device: Settings -> Passwords & accounts -> Add account ->
Google, then open Chrome and pick the same account. The AVD uses the `google_apis_playstore` image, so this is
the real Play Services sign-in and 2FA prompts work normally.

The scripts default to `parking-demo-vm` in `fieldscout-497018`/`us-west1-b`; override with `PARKING_VM_INSTANCE`,
`PARKING_VM_PROJECT` and `PARKING_VM_ZONE`. The emulator is on `parking-demo-vm`, not on `instance1`, which is an
`e2-medium` and never passed the VM gate above. scrcpy must be 2.x or newer: the emulator runs Android 15, which
the version most distributions package does not support.

The login survives an emulator restart: `-no-snapshot` only disables Quick Boot, so `userdata-qemu.img` keeps
the account and the Chrome profile. Passing `-wipe-data`, recreating the AVD, or deleting that image throws the
account away.

What it does **not** survive by default is an automated run. `appium:noReset` only stops Appium resetting the
app; chromedriver separately relaunches Chrome *with a cleared data directory*, which signs the browser out
before the first page loads. `LazAdapter` therefore sets `androidUseRunningApp`, attaching to the Chrome that
is already running instead of relaunching it. `PARKING_LAZ_ATTACH_CHROME=0` restores the old behaviour.

Two consequences follow. Chrome has to be running on the device before an attempt, or the session cannot
attach; rather than fall back to a launch, which would succeed while quietly signing the browser out, the
adapter reports `action_required`. And attaching is incompatible with `PARKING_LAZ_INJECT_CF_COOKIES=1`, since
matching the solver's user agent requires the relaunch that wipes the session — when that is on, it wins and
the Google session is lost. The command-line arguments above are not applied while attached either, but
`navigator.webdriver` stays false regardless, precisely because chromedriver did not launch the browser.

**The deployed VM currently sets `PARKING_LAZ_INJECT_CF_COOKIES=1`**, so attaching never engages there and
every attempt runs signed out — the warm-up log says `google session cookie present: no` even right after a
successful manual sign-in. The two are worth measuring against each other: attached, and with no solver
involved, the browser cleared Cloudflare by itself in one of the two probes run that way and reached the
checkout form, while keeping the Google session that reCAPTCHA reads. Two probes is not enough to choose on,
so measure it before switching; `PARKING_LAZ_INJECT_CF_COOKIES=0` is what switches to it.

The two gates are separate and want different things. Cloudflare, on the entry page, issues `cf_clearance` from
IP reputation and browser fingerprint and never reads the Google session. reCAPTCHA, at the checkout, is the one
a signed-in Google account helps: it reads google.com's cookies from its own iframe, and scores a browser with a
real session far above an anonymous one.

## reCAPTCHA at the checkout

Three things work on the checkout's reCAPTCHA score, in descending order of effect:

1. The signed-in Google account above. This is the large one.
2. Warming the profile. Before the checkout, `LazAdapter` loads `DEFAULT_WARMUP_URLS` — google.com, which is
   where the session cookies live and is the only one that really counts, then wikipedia.org and LAZ's own
   homepage, which is where a person would normally have started. Set `PARKING_LAZ_WARMUP_URLS` to a
   comma-separated list to change it, or to the empty string to switch warming off; `PARKING_LAZ_WARMUP_BUDGET`
   (default 30 s) caps the whole warm-up so a slow site cannot eat the worker's deadline. A warm-up failure is
   logged and ignored: it is not the purchase. The worker's per-attempt deadline is 180 s to allow for this.
3. Chrome now launches with `--disable-blink-features=AutomationControlled`. Chromedriver otherwise leaves
   `navigator.webdriver` set, which reCAPTCHA reads.

Each run writes `<stamp>-0-warmup.txt` into `PARKING_DIAG_DIR`, recording which sites loaded and whether a
Google session cookie was present. That file is how you confirm the sign-in actually reached Chrome; it records
cookie names only, never values.

### Adding the account to Android is not signing Chrome in

These are two separate things, and only the second one reCAPTCHA can see. `dumpsys account` can report an
account while Chrome's cookie jar for google.com still holds only `AEC`, `NID`, `SEARCH_SAMESITE` and
`__Secure-STRP` — the set a signed-out browser gets. A signed-in one also carries `SID`, `HSID`, `SSID`,
`APISID`, `SAPISID` and `__Secure-1PSID`, which is what `GOOGLE_SESSION_COOKIES` looks for.

To move the device account into the web session, in the mirrored emulator either turn on Chrome sign-in
(Chrome -> menu -> Settings -> the account card at the top -> pick the account), which makes Chrome propagate
it to Google's web properties, or open `accounts.google.com` in Chrome and sign in there, which sets the web
cookies directly and does not depend on sync. The session is shared across google.com and youtube.com, so
either entry point is enough. If the account card is missing, check that "Allow Chrome sign-in" is on under
Settings -> Google services.

### Checking it without buying anything

`deploy/probe_browser.py` opens a session exactly as `LazAdapter` would, warms it, and reports the cookie
state, `navigator.webdriver`, whether Cloudflare passed and which reCAPTCHA frames are present. It fills no
field and never clicks PAY. Copy the package somewhere readable and run it on the VM as `parking-demo`:

```bash
PYTHONPATH=/tmp/proberoot PARKING_DIAG_DIR=/tmp/probe-diag \
  /opt/parking-demo/.venv/bin/python /tmp/proberoot/parking_backend/deploy/probe_browser.py
```

`PARKING_PROBE_CHECKOUT=1` also walks GO -> NEXT to the checkout form and reports the frames there. Note that
an invisible reCAPTCHA is only evaluated when PAY is clicked, so a clean report is encouraging but not proof.

A visible challenge is now recognised rather than left to time out. Before PAY it raises `CaptchaChallenged`,
which the worker records as `action_required` / `CAPTCHA_CHALLENGED` with nothing purchased, and snapshots
`0-recaptcha-before-pay`. After PAY the result is genuinely ambiguous, so it stays `unknown` — it is only
snapshotted as `5-recaptcha-after-pay` and noted in the message. Clear a challenge by hand with
`remote_emulator.sh`; the cleared state lives in the same persistent profile.

## The generic parking agent

`generic_agent` handles a QR nobody has written an adapter for. It opens the URL in the emulator's Chrome,
observes each screen, lets a multimodal model choose one bounded UI action at a time, and navigates to the
provider's checkout. There it stops. The comma shows the driver the merchant, the location, the vehicle, the
duration and the total, and nothing is paid until they slide to confirm.

The model chooses which element to act on next. It does not hold the card, the profile, the spend cap or the
payment decision. In particular:

- The total shown, hashed and charged is parsed off the page. Whatever figure the model reports is discarded,
  and the page is re-read for exact equality immediately before the pay click; any change aborts the purchase.
- The model addresses elements only by ids the current screen handed out. Card fields are never given one, so
  the only way a card value reaches a page is `FILL_SECRET`, which names a slot and is refused until the
  driver has confirmed.
- Navigation is confined to the scanned URL's registrable domain, hosts reached by redirect from it, and
  known payment hosts.
- A captcha, an account, a one-time code or an app-only flow ends the attempt as `action_required`. The agent
  never attempts to work around a security check.
- Both spend caps bind: the driver's `ParkingMaxTotalMinor` and the VM's `PARKING_AGENT_MAX_TOTAL_MINOR`.
  The lower one wins.

Enable it with `PARKING_AGENT_ENABLED=1`. Settings, all optional except the card:

```text
PARKING_AGENT_ENABLED=1
PARKING_AGENT_MAX_TOTAL_MINOR=3000     # hard ceiling in minor units
PARKING_AGENT_DRY_RUN=0                # 1 reaches the checkout and stops before paying
PARKING_AGENT_MODEL=gemini-2.5-flash
PARKING_AGENT_LOCATION=us-west1
PARKING_AGENT_CARD_NUMBER=...          # secrets; keep these in Secret Manager
PARKING_AGENT_CARD_CVV=...
PARKING_AGENT_CARD_EXPIRY_MONTH=...
PARKING_AGENT_CARD_EXPIRY_YEAR=...
PARKING_AGENT_CARD_ZIP=...
PARKING_AGENT_EMAIL=...                # falls back to the PARKING_LAZ_* equivalents
PARKING_AGENT_MOBILE=...
PARKING_AGENT_ZIP=...
PARKING_AGENT_STREET=...
PARKING_AGENT_ATTACH_CHROME=1          # see the attach-mode note above
PARKING_AGENT_WARMUP_URLS=             # empty string turns warming off
```

Run it with `PARKING_AGENT_DRY_RUN=1` against a real provider first. That exercises the whole path, including
the confirmation prompt on the comma, and stops before spending anything.

The model runs on Vertex AI as the VM's own service account, so there is no API key. `aiplatform.googleapis.com`
is already enabled on the project, but the service account needs the role granted once:

```bash
gcloud projects add-iam-policy-binding fieldscout-497018 \
  --member serviceAccount:parking-demo-vm@fieldscout-497018.iam.gserviceaccount.com \
  --role roles/aiplatform.user
```

Add `ParkingMaxTotalMinor` to the comma parameters to set the driver's own ceiling; it defaults to 3000.

## Gmail

Create an OAuth desktop client in `fieldscout-497018`, enable the Gmail API, and add `pocketsfast@gmail.com` as a test user. Run `oauth_authorize.py` locally and place the resulting refresh token in the protected environment file. The service requests only `gmail.send`.

OAuth clients left in Testing can require reauthorization after seven days. An email failure is reported separately and never changes or repeats the form submission.

## Recovery

`accepted` and `preparing` jobs can retry before submission. The worker commits `submitting` before clicking Submit. A restart from `submitting` becomes `unknown`, generates a notification, and is never submitted again automatically.
