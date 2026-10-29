import io
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tools.apkmirror_browser import (
    APKMirrorBrowserError,
    _ClearanceExpired,
    _download_via_http,
    download_with_playwright_firefox,
)
from tools.firefox_clearance import (
    ClearanceError,
    CloudflareClearance,
    find_firefox_binary,
    firefox_version,
    read_clearance,
    title_state,
    warm_clearance,
    write_profile,
)
from tools.mirror_download import _apkmirror_browser_requested, download_from_mirror


class _FakeClearance:
    value = "cookie-value"
    host = ".apkmirror.com"
    user_agent = "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/20100101 Firefox/158.0"
    firefox_version = "158.0.1"

    def cookie(self):
        return {
            "name": "cf_clearance",
            "value": self.value,
            "domain": self.host,
            "path": "/",
            "secure": True,
            "httpOnly": True,
        }


class FirefoxClearanceTests(unittest.TestCase):
    def test_find_firefox_binary_prefers_explicit_path(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "firefox"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            self.assertEqual(find_firefox_binary(str(binary)), str(binary))

    def test_firefox_version_builds_a_matching_user_agent(self):
        completed = subprocess.CompletedProcess(
            ["firefox", "--version"], 0, "Mozilla Firefox 158.0.1\n", ""
        )
        with patch("tools.firefox_clearance.subprocess.run", return_value=completed):
            version, user_agent = firefox_version("/usr/bin/firefox")
        self.assertEqual(version, "158.0.1")
        self.assertIn("rv:158.0", user_agent)
        self.assertIn("Firefox/158.0", user_agent)

    def test_read_clearance_ignores_expired_and_foreign_cookies(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            connection = sqlite3.connect(profile / "cookies.sqlite")
            connection.execute(
                "CREATE TABLE moz_cookies ("
                "name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER, "
                "creationTime INTEGER)"
            )
            connection.executemany(
                "INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("cf_clearance", "expired", ".apkmirror.com", "/", 1000, 10),
                    ("cf_clearance", "foreign", ".example.com", "/", 9999999999999, 20),
                    ("cf_clearance", "valid", ".apkmirror.com", "/", 9999999999999, 30),
                ],
            )
            connection.commit()
            connection.close()
            cookie = read_clearance(profile, now_ms=2000)
        self.assertIsNotNone(cookie)
        self.assertEqual(cookie["value"], "valid")

    def test_read_clearance_honours_the_since_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            connection = sqlite3.connect(profile / "cookies.sqlite")
            connection.execute(
                "CREATE TABLE moz_cookies ("
                "name TEXT, value TEXT, host TEXT, path TEXT, expiry INTEGER, "
                "creationTime INTEGER)"
            )
            connection.execute(
                "INSERT INTO moz_cookies VALUES (?, ?, ?, ?, ?, ?)",
                ("cf_clearance", "old", ".apkmirror.com", "/", 9999999999999, 10),
            )
            connection.commit()
            connection.close()
            self.assertIsNone(read_clearance(profile, since_us=20, now_ms=2000))

    def test_write_profile_creates_readable_user_js(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile"
            write_profile(profile)
            text = (profile / "user.js").read_text(encoding="utf-8")
        self.assertIn("browser.aboutwelcome.enabled", text)
        self.assertIn("false", text)
        self.assertTrue(text.endswith("\n"))

    def test_warm_clearance_returns_the_cookie_and_stops_firefox(self):
        cookie = {
            "name": "cf_clearance",
            "value": "warm",
            "host": ".apkmirror.com",
            "path": "/",
            "creation": 123,
        }

        class FakeProcess:
            pid = 4242

            def __init__(self):
                self.terminated = False

            def poll(self):
                return None

        process = FakeProcess()

        @contextmanager
        def fake_display(**_kwargs):
            yield ":99"

        with tempfile.TemporaryDirectory() as directory, \
             patch("tools.firefox_clearance.find_firefox_binary", return_value="/usr/bin/firefox"), \
             patch("tools.firefox_clearance.firefox_version", return_value=("158.0.1", "UA")), \
             patch("tools.firefox_clearance.virtual_display", fake_display), \
             patch("tools.firefox_clearance.subprocess.Popen", return_value=process) as popen, \
             patch("tools.firefox_clearance.read_clearance", side_effect=[None, cookie]), \
             patch("tools.firefox_clearance._terminate") as terminate:
            clearance = warm_clearance(
                "https://www.apkmirror.com/variant/",
                timeout=5,
                profile_dir=Path(directory) / "profile",
                titles=lambda: [
                    "Pinterest 14.39.0 (nodpi) (Android 10+) APK Download "
                    "by Pinterest - APKMirror — Mozilla Firefox"
                ],
                sleep=lambda _seconds: None,
                log=lambda _message: None,
            )
            user_js = Path(directory) / "profile" / "user.js"
            self.assertTrue(user_js.is_file())

        self.assertEqual(clearance.value, "warm")
        self.assertEqual(clearance.user_agent, "UA")
        self.assertIn("--no-remote", popen.call_args.args[0])
        terminate.assert_called_once_with(process)

    def test_warm_clearance_raises_and_cleans_up_on_timeout(self):
        class FakeProcess:
            pid = 4242
            returncode = None

            def poll(self):
                return None

        process = FakeProcess()

        @contextmanager
        def fake_display(**_kwargs):
            yield ":99"

        with patch("tools.firefox_clearance.find_firefox_binary", return_value="/usr/bin/firefox"), \
             patch("tools.firefox_clearance.firefox_version", return_value=("158.0.1", "UA")), \
             patch("tools.firefox_clearance.virtual_display", fake_display), \
             patch("tools.firefox_clearance.subprocess.Popen", return_value=process), \
             patch("tools.firefox_clearance.read_clearance", return_value=None), \
             patch("tools.firefox_clearance._terminate") as terminate:
            with self.assertRaises(ClearanceError):
                warm_clearance(
                    "https://www.apkmirror.com/variant/",
                    timeout=0,
                    titles=lambda: ["Just a moment... — Mozilla Firefox"],
                    sleep=lambda _seconds: None,
                    log=lambda _message: None,
                )
        terminate.assert_called_once_with(process)

    def test_warm_clearance_rejects_a_cookie_while_still_challenged(self):
        cookie = {
            "name": "cf_clearance",
            "value": "unverified",
            "host": ".apkmirror.com",
            "path": "/",
            "creation": 123,
        }

        class FakeProcess:
            pid = 4242
            returncode = None

            def poll(self):
                return None

        process = FakeProcess()

        @contextmanager
        def fake_display(**_kwargs):
            yield ":99"

        with patch("tools.firefox_clearance.find_firefox_binary", return_value="/usr/bin/firefox"), \
             patch("tools.firefox_clearance.firefox_version", return_value=("158.0.1", "UA")), \
             patch("tools.firefox_clearance.virtual_display", fake_display), \
             patch("tools.firefox_clearance.subprocess.Popen", return_value=process), \
             patch("tools.firefox_clearance.read_clearance", return_value=cookie), \
             patch("tools.firefox_clearance._terminate") as terminate:
            with self.assertRaises(ClearanceError):
                warm_clearance(
                    "https://www.apkmirror.com/variant/",
                    timeout=0.05,
                    titles=lambda: ["Just a moment... — Mozilla Firefox"],
                    sleep=lambda _seconds: None,
                    log=lambda _message: None,
                )
        terminate.assert_called_once_with(process)

    def test_title_state_distinguishes_challenge_and_real_page(self):
        self.assertEqual(
            title_state(["Just a moment... — Mozilla Firefox"]), "challenged"
        )
        self.assertEqual(
            title_state(
                ["Loading https://www.apkmirror.com/x — Mozilla Firefox"]
            ),
            "challenged",
        )
        self.assertEqual(
            title_state(
                ["Pinterest 14.39.0 APK Download by Pinterest - APKMirror "
                 "— Mozilla Firefox"]
            ),
            "cleared",
        )
        self.assertEqual(title_state(["Firefox"]), "unknown")

    def test_clearance_cookie_shape(self):
        clearance = CloudflareClearance(
            value="value",
            host=".apkmirror.com",
            user_agent="UA",
            firefox_binary="/usr/bin/firefox",
            firefox_version="158.0.1",
            profile_dir="/tmp/profile",
        )
        self.assertEqual(clearance.cookie()["domain"], ".apkmirror.com")
        self.assertEqual(clearance.cookie()["name"], "cf_clearance")


class _FakeCookies:
    def __init__(self):
        self.set_calls = []

    def set(self, name, value, domain):
        self.set_calls.append((name, value, domain))


class _FakeResponse:
    def __init__(self, status=200, text="", content=b""):
        self.status_code = status
        self.text = text
        self._content = content
        self.headers = {}

    def iter_content(self, _size):
        yield self._content


class _FakeStream(_FakeResponse):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeSession:
    def __init__(self, pages, content):
        self.pages = list(pages)
        self.content = content
        self.cookies = _FakeCookies()
        self.gets = []
        self.stream_headers = None
        self.closed = False

    def get(self, url, headers=None, timeout=None):
        del timeout
        self.gets.append((url, dict(headers or {})))
        return self.pages.pop(0)

    def stream(self, _method, _url, headers=None, timeout=None):
        del timeout
        self.stream_headers = dict(headers or {})
        return _FakeStream(content=self.content)

    def close(self):
        self.closed = True


def _apk_bytes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"manifest")
        archive.writestr("classes.dex", b"x" * 2_000_000)
    return buffer.getvalue()


class APKMirrorHTTPTests(unittest.TestCase):
    def test_http_flow_walks_variant_download_page_and_file(self):
        variant = (
            '<a class="downloadButton" '
            'href="/apk/o/r/v-2-android-apk-download/download/?key=abc">'
            "Download APK</a>"
        )
        download_page = (
            '<a id="download-link" '
            'href="/wp-content/themes/APKMirror/download.php?id=1&amp;key=abc">'
            "here</a>"
        )
        session = _FakeSession(
            [_FakeResponse(text=variant), _FakeResponse(text=download_page)],
            _apk_bytes(),
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "base.apk"
            result = _download_via_http(
                _FakeClearance(),
                "https://www.apkmirror.com/apk/o/r/v-2-android-apk-download/",
                destination,
                expected_extension=".apk",
                download_timeout=60,
                log=lambda _message: None,
                session_factory=lambda _clearance: session,
            )
            self.assertTrue(destination.is_file())
            self.assertEqual(result["bytes"], destination.stat().st_size)
        self.assertEqual(result["transport"], "firefox-http")
        self.assertEqual(session.cookies.set_calls[0][2], ".apkmirror.com")
        self.assertEqual(len(session.gets), 2)
        self.assertIn("Referer", session.gets[1][1])
        self.assertIn("Referer", session.stream_headers)
        self.assertTrue(session.closed)

    def test_http_flow_reports_an_expired_clearance(self):
        session = _FakeSession([_FakeResponse(status=403, text="Just a moment...")], b"")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(_ClearanceExpired):
                _download_via_http(
                    _FakeClearance(),
                    "https://www.apkmirror.com/apk/o/r/v-2-android-apk-download/",
                    Path(directory) / "base.apk",
                    expected_extension=".apk",
                    download_timeout=60,
                    log=lambda _message: None,
                    session_factory=lambda _clearance: session,
                )

    def test_http_flow_rejects_a_non_package(self):
        variant = '<a class="downloadButton" href="/download/?key=abc">x</a>'
        download_page = '<a id="download-link" href="/download.php?id=1">x</a>'
        session = _FakeSession(
            [_FakeResponse(text=variant), _FakeResponse(text=download_page)],
            b"not a zip",
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(APKMirrorBrowserError):
                _download_via_http(
                    _FakeClearance(),
                    "https://www.apkmirror.com/apk/o/r/v-2-android-apk-download/",
                    Path(directory) / "base.apk",
                    expected_extension=".apk",
                    download_timeout=60,
                    log=lambda _message: None,
                    session_factory=lambda _clearance: session,
                )


class _FakePlaywright:
    def __enter__(self):
        return object()

    def __exit__(self, *_args):
        return False


class PlaywrightFirefoxTests(unittest.TestCase):
    def test_browser_failure_falls_back_to_http_with_the_same_cookie(self):
        @contextmanager
        def fake_display(**_kwargs):
            yield ":99"

        calls = {}

        def clearance_factory(_url, **kwargs):
            calls["clearance_kwargs"] = kwargs
            return _FakeClearance()

        def context_factory(*_args, **_kwargs):
            raise RuntimeError("browser unavailable")

        expected = {"path": "base.apk", "bytes": 123, "transport": "firefox-http"}
        with tempfile.TemporaryDirectory() as directory, \
             patch("tools.firefox_clearance.virtual_display", fake_display), \
             patch("tools.apkmirror_browser._download_via_http", return_value=expected) as http:
            result = download_with_playwright_firefox(
                "https://www.apkmirror.com/apk/o/r/v-2-android-apk-download/",
                Path(directory) / "base.apk",
                clearance_factory=clearance_factory,
                playwright_factory=_FakePlaywright,
                context_factory=context_factory,
                initial_wait=0,
                page_timeout=1,
                download_timeout=1,
                sleep=lambda _seconds: None,
                log=lambda _message: None,
            )
        self.assertEqual(result, expected)
        self.assertEqual(http.call_args.args[0].value, "cookie-value")
        self.assertEqual(calls["clearance_kwargs"]["timeout"], 180.0)

    def test_non_apkmirror_url_is_rejected_before_warming(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(APKMirrorBrowserError):
                download_with_playwright_firefox(
                    "https://example.test/not-apkmirror",
                    Path(directory) / "out.apk",
                    clearance_factory=lambda *_args, **_kwargs: _FakeClearance(),
                    log=lambda _message: None,
                )

    def test_config_accepts_the_playwright_firefox_mode(self):
        self.assertTrue(
            _apkmirror_browser_requested({"browser": "playwright_firefox"})
        )

    def test_mirror_download_routes_the_firefox_mode(self):
        cfg = {
            "id": "pinterest",
            "package": "com.pinterest",
            "source": {
                "type": "mirrors",
                "file_type": "apk",
                "mirrors": [{
                    "type": "apkmirror",
                    "file_type": "apk",
                    "browser": "playwright_firefox",
                    "browser_page_url": "https://www.apkmirror.com/apk/o/r/v-{version_dashes}/",
                }],
            },
        }
        with patch("tools.mirror_download._browser_allowed_in_ci", return_value=True), \
             patch("tools.mirror_download._download_apkmirror_browser", return_value="14.34.0") as browser, \
             patch("tools.mirror_download._get_provider") as provider:
            result = download_from_mirror(
                "apkmirror", cfg, "universal", "14.34.0", "/tmp/unused.apk"
            )
        self.assertEqual(result, "14.34.0")
        browser.assert_called_once()
        provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
