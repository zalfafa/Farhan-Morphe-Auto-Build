"""Warm a Cloudflare clearance cookie with a stock Firefox build.

The Playwright engines available to this project are fingerprinted by
Cloudflare: they either never render the Turnstile widget or keep looping on
it, while the distribution Firefox package clears the same challenge on the
same address without interaction.  This module runs that browser under Xvfb
with a throwaway profile, waits for the challenge page to actually clear, and
hands the cookie plus the matching user agent to the Playwright transport.

The browser is only used to obtain the cookie.  Every request that follows
still goes through the normal APKMirror transport and every downloaded byte
is still validated by the caller.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

CLEARANCE_COOKIE = "cf_clearance"
COOKIE_DB = "cookies.sqlite"
FIREFOX_HOSTS = (".apkmirror.com", "apkmirror.com", "www.apkmirror.com")

USER_JS = {
    "app.update.auto": False,
    "app.update.enabled": False,
    "browser.aboutwelcome.enabled": False,
    "browser.newtabpage.enabled": False,
    "browser.shell.checkDefaultBrowser": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "browser.startup.page": 0,
    "browser.tabs.warnOnClose": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "dom.disable_beforeunload": False,
    "intl.locale.requested": "en-US",
    "network.captive-portal-service.enabled": False,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
}


class FirefoxUnavailable(RuntimeError):
    """Raised when no usable stock Firefox installation can be found."""


class ClearanceError(RuntimeError):
    """Raised when Firefox cannot produce a Cloudflare clearance cookie."""


@dataclass(frozen=True)
class CloudflareClearance:
    """A cookie issued by Cloudflare and the UA it was issued for."""

    value: str
    host: str
    user_agent: str
    firefox_binary: str
    firefox_version: str
    profile_dir: str

    def cookie(self) -> dict[str, object]:
        """Return the cookie in the shape Playwright's ``add_cookies`` wants."""
        return {
            "name": CLEARANCE_COOKIE,
            "value": self.value,
            "domain": self.host,
            "path": "/",
            "secure": True,
            "httpOnly": True,
        }


