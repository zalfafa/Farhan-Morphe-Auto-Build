# Morphe Auto Build — Architecture and Contributor Guide

This document explains how the repository works, how sources are selected, how versions are resolved, how APKs are validated and patched, how releases are maintained, and how to add another application.

## 1. Project model

This repository is a configuration-driven GitHub Actions build system.

The intended flow is:

~~~text
apps/*.json
    ↓
app × source architecture (mirror apps default to universal)
    ↓
resolve Morphe patch + supported APK version
    ↓
download exact base package
    ↓
validate package + exact version
    ↓
run Morphe Desktop
    ↓
manifest.json
    ↓
zipalign
    ↓
apksigner
    ↓
verify
    ↓
monthly YYYY-MM release
~~~

The repository should contain automation and configuration, not copies of upstream Morphe patch definitions.

Core invariants:

- An APK version must be supported by the selected Morphe patch.
- A downloaded package must match the requested version.
- Invalid downloads must never reach Morphe.
- manifest.json is authoritative for patch outputs.
- Rebuilding one app/architecture must not delete unrelated release assets.
- Brave ARM64 and ARM32 remain supported.
- CI security controls must not be weakened to make a build pass.

## 2. Repository layout

~~~text
apps/
  brave.json
  google-photos.json

tools/
  common.py             Shared architecture/package validation
  resolve_version.py    Morphe target selection
  mirror_download.py    apkd-backed mirror fallback (apkpure/aptoide/apkcombo/apkmirror/uptodown)
  mirror_probe.py       Config-driven live audit for every mirror entry
  apkmirror_browser.py  Opt-in local headed APKMirror transport (Pinterest)
  uptodown_browser.py   Manual Uptodown browser helper (operator-run)
  github_source.py      GitHub Release downloader
  direct.py             Direct URL downloader
  patch.py              Morphe invocation + manifest
  monthly_release.py    Cumulative monthly release maintenance

tests/
  test_common.py
  test_mirror_download.py
  test_mirror_probe.py
  test_monthly_release.py
  test_resolve_version.py
  test_github_source.py
  test_apkmirror_browser.py
  test_mirror_download_live.py   (opt-in live download, skipped by default)

.github/workflows/
  build.yml             Build/sign/publish
  tests.yml             Unit/regression tests

docs/
  ARCHITECTURE.md       This document

requirements.txt        Python dependency pins
README.md               User-facing overview
~~~

## 3. Application configuration

Every application is normally represented by one file:

~~~text
apps/<id>.json
~~~

The workflow reads the files and automatically creates the build matrix.

A minimal configuration contains:

~~~json
{
  "id": "example",
  "display_name": "Example",
  "package": "com.example.app",
  "patch_repo": "OWNER/example-morphe-patches",
  "allow_experimental": false,
  "source": {},
  "flavors": []
}
~~~

Important fields:

| Field | Purpose |
| --- | --- |
| id | Stable application identifier and filename prefix |
| display_name | Human-readable name |
| package | Android package name |
| patch_repo | Morphe patch repository |
| allow_experimental | Permit experimental patch targets |
| source | Base package source |
| flavors | Output variants |
| enabled | Optional; false excludes the app from the matrix |

Do not add a separate workflow for a normal application.

## 4. Architectures

Architectures are configuration data:

~~~json
"archs": [
  {
    "name": "arm64",
    "asset": "BraveMonoarm64.apk"
  },
  {
    "name": "arm32",
    "asset": "BraveMonoarm.apk"
  }
]
~~~

Multiple entries become multiple GitHub Actions matrix jobs.

### Brave

~~~text
arm64 → arm64-v8a → BraveMonoarm64.apk
arm32 → armeabi-v7a → BraveMonoarm.apk
~~~

ARM32 is a first-class target. Do not remove it when modifying the Brave configuration or workflow.

### Universal contract

`arch: universal` means **both** ARM targets in the same downloaded artifact:

~~~text
arm64-v8a + armeabi-v7a
~~~

It does not mean "prefer arm64" and it does not mean an arm64-only fallback.
The downloader passes the universal request to the provider without replacing
it with a single ABI. After download, `validate_package(..., expected_arch)`
inspects `aapt dump badging` for an APK and every APK entry in an APKM/XAPK.
The audit fails if either ARM ABI is missing. A package with no native code is
architecture-independent and is accepted; extra ABIs such as x86_64 are fine.

If a provider publishes separate arm64 and arm32 assets but no combined asset,
that provider is **not** a valid universal mirror. Keep the failure visible and
let another exact-version universal mirror handle the build; never relabel an
arm64-only file as universal.

When *no* mirror publishes a universal package for an app at all (Native
Camera: only APKPure carries it, as separate arm64/arm32 XAPKs; Instagram
439.0.0.37.89: separate ARM64_V8A 384510827 / ARMEABI_V7A 384510826 APKM
bundles), the config
declares explicit per-ABI entries instead of taking the implicit contract:

~~~json
"source": {
  "type": "mirrors",
  "file_type": "xapk",
  "archs": [{"name": "arm64"}, {"name": "arm32"}],
  "mirrors": [{"type": "apkpure", "name": "native-camera", "file_type": "xapk"}]
}
~~~

Each entry becomes its own matrix job downloading and validating that ABI
(`expected_arch=arm64` requires `arm64-v8a`, not both). `universal` must never
be mixed with concrete ABIs in one list, and the requested arch must be one
the config declares — otherwise the downloader refuses before any network
request. A concrete name that is not a real ABI is rejected the same way.

### Google Photos

Google Photos uses the same implicit universal mirror contract as the other
mirror-backed applications; the selected provider is responsible for supplying
one combined exact-version artifact.

## 5. Source system

The source is selected with source.type.

Supported types:

~~~text
mirrors
apkmirror
github
direct
~~~

### mirrors

Use this when several sources can provide the same exact version.

Current Google Photos order:

~~~text
APKMirror
    ↓ failure
APKPure
    ↓ failure
APKCombo
    ↓ failure
Aptoide
~~~

Every candidate is resolved at its exact version through the
[apkd](https://github.com/Fahry-a/apkd) library
(`apkd @ git+https://github.com/Fahry-a/apkd` in requirements.txt),
downloaded to a temporary .partial file and validated before it becomes
the final package. Provider resolve/download calls get a bounded retry
(`--attempts`, default 2, or `MIRROR_DOWNLOAD_ATTEMPTS`); a deterministic
version or file-type mismatch is not retried. A `mirrors` source does not
declare `source.archs`; its architecture contract is implicitly `universal`.

### APKMirror

`source.type == "mirrors"` entries with `{"type": "apkmirror"}` go through
apkd and only need the org/repo slug:

~~~json
{"type": "apkmirror", "org": "admtorrent", "repo": "advanced-download-manager"}
~~~

or `{"type": "apkmirror", "slug": "admtorrent/advanced-download-manager"}`.
apkd discovers the real variants URL from the repo version listing (the display
slug often differs from the repo slug: `x-` vs `twitter`,
`google-photos-` vs `photos`) and filters arch/dpi client-side.

The native apkd mirror provider does not silently bypass a challenge: a
`403`/Turnstile response is reported as `APKMirror Cloudflare/Turnstile
challenge`, so the target can be completed manually in a browser and retried.
A challenge is not evidence that the package lacks a universal ABI, and a
transport-level failure is never reported as "slug not found".

Optional per-mirror hints: `arch`, `dpi`, `min_sdk`. `dpi` defaults to
`"*"` (APKMirror rows are usually density-scoped like `120-640dpi`, so
`nodpi`-only would match nothing). The mirror dispatcher itself always keeps
its implicit `universal` contract and never retries as `arm64-v8a`. When the
exact version only ships as a bundle, the mirror must expose a compatible
bundle container; APKM and XAPK are equivalent bundle labels, while an APK is
not accepted for a bundle-only request.

Bundle-sourced apps declare it at the source level instead — ADM and X use
`"file_type": "apkm"`, so the base file is `base.apkm`, validation checks
the inner `base.apk` version with aapt, and Morphe merges the splits before
patching (verified locally: ADM 14.0.39 `.apkm` patches cleanly into an
unsigned APK). Per-mirror `file_type` still overrides per mirror, so APK
mirrors stay usable as fallback wherever they serve the same version as APK.

### APKPure

APKPure is handled by tools/mirror_download.py via apkd.

The downloader resolves the requested application version and extracts its package URL. It must not silently replace the requested version with latest.

APKPure serves both monolithic APK and XAPK bundle assets. Both are accepted; the asset type matching the app config's file_type is preferred, so XAPK-only releases (e.g. Native Camera) download and validate as bundles.

### APKCombo

APKCombo is handled by tools/mirror_download.py via apkd.

The downloader searches by package name, resolves the exact requested
version (including old-versions), picks the matching arch variant, then
validates the resulting package.

Uptodown was previously removed because its final file URL is issued after a
Cloudflare Turnstile flow. It is now available again only as an explicit,
browser-assisted provider; it is not part of the default unattended fallback
order.

### Aptoide

Aptoide is accessed through its public API.

The downloader finds the requested vername, obtains the corresponding vercode, requests metadata and downloads the returned package path.

### Uptodown

Uptodown is an opt-in, browser-assisted provider. Its public version metadata
is resolved normally, but the final file URL is issued only after an
interactive Cloudflare Turnstile flow. Use the local helper rather than
unattended CI:

~~~bash
python tools/uptodown_browser.py com.pinterest \
  --version 14.34.0 --app-slug pinterest --app-id 20013 \
  --output /tmp/pinterest-14.34.0.apk
~~~

For Pinterest `14.34.0`, the manual test uses the APK candidate
(`file_id=1210795434`). A container/package mismatch is rejected; bytes are
never relabeled merely to satisfy the requested extension.

The helper opens a visible Playwright Chromium window, waits for the operator
to complete the normal challenge, captures Uptodown's download response, and
then runs the same exact-version and universal ABI validation as the build. If
Chrome emits the final download, the provider captures it from the same
browser context before disconnecting; otherwise it transfers the signed URL
with the browser cookies and user agent. If the bundled Chromium is rejected
by Turnstile, start a normal Chrome with a
dedicated profile and remote debugging, then pass its CDP endpoint with
`--cdp-url`; `--manual-click` lets the operator press Uptodown's Download
button while Playwright only observes the normal response. The operator still
completes the challenge manually. A bounded headless diagnostic can retry the
ordinary click with a five-second wait, but cannot solve Turnstile. No
third-party CAPTCHA solver or stealth/automation bypass is used.

Variant pages (the `-x` suffix trap): the plain `/download/{file_id}` page
serves Uptodown's own store wrapper (`com.uptodown`), not the app — verified
by hand for Pinterest 14.34.0 (`/download/1210795434` → Uptodown 7.39,
`/download/1210795434-x` → `com.pinterest` 14.34.0, universal APK:
arm64-v8a/armeabi-v7a/x86/x86_64, 124.92 MB). The resolver therefore reads the
"All variants" panel (`/app/{app_id}/version/{version_id}/files`, no browser
needed), picks the same-kind variant covering the requested ABI (universal =
arm64+arm32), and targets its `/download/{id}-x` page. The Pinterest Uptodown
entry is enabled with `file_type: apk` pointing at that universal variant.

Stealth engine: with `APKD_UPTODOWN_INVISIBLE=1` the download page is driven
by `invisible_playwright` (stealth Firefox) instead of stock Chromium
(`apkd[invisible]` extra). This only presents a coherent fingerprint — it is
not a CAPTCHA solver, and a persistent interactive challenge still needs an
operator (or another `APKD_UPTODOWN_SEED`). Verified end to end from a
residential IP: headless invisible solved the Turnstile, clicked the `-x`
variant page, and downloaded the exact universal APK above (aapt:
`com.pinterest` 14.34.0, 130983955 bytes). An explicit `--cdp-url` always wins
over invisible mode. Unattended CI still fails fast with "requires an
interactive browser" when the challenge does not clear and moves on, while an
operator run (`APKD_UPTODOWN_BROWSER=1` or the helper below) downloads the
real artifact.

### Live mirror audit

The production fallback chain and the live audit have different jobs:

~~~text
production build:
  configured order → first valid exact package → patch

live audit:
  every configured mirror → independent exact package download → report
~~~

`tools/mirror_probe.py` discovers mirror targets from `apps/*.json`; it does
not contain an application allowlist. For each target it:

1. Resolves the patch-supported version once per app.
2. Calls the existing `download_from_mirror` implementation for that provider.
3. Validates the configured package type, exact version and ARM contract with `aapt`.
4. Records size, ZIP entries, SHA-256, duration and the error, if any.
5. Removes temporary downloads after validation unless `--download-dir` is set.

A provider failure does not prevent the remaining targets from being tested.
The command exits non-zero after the complete run, so GitHub Actions shows a
real provider regression instead of hiding it behind fallback. A mirror entry
may set `enabled: false` when it is intentionally retained as documentation but
must not be treated as an active build source or audit target.

Run it with:

~~~bash
python tools/mirror_probe.py --report mirror-report.json
python tools/mirror_probe.py --config apps/x.json --mirror apkcombo
~~~

The live audit matrix is built from the same discovery function when the
audit workflow is enabled. (The `Live Mirror Downloads` workflow file is
currently stored as `live-mirror-downloads.yml.disable`, so it does not run;
re-enable it by restoring the `.yml` name and fixing its `paths:` filter,
which still names the old filename. Until then this paragraph describes intent,
not CI behaviour.) Adding a new mirror-backed application is
configuration-only from a CI perspective.

### Per-mirror package types

Mirror configurations may override the source-level package type with their own `file_type`. This is useful when different mirrors publish the same application version in different package formats.

Example:

~~~json
{
  "type": "mirrors",
  "file_type": "apk",
  "mirrors": [
    {
      "type": "apkpure",
      "name": "pinterest",
      "file_type": "xapk"
    },
    {
      "type": "apkcombo",
      "name": "pinterest",
      "file_type": "xapk"
    },
    {
      "type": "aptoide",
      "file_type": "apk"
    }
  ]
}
~~~

The effective package type is resolved in this order:

~~~text
mirror.file_type
       ↓ if absent
source.file_type
       ↓ if absent
apk
~~~

The selected type is used both when choosing a mirror asset and when validating the downloaded package. This prevents a bundle such as XAPK from being treated as a monolithic APK. The CLI writes the successful artifact to a path with the selected extension and publishes that actual `base_file`/`file_type` pair to GitHub Actions, so a mirror-level override remains consistent through patching.

For example, a Pinterest configuration can explicitly document:

| Mirror | Format that is validated |
| --- | --- |
| APKPure | XAPK |
| APKCombo | XAPK |
| Aptoide | APK |

This also handles mirrors that return a bundle for a requested version. The package validator must validate the configured bundle type rather than only checking for an APK root manifest.


### GitHub

Use:

~~~json
"source": {
  "type": "github",
  "repo": "OWNER/REPOSITORY",
  "tag_prefix": "v",
  "file_type": "apk",
  "archs": [
    {
      "name": "arm64",
      "asset": "ExampleArm64.apk"
    }
  ]
}
~~~

tools/github_source.py resolves the exact release tag and exact asset name using the GitHub API.

This is currently used for Brave.

### Direct

Use a direct source for a known URL pattern:

~~~json
"source": {
  "type": "direct",
  "file_type": "xapk",
  "archs": [
    {
      "name": "universal",
      "url": "https://example.invalid/releases/{version}/{package}-{version}.xapk"
    }
  ]
}
~~~

Supported placeholders:

- {version}
- {package}
- {arch}

tools/direct.py downloads to .partial, validates the package and exact version, then atomically moves it into place.

## 6. Version resolution

tools/resolve_version.py is the authority for selecting the application version.

Conceptually:

~~~text
patch repository
    ↓
prebuilt MPP release
    ↓
patches-list.json
    ↓
matching Android package
    ↓
stable/experimental filtering
    ↓
highest supported target
~~~

The resolver returns the MPP version, MPP URL and supported APK version.

There is deliberately no arbitrary application-version input.

This prevents a user from forcing an APK version that the selected Morphe patch does not declare as compatible.

Experimental targets are excluded unless enabled through configuration or the manual allow_experimental input.

## 7. Package validation

tools/common.py provides shared validation.

Validation checks:

1. Minimum file size.
2. Valid ZIP structure.
3. APK contains AndroidManifest.xml.
4. APKM/XAPK contains APK entries.
5. Exact version can be read with aapt.
6. Detected version equals the requested resolver version.
7. Universal packages contain both arm64-v8a and armeabi-v7a.

For a bundle, base.apk is preferred; otherwise the first APK entry is checked.

This is important because HTTP 200 does not mean "valid APK". A server can return HTML, Cloudflare content, a partial download or the wrong release with status 200.

Validation must happen before Morphe.

## 8. Patching and manifest

tools/patch.py invokes Morphe Desktop once for every configured flavor.

A flavor contains:

~~~json
{
  "name": "special",
  "description": "Special patch combination",
  "patch_include": [
    "Patch A",
    "Patch B"
  ],
  "output_suffix": "-special"
}
~~~

Morphe produces unsigned APKs.

tools/patch.py writes manifest.json containing the files actually produced.

Later stages use the manifest rather than reconstructing filenames.

This is important because the patcher, not the workflow's assumptions, is the authority on which files exist.

## 9. Signing

The final order is:

~~~text
Morphe unsigned APK
    ↓
zipalign
    ↓
apksigner
    ↓
apksigner verify
    ↓
final APK
~~~

The workflow must not rewrite the APK ZIP after zipalign. Repacking after alignment can invalidate the alignment.

The release keystore comes from GitHub Actions secrets and is deleted after signing.

## 10. Morphe CLI integrity

The workflow pins the Morphe Desktop version:

~~~text
MORPHE_CLI_VERSION=1.17.0
~~~

The exact JAR is SHA-256 verified before it is executed.

When upgrading Morphe:

1. Choose the intended official release.
2. Obtain the checksum for the exact JAR.
3. Update version and checksum together.
4. Run tests.
5. Review the workflow diff.
6. Verify the resulting CI run.

Do not remove checksum validation as a workaround.

## 11. Monthly release system

Release tags use:

~~~text
YYYY-MM
~~~

tools/monthly_release.py updates one app/architecture at a time.

For a new build it:

1. Uploads current APKs with --clobber.
2. Finds stale assets using the current app + architecture prefix.
3. Deletes only those stale assets.
4. Replaces only the marked release-note section for that app/architecture.
5. Preserves unrelated assets and release-note sections.

Example:

~~~text
Brave ARM64 rebuilt
    ↓
old brave-arm64 assets may be removed

Brave ARM32
Google Photos
other applications
    ↓
must remain
~~~

## 12. Existing-release optimization

Before downloading or patching, the workflow calculates the expected output names from the application configuration and resolved version.

If every expected asset already exists in the current monthly release **and** the release-note section for that app/architecture records the same Morphe version that was just resolved, that matrix job is skipped.

The recorded Morphe version is read from the section's build table:

~~~text
| v14.0.39 | 1.53.0 | 2026-09-24 |
            ↑ compared with the resolved mpp_version
~~~

A leading `v` is normalized before comparing. When the patch repository publishes a new `.mpp` while the supported APK version stays the same, the comparison fails and the job rebuilds, so the published APK always carries the currently resolved patch. A section that is missing or has no parsable build table counts as stale and is rebuilt.

Use:

~~~text
rebuild=true
~~~

when a manual rebuild is required.

This optimization exists to avoid expensive downloads and patch operations.

## 13. Adding a new application

### Step 1: Verify Morphe support

Confirm:

- patch repository
- prebuilt MPP release
- patches-list.json
- Android package name
- supported application versions

Do not guess compatibility.

### Step 2: Choose a source

Prefer an existing generic source type.

Use:

- github for GitHub release assets
- mirrors for fallback mirrors
- apkmirror for APKMirror-specific configuration
- direct for a known URL pattern

Only add downloader code when the existing generic sources cannot represent the source.

### Step 3: Create apps/<id>.json

Example:

~~~json
{
  "id": "example",
  "display_name": "Example",
  "package": "com.example.app",
  "patch_repo": "OWNER/example-morphe-patches",
  "allow_experimental": false,
  "source": {
    "type": "github",
    "repo": "OWNER/example",
    "tag_prefix": "v",
    "file_type": "apk",
    "archs": [
      {
        "name": "arm64",
        "asset": "ExampleArm64.apk"
      }
    ]
  },
  "flavors": [
    {
      "name": "default",
      "description": "Default patches",
      "patch_include": [],
      "output_suffix": ""
    }
  ]
}
~~~

### Step 4: Test version resolution

~~~bash
python3 tools/resolve_version.py   --config apps/example.json
~~~

### Step 5: Test the downloader

GitHub:

~~~bash
python3 tools/github_source.py   --config apps/example.json   --arch arm64   --apk-version <version>   --output base.apk
~~~

Mirrors:

~~~bash
python3 tools/mirror_download.py   --config apps/example.json   --arch universal   --exact-version <version>   --output base.apk
~~~

### Step 6: Add regression tests

Test exact-version behavior and failure cases for any new downloader behavior.

### Step 7: Run the complete test suite

~~~bash
python -m unittest discover -s tests -v
~~~

### Step 8: Update README

Add the application, source order, architectures and flavors to the supported-app documentation.

## 14. Adding a flavor

Normally this requires only configuration:

~~~json
{
  "name": "special",
  "description": "Special patches",
  "patch_include": [
    "Patch A"
  ],
  "output_suffix": "-special"
}
~~~

The generic workflow automatically patches, signs and publishes it.

## 15. Adding a mirror

A new mirror implementation must:

1. Resolve the exact requested version.
2. Return a package URL.
3. Download to .partial.
4. Validate package shape.
5. Validate exact version.
6. Remove partial files after failure.
7. Have regression tests.
8. Be added to configuration in the intended fallback order.
9. Be documented.

Never use "latest" as an implicit fallback for an exact-version request.

## 16. Local development with uv

Python dependencies are installed with uv.

~~~bash
uv pip install --system -r requirements.txt
~~~

Install Playwright Chromium:

~~~bash
python -m playwright install chromium --with-deps
~~~

Run tests:

~~~bash
python -m unittest discover -s tests -v
~~~

CI uses the SHA-pinned astral-sh/setup-uv action.

## 17. Local build requirements

A complete build environment needs:

- Python 3.12+
- uv
- Java 21+
- Android build tools containing aapt, zipalign and apksigner
- Chromium for the Playwright fallback
- Morphe Desktop JAR
- compatible Morphe MPP
- GitHub CLI for release maintenance

Signing secrets are only expected in GitHub Actions.

## 18. Testing strategy

Tests cover:

- architecture selection
- multi-architecture requirements
- APK shape validation
- APKM/XAPK shape validation
- exact APK version validation
- exact bundle version validation
- patch target selection
- experimental target selection
- Aptoide exact-version lookup
- configuration-driven mirror target discovery and independent live reports
- stale release assets
- cumulative release-note sections

Run:

~~~bash
python -m unittest discover -s tests -v
# network-backed; downloads every configured mirror target
python tools/mirror_probe.py --report mirror-report.json
~~~

When fixing a real bug, add a regression test before considering the fix complete.

## 19. CI security

Preserve:

- contents: read by default
- write permission only in the publish job
- SHA-pinned Actions
- signing secrets outside the repository
- package validation
- exact-version validation
- Morphe CLI checksum verification
- workflow concurrency
- separate test workflow

Do not fix CI by disabling validation, exposing secrets, removing tests or using unverified critical binaries.

## 20. Common failure modes

### HTTP 200 but Morphe fails

Check whether the downloaded file is HTML, a Cloudflare response, a partial file, the wrong package or the wrong version.

### Wrong application version

Fix source resolution. Do not accept the wrong version.

### Unsupported patch target

The selected APK version is not declared compatible by the patch. Do not invent the pairing.

### Brave ARM32 missing

Check apps/brave.json for:

~~~json
{
  "name": "arm32",
  "asset": "BraveMonoarm.apk"
}
~~~

Then confirm the matrix includes arm32.

### Release deletes another application

Check tools/monthly_release.py. Stale deletion must be limited to the current app and architecture.

### APK is not aligned

The final sequence must remain:

~~~text
zipalign → apksigner → apksigner verify
~~~

No ZIP rewrite belongs after zipalign.

## 21. Change checklist

Before opening a PR:

- Inspect current GitHub state.
- Prefer configuration over duplicated workflow code.
- Preserve patch/application compatibility.
- Preserve exact package validation.
- Preserve Brave ARM64 and ARM32.
- Keep manifest.json authoritative.
- Preserve unrelated monthly release assets.
- Keep Actions SHA-pinned.
- Keep permissions least-privilege.
- Do not commit secrets.
- Add regression tests for meaningful bugs.
- Run the full test suite.
- Update documentation for behavior changes.
- Review CI impact.

## 22. Current supported applications

### Google Photos

~~~text
Package:       com.google.android.apps.photos
Patch repo:    Akash-Sriram/morphe-google-photos
Sources:       APKMirror → APKPure → APKCombo → Aptoide
Architecture:  universal
Flavors:       mod, original
~~~

### Brave Browser

~~~text
Package:       com.brave.browser
Patch repo:    kveld9/kveld-morphe-patches
Source:        brave/brave-browser GitHub releases
Architectures: arm64, arm32

arm64 → BraveMonoarm64.apk
arm32 → BraveMonoarm.apk
~~~

### Mirror-backed applications

The remaining enabled applications are discovered from their JSON files and
use the same mirror dispatcher:

| Application | Package | Patch repository | Mirrors | Format |
| --- | --- | --- | --- | --- |
| AudioRelay | `com.azefsw.audioconnect` | `kiraio-moe/Lain-Patches` | APKPure → APKCombo → Aptoide | APK |
| Native Camera | `com.rawcam.app` | `WaggBR/Wagg13Patch_Morphe` | APKPure (arm64 + arm32 jobs; no universal package published) | XAPK |
| Pinterest | `com.pinterest` | `browzomje/browzomje-patches` | APKMirror → APKCombo → APKPure → Aptoide | APK |
| Advanced Download Manager | `com.dv.adm` | `arandomhooman/hoomans-morphe-patches` | APKMirror → APKPure → APKCombo → Aptoide | APKM |
| X / Twitter | `com.twitter.android` | `crimera/piko` | APKMirror → APKPure → APKCombo → Aptoide | APKM |
| Instagram | `com.instagram.android` | `SysAdminDoc/HushGram` | APKPure → APKMirror → APKCombo → Aptoide | APKM (arm64 + arm32 jobs; no universal package published) |

All of these use `source.type: mirrors` and the implicit universal contract,
except Native Camera and Instagram, which declare explicit per-ABI `source.archs` because
no mirror publishes a universal package for them. The live audit
discovers them from the same files rather than maintaining a second hardcoded
list.

## 23. Guiding principle

Keep this repository a thin automation layer around upstream Morphe metadata and application sources.

Prefer:

~~~text
new application configuration
+
generic source behavior
+
regression test
~~~

over:

~~~text
new workflow
+
copied patch definitions
+
application-specific assumptions
~~~
