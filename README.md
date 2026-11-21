# Morphe Auto Build

[![Build](https://github.com/Fahry-a/Farhan-Morphe-Auto-Build/actions/workflows/build.yml/badge.svg)](https://github.com/Fahry-a/Farhan-Morphe-Auto-Build/actions/workflows/build.yml)
[![Tests](https://github.com/Fahry-a/Farhan-Morphe-Auto-Build/actions/workflows/tests.yml/badge.svg)](https://github.com/Fahry-a/Farhan-Morphe-Auto-Build/actions/workflows/tests.yml)

Configuration-driven GitHub Actions automation for building Morphe-patched Android APKs.

## Supported applications

| Application | Package | Patch repository | Sources | Architectures | Flavors |
| --- | --- | --- | --- | --- | --- |
| Google Photos | com.google.android.apps.photos | Akash-Sriram/morphe-google-photos | APKMirror → APKPure → APKCombo → Aptoide | Universal | mod, original |
| Brave Browser | com.brave.browser | kveld9/kveld-morphe-patches | GitHub | ARM64, ARM32 | Default |
| AudioRelay | com.azefsw.audioconnect | kiraio-moe/Lain-Patches | APKPure → APKCombo → Aptoide | Universal | Default |
| Native Camera | com.rawcam.app | WaggBR/Wagg13Patch_Morphe | APKPure | ARM64, ARM32 (no universal package published) | Default |
| Pinterest | com.pinterest | browzomje/browzomje-patches | APKMirror → APKCombo → APKPure → Aptoide | Universal | Default |
| Advanced Download Manager | com.dv.adm | arandomhooman/hoomans-morphe-patches | APKMirror → APKPure → APKCombo → Aptoide | Universal | Default |
| X / Twitter | com.twitter.android | crimera/piko | APKMirror → APKPure → APKCombo → Aptoide | Universal | Default |
| Instagram | com.instagram.android | SysAdminDoc/HushGram | APKPure → APKMirror → APKCombo → Aptoide | ARM64, ARM32 | Default |

Brave assets:

~~~text
arm64 → BraveMonoarm64.apk
arm32 → BraveMonoarm.apk
~~~

ARM32 / armeabi-v7a is intentionally a first-class supported target.

## Build pipeline

~~~text
configuration
  → patch-supported version
  → exact package download
  → package + version validation
  → Morphe patch
  → manifest-driven signing
  → monthly release
~~~

The workflow does not accept an arbitrary application-version override. The application version is selected from versions supported by the selected Morphe patch.

Mirror sources can define a package type per mirror. A mirror-level `file_type` overrides the source-level value; if neither is set, `apk` is used. This allows configurations where one mirror provides XAPK while another provides APK for the same application.

`arch: universal` is strict: the selected package must contain both `arm64-v8a` and `armeabi-v7a` (or be architecture-independent). An arm64-only or arm32-only asset is rejected instead of being mislabeled as universal. Mirror-backed app configs do not need a manual `source.archs` list; the universal contract is implicit.

## Manual workflow

The build workflow supports:

| Input | Description |
| --- | --- |
| app | One app or all |
| arch | One configured architecture or all |
| direct_apk_url | Manual direct URL for one app/architecture |
| allow_experimental | Allow experimental Morphe targets |
| rebuild | Force a rebuild even when release assets already exist |

## Releases

Builds are published to cumulative monthly GitHub Releases:

~~~text
YYYY-MM
~~~

Updating one app/architecture replaces only its stale assets and release-note section. Other applications and architectures remain intact.

## Development

Python dependencies use uv:

~~~bash
uv pip install --system -r requirements.txt
python -m playwright install chromium --with-deps
~~~

Run tests:

~~~bash
python -m unittest discover -s tests -v
~~~

### Live mirror audit

The build uses the first healthy mirror, while the live audit tests **every
configured mirror independently**. It discovers only `source.type: mirrors`
applications from `apps/*.json`, resolves the version supported by each app's
Morphe patch, downloads the real artifact, validates its package type, exact
`aapt` version and universal ARM coverage, and writes a JSON report.

~~~bash
# All mirror entries in every mirror-backed app
python tools/mirror_probe.py --report mirror-report.json

# One target, useful while debugging a provider
python tools/mirror_probe.py \
  --config apps/pinterest.json --app pinterest \
  --arch universal --mirror apkcombo

# Keep the downloaded files for inspection (otherwise they are temporary)
python tools/mirror_probe.py --app adm --download-dir /tmp/morphe-mirror-audit
~~~

The command exits non-zero if any selected mirror fails, while still reporting
all other results. The `Live Mirror Downloads` workflow file is currently
disabled (`live-mirror-downloads.yml.disable`); when re-enabled, its matrix is
generated from configuration, so a new mirror-backed app is included without a
workflow edit. Each matrix job uploads its JSON/log report even when the
provider fails.

### Local APKMirror browser transport

Pinterest's APKMirror entry can use the pinned `invisible_playwright` Firefox
transport locally. It opens the exact configured variant page in a headed
window, closes only Google's visible vignette control when present, clicks the
download control once, watches the browser's `.part` file, and then runs the
normal exact package/version/universal-ABI validation:

~~~bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m invisible_playwright fetch
AAPT=/opt/android-sdk/build-tools/37.0.0/aapt \
  .venv/bin/python tools/mirror_download.py \
  --config apps/pinterest.json --arch universal \
  --exact-version 14.34.0 --output /tmp/pinterest-14.34.0.apk
~~~

This is an operator-run headed flow, not a CAPTCHA solver or unattended CI
integration. When `CI=true`, the downloader deliberately skips the browser
transport and uses the native `apkd` provider; the live mirror workflow still
runs its normal network validation. Set `APKMIRROR_BROWSER_ALLOW_CI=1` only for
an explicitly controlled CI diagnostic.

### Manual Uptodown download

Uptodown is registered as an opt-in provider, but its final download URL is
issued only after an interactive Cloudflare Turnstile flow. It is therefore
disabled in the normal Pinterest mirror chain until a browser-assisted
artifact has been validated locally:

~~~bash
python tools/uptodown_browser.py com.pinterest \
  --version 14.34.0 --app-slug pinterest --app-id 20013 \
  --output /tmp/pinterest-14.34.0.apk
~~~

For the current `14.34.0` test, use the APK candidate (`file_id=1210795434`).
The XAPK candidate is retained only for diagnostics; a browser response that
contains a monolithic APK or a different package is rejected rather than
renamed.

Run this on a local machine with a visible Chromium window. The script does
not use a third-party solver and validates exact version plus the universal ARM
contract after the browser download. When Chrome emits the final download
itself, the helper captures that file from the same browser context before the
context closes; otherwise it falls back to the signed URL with the browser
cookies and user agent. If Uptodown rejects Playwright's bundled
Chromium fingerprint, connect to a normal Chrome instance that you started
yourself (there is no challenge automation or solver):

~~~bash
# Terminal 1: use a dedicated profile so your normal profile stays untouched
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/uptodown-chrome about:blank

# Terminal 2: complete Turnstile in that Chrome window
python tools/uptodown_browser.py com.pinterest \
  --version 14.34.0 --app-slug pinterest --app-id 20013 \
  --cdp-url http://127.0.0.1:9222 --manual-click \
  --output /tmp/pinterest-14.34.0.apk
~~~

For a bounded automatic diagnostic (without CDP/manual click), use:

~~~bash
python tools/uptodown_browser.py com.pinterest \
  --version 14.34.0 --app-slug pinterest --app-id 20013 \
  --headless --initial-wait 5 --retry-wait 5 --max-attempts 2 \
  --response-timeout 30 --output /tmp/pinterest-14.34.0.apk
~~~

Headless mode cannot complete a human Turnstile and is not a challenge bypass.
It only retries the normal Download click up to the configured bound.

Keep the Pinterest entry `enabled: false`; GitHub
Actions has no interactive browser session, so it must not be part of
unattended CI.

## Documentation

See the detailed contributor and architecture guide:

- docs/ARCHITECTURE.md — application configuration, architecture handling, source types, version resolution, validation, patching, signing, releases, CI security, testing, and how to add applications.

## Security

The build workflow uses:

- least-privilege GitHub Actions permissions
- SHA-pinned Actions
- externalized signing secrets
- exact package and version validation
- checksum verification for the pinned Morphe CLI
- workflow concurrency
- a separate test workflow

Do not commit APKs, keystores, credentials or other release secrets.

## Repository layout

~~~text
apps/       Application configuration
tools/      Generic build/download/patch/release logic
             (including the config-driven mirror probe)
tests/      Unit and regression tests
docs/       Detailed project documentation
.github/    GitHub Actions workflows and repository automation
~~~