def find_firefox_binary(explicit: str | None = None) -> str:
    """Locate a real Firefox executable, preferring an explicit override."""
    candidates = [
        explicit,
        os.getenv("APKMIRROR_FIREFOX_BIN"),
        shutil.which("firefox"),
        "/usr/bin/firefox",
        "/usr/lib/firefox/firefox",
        "/opt/firefox/firefox",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise FirefoxUnavailable(
        "stock Firefox was not found; install firefox or set "
        "APKMIRROR_FIREFOX_BIN to the executable"
    )


def firefox_version(binary: str) -> tuple[str, str]:
    """Return ``(version, user_agent)`` for a stock Firefox executable."""
    try:
        completed = subprocess.run(
            [binary, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FirefoxUnavailable(f"could not run {binary} --version: {exc}") from exc
    output = f"{completed.stdout}\n{completed.stderr}"
    match = re.search(r"Firefox\s+(\d+(?:\.\d+)*)", output)
    if not match:
        raise FirefoxUnavailable(f"could not parse the Firefox version from {output!r}")
    version = match.group(1)
    major = version.split(".", 1)[0]
    machine = platform.machine().lower() or "x86_64"
    architecture = "aarch64" if machine in {"aarch64", "arm64"} else "x86_64"
    user_agent = (
        f"Mozilla/5.0 (X11; Linux {architecture}; rv:{major}.0) "
        f"Gecko/20100101 Firefox/{major}.0"
    )
    return version, user_agent


def write_profile(profile: Path) -> None:
    """Create a minimal profile that skips every first-run prompt."""
    profile.mkdir(parents=True, exist_ok=True)
    lines = [
        f"user_pref({json.dumps(key)}, {json.dumps(value)});"
        for key, value in USER_JS.items()
    ]
    (profile / "user.js").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _copy_cookie_database(profile: Path, destination: Path) -> Path | None:
    source = profile / COOKIE_DB
    if not source.is_file():
        return None
    for name in (COOKIE_DB, f"{COOKIE_DB}-wal", f"{COOKIE_DB}-shm"):
        candidate = profile / name
        if candidate.is_file():
            try:
                shutil.copy2(candidate, destination / name)
            except OSError:
                pass
    return destination / COOKIE_DB


def read_clearance(
    profile: Path,
    *,
    since_us: int = 0,
    now_ms: int | None = None,
) -> dict[str, object] | None:
    """Read the newest unexpired APKMirror clearance cookie from a profile."""
    with tempfile.TemporaryDirectory(prefix="apkmirror-cookies-") as directory:
        database = _copy_cookie_database(profile, Path(directory))
        if database is None:
            return None
        rows: list[tuple[object, ...]] = []
        try:
            connection = sqlite3.connect(
                f"file:{database}?mode=ro", uri=True, timeout=2
            )
            try:
                rows = connection.execute(
                    "SELECT name, value, host, path, expiry, creationTime "
                    "FROM moz_cookies WHERE name = ?",
                    (CLEARANCE_COOKIE,),
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return None
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    best: dict[str, object] | None = None
    for name, value, host, path, expiry, creation in rows:
        if not value or host not in FIREFOX_HOSTS:
            continue
        if expiry and int(expiry) <= current_ms:
            continue
        created = int(creation or 0)
        if since_us and created < since_us:
            continue
        if best is None or int(best.get("creation", 0)) < created:
            best = {
                "name": str(name),
                "value": str(value),
                "host": str(host),
                "path": str(path or "/"),
                "creation": created,
            }
    return best


CLEARANCE_TITLE_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "performing security verification",
    "verify you are human",
    "loading http://",
    "loading https://",
)


def _xlib():
    try:
        from Xlib import X, display as xdisplay
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise FirefoxUnavailable(
            "python-xlib is required to confirm that the Cloudflare page "
            "actually cleared; install it with pip"
        ) from exc
    return X, xdisplay


def window_titles(display: str | None = None, *, pid: int | None = None) -> list[str]:
    """Return the titles of the X windows owned by ``pid``.

    Firefox keeps ``_NET_WM_NAME`` in sync with the active tab, which is the
    cheapest way to distinguish "Just a moment..." from the real APKMirror
    page without touching the browser or the network.
    """
    X, xdisplay = _xlib()
    try:
        connection = xdisplay.Display(display)
    except Exception as exc:
        raise FirefoxUnavailable(
            f"cannot read window titles from X display {display!r}: {exc}"
        ) from exc
    try:
        name_atom = connection.intern_atom("_NET_WM_NAME")
        pid_atom = connection.intern_atom("_NET_WM_PID")
        titles: list[str] = []

        def walk(window, owned: bool, depth: int) -> None:
            try:
                prop = window.get_full_property(pid_atom, X.AnyPropertyType)
            except Exception:
                prop = None
            if prop is not None:
                try:
                    owned = pid is None or int(prop.value[0]) == int(pid)
                except (IndexError, TypeError, ValueError):
                    owned = pid is None
            elif depth == 1 and pid is not None:
                # A top-level window without _NET_WM_PID cannot be attributed
                # to our Firefox process, so leave it and its children alone.
                owned = False
            if not owned:
                return
            try:
                prop = window.get_full_property(name_atom, X.AnyPropertyType)
            except Exception:
                prop = None
            if prop is not None:
                value = prop.value
                if isinstance(value, bytes):
                    value = value.decode("utf-8", "replace")
                value = str(value).strip()
                if value and value not in titles:
                    titles.append(value)
            try:
                children = window.query_tree().children
            except Exception:
                children = []
            for child in children:
                walk(child, owned, depth + 1)

        walk(connection.screen().root, True, 0)
        return titles
    finally:
        try:
            connection.close()
        except Exception:
            pass


def title_state(titles: list[str], *, expected: str = "apkmirror") -> str:
    """Classify window titles as ``cleared``, ``challenged`` or ``unknown``."""
    expected = expected.lower()
    for title in titles:
        lowered = title.lower()
        if any(marker in lowered for marker in CLEARANCE_TITLE_MARKERS):
            return "challenged"
        if expected in lowered:
            return "cleared"
    return "unknown"


def _terminate(process: subprocess.Popen[bytes], timeout: float = 10.0) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except OSError:
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


@contextmanager
def virtual_display(
    *, log: Callable[[str], None] = print
) -> Iterator[str]:
    """Use the current X display, or start a private Xvfb server."""
    existing = os.getenv("DISPLAY")
    if existing:
        yield existing
        return
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        raise FirefoxUnavailable(
            "no DISPLAY is set and Xvfb is not installed; the stock Firefox "
            "clearance path needs a real X server"
        )
    for number in range(90, 130):
        socket = Path(f"/tmp/.X11-unix/X{number}")
        if socket.exists():
            continue
        process = subprocess.Popen(
            [xvfb, f":{number}", "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if socket.exists():
                break
            if process.poll() is not None:
                break
            time.sleep(0.1)
        if socket.exists():
            previous = os.environ.get("DISPLAY")
            os.environ["DISPLAY"] = f":{number}"
            log(f"[APKMirror clearance] Xvfb display :{number}")
            try:
                yield f":{number}"
            finally:
                if previous is None:
                    os.environ.pop("DISPLAY", None)
                else:
                    os.environ["DISPLAY"] = previous
                _terminate(process)
            return
        _terminate(process)
    raise FirefoxUnavailable("Xvfb could not allocate a display")


def warm_clearance(
    url: str,
    *,
    timeout: float = 180.0,
    firefox_bin: str | None = None,
    profile_dir: str | Path | None = None,
    titles: Callable[[], list[str]] | None = None,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> CloudflareClearance:
    """Open ``url`` in stock Firefox until Cloudflare clears the challenge.

    A ``cf_clearance`` row by itself is not enough: Cloudflare can write the
    cookie while the tab still shows "Just a moment...".  The warmer therefore
    waits for the browser window title to become the real APKMirror page and
    only then returns the cookie.
    """
    binary = find_firefox_binary(firefox_bin)
    version, user_agent = firefox_version(binary)
    log(
        f"[APKMirror clearance] using {binary} (Firefox {version}, "
        f"UA {user_agent})"
    )

    temporary = None
    if profile_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="apkmirror-firefox-")
        profile = Path(temporary.name)
    else:
        profile = Path(profile_dir)
    write_profile(profile)
    started_us = int(time.time() * 1_000_000)

    try:
        with virtual_display(log=log) as display:
            env = os.environ.copy()
            env.update(
                {
                    "DISPLAY": display,
                    "LIBGL_ALWAYS_SOFTWARE": "1",
                    "MOZ_CRASHREPORTER_DISABLE": "1",
                    "MOZ_ENABLE_WAYLAND": "0",
                }
            )
            command = [
                binary,
                "--no-remote",
                "--profile",
                str(profile),
                "--new-window",
                url,
            ]
            log(f"[APKMirror clearance] opening {url}")
            process = subprocess.Popen(
                command,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + timeout
                title_probe = titles
                if title_probe is None:
                    title_probe = lambda: window_titles(display, pid=process.pid)
                cookie: dict[str, object] | None = None
                cookie_seen = False
                state = "unknown"
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise ClearanceError(
                            f"Firefox exited with code {process.returncode} "
                            "before Cloudflare cleared the challenge"
                        )
                    current = title_state(title_probe())
                    if current != state:
                        log(f"[APKMirror clearance] page state: {current}")
                        state = current
                    cookie = read_clearance(profile, since_us=started_us)
                    if cookie and state == "cleared":
                        log(
                            "[APKMirror clearance] Cloudflare challenge cleared "
                            f"for {cookie['host']}"
                        )
                        return CloudflareClearance(
                            value=str(cookie["value"]),
                            host=str(cookie["host"]),
                            user_agent=user_agent,
                            firefox_binary=binary,
                            firefox_version=version,
                            profile_dir=str(profile),
                        )
                    if cookie and not cookie_seen:
                        cookie_seen = True
                        log(
                            "[APKMirror clearance] cookie issued before the page "
                            "cleared; waiting for confirmation"
                        )
                    sleep(1.0)
                if cookie:
                    raise ClearanceError(
                        "Cloudflare issued a clearance cookie but the page never "
                        f"left the challenge (last state: {state})"
                    )
                raise ClearanceError(
                    f"Cloudflare did not clear the challenge within {timeout:g}s"
                )
            finally:
                _terminate(process)
    finally:
        if temporary is not None:
            temporary.cleanup()
