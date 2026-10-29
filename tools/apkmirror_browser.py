#!/usr/bin/env python3
"""Local, opt-in APKMirror download transport backed by a Playwright browser.

The normal CI/build path remains the native ``apkd`` HTTP provider.  This
module is deliberately separate: it opens one browser, clears the Cloudflare
Turnstile interstitial when APKMirror serves one, closes only Google's visible
ad-vignette control when present, clicks the download control once, and watches
the browser's filesystem download because patched Firefox does not reliably
expose ``expect_download`` events.

The challenge interaction lives in ``tools/turnstile.py``: it unlocks closed
shadow roots, clicks the widget's checkbox with a humanized mouse path, and
polls until the challenge page is gone.  It does not fabricate tokens, and the
downloaded bytes are only staged; the caller still runs its normal exact
package/version/architecture validator before publishing the file.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

try:  # package import (tools.mirror_download)
    from .turnstile import (
        challenge_state,
        describe_challenge,
        install_shadow_root_unlock,
        solve_turnstile_challenge,
    )
except ImportError:  # direct ``python tools/apkmirror_browser.py`` use
    from turnstile import (  # type: ignore[no-redef]
        challenge_state,
        describe_challenge,
        install_shadow_root_unlock,
        solve_turnstile_challenge,
    )


class APKMirrorBrowserError(RuntimeError):
    """Raised when the browser transport cannot produce a complete package."""


VIGNETTE_CLOSE_SELECTOR = "#dismiss-button-element"
DOWNLOAD_SELECTOR = "a.downloadButton[href*='/download/']"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _active_page(context: Any):
    """Choose the APKMirror page without holding a stale page object."""
    try:
        pages = [page for page in context.pages if not page.is_closed()]
    except Exception:
        return context.new_page()
    for page in reversed(pages):
        try:
            if "apkmirror.com" in (page.url or ""):
                return page
        except Exception:
            continue
    return pages[-1] if pages else context.new_page()


def _frames(page: Any) -> list[Any]:
    try:
        return list(page.frames)
    except Exception:
        return [page]


CHALLENGE_MARKERS = (
    "Just a moment",
    "Performing security verification",
    "Verify you are human",
    "Enable JavaScript and cookies",
    "Attention Required",
)


def describe_page(page: Any) -> dict[str, Any]:
    """Best-effort snapshot of what the browser actually shows.

    Read-only: title, URL and challenge markers only. Never interacts
    with any challenge. Every probe is guarded because the patched
    Firefox may dispose the session mid-transition.
    """
    state: dict[str, Any] = {}
    try:
        state["url"] = page.url
    except Exception:
        pass
    try:
        state["title"] = page.title()
    except Exception:
        pass
    try:
        text = page.locator("body").inner_text(timeout=3000)
        state["markers"] = [m for m in CHALLENGE_MARKERS if m.lower() in text.lower()]
        state["body_preview"] = text[:300]
    except Exception as exc:
        state["body_error"] = f"{type(exc).__name__}: {exc}"
    try:
        state["challenge"] = describe_challenge(page)
    except Exception as exc:
        state["challenge_error"] = f"{type(exc).__name__}: {exc}"
    return state


def dismiss_google_vignette(page: Any, *, log: Callable[[str], None] = print) -> bool:
    """Close one visible Google vignette, including inside child frames."""
    for frame in _frames(page):
        try:
            buttons = frame.locator(VIGNETTE_CLOSE_SELECTOR)
            count = buttons.count()
        except Exception:
            continue
        for index in range(min(count, 4)):
            try:
                candidate = buttons.nth(index)
                if not candidate.is_visible():
                    continue
                candidate.scroll_into_view_if_needed(timeout=2000)
                # The ad is a top-layer overlay; force avoids a second click on
                # the underlying APKMirror control if actionability is delayed.
                candidate.click(force=True, timeout=5000)
                log("[APKMirror browser] closed Google vignette")
                return True
            except Exception:
                continue
    return False


def find_download_href(page: Any, *, exact_fragment: str | None = None) -> str | None:
    """Find the single APKMirror download anchor on a variant page."""
    selectors = [DOWNLOAD_SELECTOR]
    if exact_fragment:
        selectors.insert(
            0,
            f"a.downloadButton[href*='/download/'][href*='{exact_fragment}']",
        )
    for selector in selectors:
        try:
            buttons = page.locator(selector)
            count = buttons.count()
        except Exception:
            continue
        for index in range(count):
            try:
                candidate = buttons.nth(index)
                href = candidate.get_attribute("href") or ""
            except Exception:
                continue
            if not href or (exact_fragment and exact_fragment not in href):
                continue
            return href
    return None


def _click_download_once(page: Any, href: str, *, timeout_ms: int = 20_000) -> None:
    """Re-query and click the previously identified button exactly once."""
    exact_fragment = None
    marker = "android-apk-download"
    if marker in href:
        exact_fragment = href.split("/android-apk-download", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    selectors = [DOWNLOAD_SELECTOR]
    if exact_fragment:
        selectors.insert(
            0,
            f"a.downloadButton[href*='/download/'][href*='{exact_fragment}']",
        )
    button = None
    for selector in selectors:
        try:
            buttons = page.locator(selector)
            count = buttons.count()
        except Exception:
            continue
        for index in range(count):
            try:
                candidate = buttons.nth(index)
                candidate_href = candidate.get_attribute("href") or ""
            except Exception:
                continue
            if candidate_href == href or (exact_fragment and exact_fragment in candidate_href):
                button = candidate
                break
        if button is not None:
            break
    if button is None:
        raise APKMirrorBrowserError("APKMirror download button disappeared before click")
    try:
        button.scroll_into_view_if_needed(timeout=5000)
        button.click(timeout=timeout_ms)
    except Exception as exc:
        raise APKMirrorBrowserError(f"APKMirror download click failed: {exc}") from exc


def _package_shape(path: Path, expected_extension: str) -> bool:
    """Check only enough ZIP shape to avoid staging a partial response."""
    try:
        if not zipfile.is_zipfile(path):
            return False
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False
    normalized = str(expected_extension or ".apk").lower().lstrip(".")
    if normalized == "apk":
        return "AndroidManifest.xml" in names
    return any(name.lower().endswith(".apk") for name in names)


class _ClearanceExpired(APKMirrorBrowserError):
    """Cloudflare rejected a warmed cookie before the file was served."""


def _challenge_response(status: int, body: str) -> bool:
    lowered = body.lower()
    markers = ("just a moment", "performing security verification",
               "verify you are human", "enable javascript and cookies")
    return status in (403, 429, 503) and any(marker in lowered for marker in markers)


def _download_via_http(
    clearance: Any,
    page_url: str,
    destination: Path,
    *,
    expected_extension: str,
    download_timeout: float,
    log: Callable[[str], None],
    session_factory: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """Fetch variant page, download page and package over plain HTTP.

    The browser path exists to obtain ``cf_clearance``.  Once that cookie is
    warm, the three APKMirror pages are ordinary GETs and curl_cffi can carry
    them with the matching Firefox TLS fingerprint.
    """
    try:
        from bs4 import BeautifulSoup
        from curl_cffi import requests
    except ImportError as exc:  # pragma: no cover - hard dependency
        raise APKMirrorBrowserError(
            "curl_cffi and beautifulsoup4 are required for the HTTP fallback"
        ) from exc
    from urllib.parse import urljoin

    session = (
        session_factory(clearance)
        if session_factory is not None
        else requests.Session(impersonate="firefox")
    )
    headers = {
        "User-Agent": clearance.user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        session.cookies.set(
            "cf_clearance", clearance.value, domain=clearance.host
        )
        response = session.get(page_url, headers=headers, timeout=60)
        if _challenge_response(response.status_code, response.text):
            raise _ClearanceExpired(
                "APKMirror served a Cloudflare challenge to the warmed cookie"
            )
        if response.status_code != 200:
            raise APKMirrorBrowserError(
                f"APKMirror variant page returned HTTP {response.status_code}"
            )
        soup = BeautifulSoup(response.text, "html.parser")
        button = soup.select_one("a.downloadButton[href*='/download/']")
        if button is None:
            raise APKMirrorBrowserError(
                "APKMirror variant page has no download button"
            )
        download_page = urljoin("https://www.apkmirror.com", button["href"])
        log(f"[APKMirror HTTP] download page: {download_page.split('?', 1)[0]}")

        response = session.get(
            download_page,
            headers={**headers, "Referer": page_url},
            timeout=60,
        )
        if _challenge_response(response.status_code, response.text):
            raise _ClearanceExpired(
                "APKMirror challenged the download page despite the cookie"
            )
        if response.status_code != 200:
            raise APKMirrorBrowserError(
                f"APKMirror download page returned HTTP {response.status_code}"
            )
        soup = BeautifulSoup(response.text, "html.parser")
        link = soup.select_one("a#download-link[href], a[href*='download.php']")
        if link is None:
            raise APKMirrorBrowserError(
                "APKMirror download page has no file link"
            )
        file_url = urljoin("https://www.apkmirror.com", link["href"])
        log(f"[APKMirror HTTP] file: {file_url.split('?', 1)[0]}")

        partial = destination.with_name(destination.name + ".partial")
        partial.unlink(missing_ok=True)
        total = 0
        try:
            with session.stream(
                "GET",
                file_url,
                headers={**headers, "Referer": download_page},
                timeout=download_timeout,
            ) as stream:
                if _challenge_response(
                    stream.status_code, stream.text if stream.status_code != 200 else ""
                ):
                    raise _ClearanceExpired(
                        "APKMirror challenged the file endpoint"
                    )
                if stream.status_code != 200:
                    raise APKMirrorBrowserError(
                        f"APKMirror file endpoint returned HTTP {stream.status_code}"
                    )
                with partial.open("wb") as handle:
                    for chunk in stream.iter_content(262_144):
                        if chunk:
                            handle.write(chunk)
                            total += len(chunk)
        except _ClearanceExpired:
            partial.unlink(missing_ok=True)
            raise
        except Exception as exc:
            partial.unlink(missing_ok=True)
            raise APKMirrorBrowserError(
                f"APKMirror file download failed: {exc}"
            ) from exc
        if total < 1_000_000 or not _package_shape(partial, expected_extension):
            partial.unlink(missing_ok=True)
            raise APKMirrorBrowserError(
                f"APKMirror file is not a valid {expected_extension} package "
                f"({total:,} bytes)"
            )
        partial.replace(destination)
        log(f"[APKMirror HTTP] saved {total:,} bytes to {destination}")
        return {
            "path": str(destination),
            "source_path": str(destination),
            "bytes": total,
            "vignette_dismissed": False,
            "headless": None,
            "seed": None,
            "transport": "firefox-http",
        }
    finally:
        try:
            session.close()
        except Exception:
            pass


def _download_files(directory: Path, *, min_size: int = 1_000_000) -> list[Path]:
    result: list[Path] = []
    try:
        paths = list(directory.iterdir())
    except OSError:
        return result
    for path in paths:
        if not path.is_file():
            continue
        name = path.name.lower()
        # Playwright saves intercepted downloads under a GUID without an
        # extension; Firefox's own download manager keeps the real suffix.
        # The package-shape check downstream is what rejects non-packages.
        if "." in name and not name.endswith(
            (".apk", ".apkm", ".apks", ".xapk", ".part", ".crdownload")
        ):
            continue
        try:
            if path.stat().st_size > min_size:
                result.append(path)
        except OSError:
            continue
    return sorted(result, key=lambda item: item.stat().st_size, reverse=True)


def _wait_for_download_start(
    directory: Path,
    *,
    timeout: float,
    poll_interval: float,
    sleep: Callable[[float], None],
    log: Callable[[str], None],
) -> Path | None:
    """Return the first file the browser starts writing, if any."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        candidates = _download_files(directory, min_size=1)
        if candidates:
            log(
                "[APKMirror browser] download started: "
                f"{candidates[0].name}"
            )
            return candidates[0]
        sleep(min(2.0, poll_interval))
    return None


