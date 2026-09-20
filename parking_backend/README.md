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

The login persists: `-no-snapshot` only disables Quick Boot, so `userdata-qemu.img` keeps the account and the
Chrome profile across restarts, and both adapters set `appium:noReset` so a session never clears them. Passing
`-wipe-data`, recreating the AVD, or deleting that image throws the account away.

A Google session only affects Google's own reCAPTCHA. The LAZ entry page is gated by Cloudflare, which issues
`cf_clearance` from IP reputation and browser fingerprint and never reads the Google session, so it is not a
fix for the verification page sticking.

## Gmail

Create an OAuth desktop client in `fieldscout-497018`, enable the Gmail API, and add `pocketsfast@gmail.com` as a test user. Run `oauth_authorize.py` locally and place the resulting refresh token in the protected environment file. The service requests only `gmail.send`.

OAuth clients left in Testing can require reauthorization after seven days. An email failure is reported separately and never changes or repeats the form submission.

## Recovery

`accepted` and `preparing` jobs can retry before submission. The worker commits `submitting` before clicking Submit. A restart from `submitting` becomes `unknown`, generates a notification, and is never submitted again automatically.
