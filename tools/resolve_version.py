#!/usr/bin/env python3
"""Resolve the APK version + prebuilt .mpp URL from a patch repo.

Source of truth: patches-list.json (not the README).
Flow:
  1. Fetch patches-bundle.json (main) -> .mpp version + download_url.
  2. Fetch patches-list.json at tag v<mpp_version> (fallback to main).
  3. Filter compatiblePackages by package name, collect targets,
     pick the highest stable version unless --allow-experimental.

Stdlib only (urllib) so it runs without pip install.
"""
import argparse
import json
import os
import re
import sys
import urllib.request


def fetch_json(url, token=None):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "morphe-auto-build",
            "Accept": "application/vnd.github.raw+json, application/json",
        },
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_version_tuple(ver):
    parts = re.findall(r"\d+", ver or "")
    return tuple(int(p) for p in parts) if parts else (0,)


def _target_experimental(target):
    """Experimental flag name differs between patches-list layouts."""
    return bool(target.get("isExperimental", target.get("experimental", False)))


def collect_target_versions(patches_list, package):
    """Return (version, is_experimental) pairs for one package.

    Two patches-list layouts are in use:

    * classic Morphe/ReVanced: ``compatiblePackages`` is a list of
      ``{"packageName", "targets": [{"version", "isExperimental"}]}``;
    * HushGram: ``compatiblePackages`` maps a package name to version strings,
      and the experimental flag lives in ``compatibility[].targets[]`` as
      ``{"version", "experimental"}``.

    Both are merged; a version counts as experimental if any entry says so.
    """
    found = {}

    def add(version, experimental=False):
        if version:
            key = str(version)
            found[key] = found.get(key, False) or bool(experimental)

    for patch in patches_list.get("patches") or []:
        if not isinstance(patch, dict):
            continue
        compatible = patch.get("compatiblePackages")
        if isinstance(compatible, dict):
            for version in compatible.get(package) or []:
                add(version)
        elif isinstance(compatible, list):
            for pkg in compatible:
                if not isinstance(pkg, dict) or pkg.get("packageName") != package:
                    continue
                for target in pkg.get("targets") or []:
                    if isinstance(target, dict):
                        add(target.get("version"), _target_experimental(target))
        for compat in patch.get("compatibility") or []:
            if not isinstance(compat, dict) or compat.get("packageName") != package:
                continue
            for target in compat.get("targets") or []:
                if isinstance(target, dict):
                    add(target.get("version"), _target_experimental(target))
    return list(found.items())


def pick_supported_version(targets, allow_experimental=False):
    if not targets:
        raise ValueError("No target versions for this package in patches-list.json")
    stable = [(v, e) for v, e in targets if not e]
    pool = targets if (allow_experimental or not stable) else stable
    # deduplicate, pick the highest version tuple
    uniq = {}
    for ver, exp in pool:
        uniq[ver] = uniq.get(ver, exp) or exp
    best = sorted(uniq.keys(), key=parse_version_tuple, reverse=True)[0]
    return best, uniq[best]


def resolve(patch_repo, package, allow_experimental=False, token=None):
    bundle_url = f"https://raw.githubusercontent.com/{patch_repo}/main/patches-bundle.json"
    bundle = fetch_json(bundle_url, token)
    mpp_version = bundle["version"]
    mpp_url = bundle["download_url"]

    tag = mpp_version if str(mpp_version).startswith("v") else f"v{mpp_version}"
    list_data = None
    last_err = None
    for url in (
        f"https://raw.githubusercontent.com/{patch_repo}/refs/tags/{tag}/patches-list.json",
        f"https://raw.githubusercontent.com/{patch_repo}/main/patches-list.json",
    ):
        try:
            list_data = fetch_json(url, token)
            break
        except Exception as e:  # noqa: BLE001 - fall back between URLs
            last_err = e
    if list_data is None:
        raise RuntimeError(f"Failed to fetch patches-list.json: {last_err}")

    targets = collect_target_versions(list_data, package)
    apk_version, is_experimental = pick_supported_version(targets, allow_experimental)
    return {
        "patch_repo": patch_repo,
        "package": package,
        "mpp_version": str(mpp_version),
        "mpp_url": mpp_url,
        "apk_version": apk_version,
        "apk_experimental": is_experimental,
    }


def main():
    parser = argparse.ArgumentParser(description="Resolve the APK version supported by a patch bundle")
    parser.add_argument("--config", help="Path to apps/<id>.json (alternative to --patch-repo/--package)")
    parser.add_argument("--patch-repo", help="e.g. Akash-Sriram/morphe-google-photos")
    parser.add_argument("--package", help="e.g. com.google.android.apps.photos")
    parser.add_argument("--allow-experimental", action="store_true")
    parser.add_argument("--json-out", help="Write the result JSON to a file (optional)")
    args = parser.parse_args()

    if args.config:
        with open(args.config) as fh:
            cfg = json.load(fh)
        patch_repo = cfg["patch_repo"]
        package = cfg["package"]
        allow_exp = cfg.get("allow_experimental", False) or args.allow_experimental
    else:
        if not args.patch_repo or not args.package:
            parser.error("--config or (--patch-repo + --package) is required")
        patch_repo, package, allow_exp = args.patch_repo, args.package, args.allow_experimental

    token = os.environ.get("GITHUB_TOKEN")
    try:
        result = resolve(patch_repo, package, allow_exp, token)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, indent=2))
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(result, fh, indent=2)
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"mpp_version={result['mpp_version']}\n")
            fh.write(f"mpp_url={result['mpp_url']}\n")
            fh.write(f"apk_version={result['apk_version']}\n")


if __name__ == "__main__":
    main()