def _wait_for_download(
    directory: Path,
    destination: Path,
    *,
    expected_extension: str,
    timeout: float,
    poll_interval: float,
    sleep: Callable[[float], None],
    log: Callable[[str], None],
) -> tuple[Path, int, int]:
    deadline = time.monotonic() + timeout
    previous: tuple[str, int] | None = None
    stable = 0
    while time.monotonic() < deadline:
        sleep(poll_interval)
        candidates = _download_files(directory)
        if not candidates:
            previous = None
            stable = 0
            continue
        candidate = candidates[0]
        try:
            size = candidate.stat().st_size
        except OSError:
            continue
        current = (str(candidate), size)
        if current == previous:
            stable += 1
        else:
            previous = current
            stable = 0
        log(f"[APKMirror browser] download candidate: {candidate.name} ({size:,} bytes)")
        if stable < 3 or not _package_shape(candidate, expected_extension):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidate, destination)
        return candidate, destination.stat().st_size, stable
    raise APKMirrorBrowserError(
        f"APKMirror browser download did not stabilize within {timeout:g}s"
    )


def _check_page_url(page_url: str) -> None:
    parsed = urlparse(page_url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith("apkmirror.com"):
        raise APKMirrorBrowserError(f"refusing non-APKMirror browser URL: {page_url}")


def download_with_playwright_firefox(
    page_url: str,
    destination: str | Path,
    *,
    expected_extension: str = ".apk",
    clearance_timeout: float = 180.0,
    clearance_factory: Callable[..., Any] | None = None,
    firefox_bin: str | None = None,
    headless: bool = False,
    profile_dir: str | Path | None = None,
    initial_wait: float = 5.0,
    page_timeout: float = 180.0,
    download_timeout: float = 700.0,
    poll_interval: float = 4.0,
    challenge_timeout: float | None = None,
    max_challenge_solves: int = 2,
    close_vignette: bool = True,
    playwright_factory: Callable[[], Any] | None = None,
    context_factory: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Download APKMirror through Playwright using a stock-Firefox cookie.

    Playwright's engines cannot clear APKMirror's challenge here, but the
    distribution Firefox does.  The warmer runs first; its cookie and user
    agent are injected into a Playwright Firefox context, which then performs
    the normal click and download.  If the browser cannot turn the click into
    a file, the same cookie is reused for a plain HTTP fetch.
    """
    _check_page_url(page_url)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)

    if clearance_factory is None:
        try:
            from .firefox_clearance import warm_clearance, virtual_display
        except ImportError:  # pragma: no cover - direct script use
            from firefox_clearance import warm_clearance, virtual_display  # type: ignore[no-redef]
        clearance_factory = warm_clearance
    else:
        try:
            from .firefox_clearance import virtual_display
        except ImportError:  # pragma: no cover
            from firefox_clearance import virtual_display  # type: ignore[no-redef]

    temporary_profile: tempfile.TemporaryDirectory[str] | None = None
    if profile_dir is None:
        temporary_profile = tempfile.TemporaryDirectory(
            prefix="apkmirror-playwright-"
        )
        profile_path = Path(temporary_profile.name)
    else:
        profile_path = Path(profile_dir)
        profile_path.mkdir(parents=True, exist_ok=True)

    def warm() -> Any:
        return clearance_factory(
            page_url,
            timeout=clearance_timeout,
            firefox_bin=firefox_bin,
            log=log,
            sleep=sleep,
        )

    def run_browser(clearance: Any, download_path: Path) -> dict[str, Any]:
        if playwright_factory is None:
            from playwright.sync_api import sync_playwright

            factory = sync_playwright
        else:
            factory = playwright_factory
        with factory() as playwright:
            if context_factory is not None:
                context = context_factory(
                    playwright, profile_path, download_path, clearance
                )
            else:
                context = playwright.firefox.launch_persistent_context(
                    str(profile_path),
                    headless=headless,
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                    user_agent=clearance.user_agent,
                    accept_downloads=True,
                    downloads_path=str(download_path),
                    firefox_user_prefs={
                        "browser.download.folderList": 2,
                        "browser.download.dir": str(download_path),
                        "browser.download.useDownloadDir": True,
                        "browser.helperApps.neverAsk.saveToDisk": (
                            "application/vnd.android.package-archive,"
                            "application/octet-stream"
                        ),
                    },
                )
            try:
                context.add_cookies([clearance.cookie()])
                install_shadow_root_unlock(context, log=log)
                page = _active_page(context)
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                log(f"[APKMirror browser] opening {page_url}")
                try:
                    page.goto(
                        page_url,
                        wait_until="commit",
                        timeout=min(int(page_timeout * 1000), 30_000),
                    )
                except Exception as exc:
                    # A warm cookie commits almost immediately.  A timeout here
                    # means Cloudflare is holding the response; inspect the
                    # challenge state first and let the HTTP fallback take over
                    # if the page never arrives.
                    log(
                        "[APKMirror browser] navigation did not commit "
                        f"({type(exc).__name__}); checking the page state"
                    )
                if initial_wait > 0:
                    sleep(initial_wait)

                challenge_budget = (
                    page_timeout if challenge_timeout is None else challenge_timeout
                )
                deadline = time.monotonic() + max(page_timeout, 30.0)
                href = None
                challenge_solves = 0
                while time.monotonic() < deadline:
                    page = _active_page(context)
                    if close_vignette:
                        dismiss_google_vignette(page, log=log)
                    href = find_download_href(page)
                    if href:
                        break
                    if (
                        challenge_state(page) == "challenge"
                        and challenge_solves < max_challenge_solves
                    ):
                        challenge_solves += 1
                        remaining = max(1.0, deadline - time.monotonic())
                        log(
                            "[APKMirror browser] Cloudflare challenge survived the "
                            f"warmed cookie; solving (attempt {challenge_solves}/"
                            f"{max_challenge_solves})"
                        )
                        outcome = solve_turnstile_challenge(
                            page,
                            timeout=min(remaining, challenge_budget),
                            poll_interval=min(1.0, poll_interval),
                            sleep=sleep,
                            log=log,
                        )
                        log(f"[APKMirror browser] challenge result: {outcome.summary()}")
                        if outcome.cleared:
                            continue
                    sleep(min(5.0, poll_interval))
                if not href:
                    detail = describe_page(page)
                    raise APKMirrorBrowserError(
                        "APKMirror download button was not found in the "
                        f"Playwright page (state={detail})"
                    )
                if close_vignette:
                    dismiss_google_vignette(page, log=log)
                log(f"[APKMirror browser] clicking download once: {href.split('?', 1)[0]}")
                _click_download_once(page, href)
                start_timeout = min(download_timeout, 90.0)
                started = _wait_for_download_start(
                    download_path,
                    timeout=start_timeout,
                    poll_interval=poll_interval,
                    sleep=sleep,
                    log=log,
                )
                if started is None:
                    raise APKMirrorBrowserError(
                        "APKMirror browser did not start a file download "
                        f"within {start_timeout:g}s"
                    )
                source, size, _ = _wait_for_download(
                    download_path,
                    destination,
                    expected_extension=expected_extension,
                    timeout=download_timeout,
                    poll_interval=poll_interval,
                    sleep=sleep,
                    log=log,
                )
                return {
                    "path": str(destination),
                    "source_path": str(source),
                    "bytes": size,
                    "vignette_dismissed": True,
                    "headless": headless,
                    "seed": None,
                    "transport": "playwright-firefox",
                }
            finally:
                try:
                    context.close()
                except Exception:
                    pass

    try:
        with virtual_display(log=log) as display:
            log(f"[APKMirror] virtual display {display}")
            clearance = warm()
            log(
                f"[APKMirror] clearance cookie from Firefox "
                f"{clearance.firefox_version}; starting Playwright"
            )
            with tempfile.TemporaryDirectory(prefix="apkmirror-download-") as directory:
                download_path = Path(directory)
                try:
                    return run_browser(clearance, download_path)
                except Exception as exc:
                    log(
                        "[APKMirror] Playwright download failed "
                        f"({type(exc).__name__}: {exc}); falling back to HTTP"
                    )
            try:
                return _download_via_http(
                    clearance,
                    page_url,
                    destination,
                    expected_extension=expected_extension,
                    download_timeout=download_timeout,
                    log=log,
                )
            except _ClearanceExpired as exc:
                log(f"[APKMirror] warmed cookie was rejected ({exc}); refreshing")
                clearance = warm()
                return _download_via_http(
                    clearance,
                    page_url,
                    destination,
                    expected_extension=expected_extension,
                    download_timeout=download_timeout,
                    log=log,
                )
    except APKMirrorBrowserError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise APKMirrorBrowserError(
            f"Firefox/Playwright APKMirror flow failed: {exc}"
        ) from exc
    finally:
        if temporary_profile is not None:
            temporary_profile.cleanup()


def download_with_invisible_playwright(
    page_url: str,
    destination: str | Path,
    *,
    expected_extension: str = ".apk",
    headless: bool = False,
    profile_dir: str | Path | None = None,
    seed: int | None = None,
    humanize: bool = True,
    initial_wait: float = 45.0,
    page_timeout: float = 60.0,
    download_timeout: float = 420.0,
    poll_interval: float = 4.0,
    challenge_timeout: float | None = None,
    max_challenge_solves: int = 2,
    close_vignette: bool = True,
    browser_factory: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Download one APKMirror variant page into ``destination``.

    ``browser_factory`` is injectable for unit tests.  Production callers
    leave it unset so ``invisible_playwright`` is imported lazily.
    """
    parsed = urlparse(page_url)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith("apkmirror.com"):
        raise APKMirrorBrowserError(f"refusing non-APKMirror browser URL: {page_url}")

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)

    if browser_factory is None:
        try:
            from invisible_playwright import InvisiblePlaywright
        except ImportError as exc:
            raise APKMirrorBrowserError(
                "invisible-playwright is not installed; install the pinned "
                "dependency and run `python -m invisible_playwright fetch`"
            ) from exc
        browser_factory = InvisiblePlaywright

    temporary_profile: tempfile.TemporaryDirectory[str] | None = None
    if profile_dir is None:
        temporary_profile = tempfile.TemporaryDirectory(prefix="apkmirror-profile-")
        profile_path = Path(temporary_profile.name)
    else:
        profile_path = Path(profile_dir)
        profile_path.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix="apkmirror-download-") as download_dir:
            download_path = Path(download_dir)
            browser = browser_factory(
                seed=seed,
                headless=headless,
                humanize=humanize,
                profile_dir=profile_path,
                extra_prefs={
                    "browser.download.folderList": 2,
                    "browser.download.dir": str(download_path),
                    "browser.download.useDownloadDir": True,
                },
            )
            vignette_closed = False
            with browser as context:
                unlocked = install_shadow_root_unlock(context, log=log)
                if unlocked:
                    log("[APKMirror browser] installed closed-shadow-root unlock")
                page = context.new_page()
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                log(f"[APKMirror browser] opening {page_url}")
                page.goto(
                    page_url,
                    wait_until="domcontentloaded",
                    timeout=int(page_timeout * 1000),
                )
                if initial_wait > 0:
                    sleep(initial_wait)

                challenge_budget = (
                    page_timeout if challenge_timeout is None else challenge_timeout
                )
                deadline = time.monotonic() + max(page_timeout, 30.0)
                href = None
                challenge_solves = 0
                while time.monotonic() < deadline:
                    page = _active_page(context)
                    if close_vignette and not vignette_closed:
                        vignette_closed = dismiss_google_vignette(page, log=log)
                    href = find_download_href(page)
                    if href:
                        break
                    if (
                        challenge_state(page) == "challenge"
                        and challenge_solves < max_challenge_solves
                    ):
                        challenge_solves += 1
                        remaining = max(1.0, deadline - time.monotonic())
                        log(
                            "[APKMirror browser] Cloudflare challenge detected; "
                            f"solving (attempt {challenge_solves}/{max_challenge_solves})"
                        )
                        outcome = solve_turnstile_challenge(
                            page,
                            timeout=min(remaining, challenge_budget),
                            poll_interval=min(1.0, poll_interval),
                            seed=seed,
                            sleep=sleep,
                            log=log,
                        )
                        log(f"[APKMirror browser] challenge result: {outcome.summary()}")
                        if outcome.cleared:
                            continue
                        # A failed solve still leaves a readable page; fall
                        # through so the state is reported if time runs out.
                    sleep(min(5.0, poll_interval))
                if not href:
                    detail = describe_page(page)
                    log(f"[APKMirror browser] page state on failure: {detail}")
                    raise APKMirrorBrowserError(
                        "APKMirror download button was not found in the "
                        f"browser page (state={detail})"
                    )

                if close_vignette and not vignette_closed:
                    vignette_closed = dismiss_google_vignette(page, log=log)
                log(f"[APKMirror browser] clicking download once: {href.split('?', 1)[0]}")
                _click_download_once(page, href)

                # Do not call page APIs after the click.  The patched Firefox
                # can dispose the Playwright session while the file downloads.
                source, size, _ = _wait_for_download(
                    download_path,
                    destination,
                    expected_extension=expected_extension,
                    timeout=download_timeout,
                    poll_interval=poll_interval,
                    sleep=sleep,
                    log=log,
                )
            return {
                "path": str(destination),
                "source_path": str(source),
                "bytes": size,
                "vignette_dismissed": vignette_closed,
                "headless": headless,
                "seed": seed,
            }
    except APKMirrorBrowserError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise APKMirrorBrowserError(f"invisible_playwright APKMirror flow failed: {exc}") from exc
    finally:
        if temporary_profile is not None:
            temporary_profile.cleanup()
