import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools.apkmirror_browser import (
    APKMirrorBrowserError,
    _download_files,
    dismiss_google_vignette,
    download_with_invisible_playwright,
)
from tools.mirror_download import (
    _apkmirror_browser_requested,
    _browser_allowed_in_ci,
    _render_browser_page_url,
    download_from_mirror,
)


class _FakeButton:
    def __init__(self, href, on_click=None, visible=True):
        self.href = href
        self.on_click = on_click
        self.visible = visible
        self.clicks = 0
        self.scrolls = 0

    def count(self):
        return 1

    def nth(self, index):
        if index != 0:
            raise IndexError(index)
        return self

    def is_visible(self):
        return self.visible

    def scroll_into_view_if_needed(self, **kwargs):
        self.scrolls += 1

    def get_attribute(self, name):
        return self.href if name == "href" else None

    def click(self, **kwargs):
        del kwargs
        self.clicks += 1
        if self.on_click:
            self.on_click()


class _FakeLocator:
    def __init__(self, button):
        self.button = button

    def count(self):
        return 1

    def nth(self, index):
        return self.button.nth(index)


class _FakePage:
    def __init__(self, button, url="https://www.apkmirror.com/variant/", close_button=None):
        self.button = button
        self.close_button = close_button
        self.url = url
        self.frames = [self]
        self.goto_calls = []

    def locator(self, selector):
        if selector == "#dismiss-button-element":
            return _FakeLocator(self.close_button) if self.close_button else _FakeLocator(_FakeButton("", visible=False))
        return _FakeLocator(self.button)

    def new_page(self):
        return self

    def is_closed(self):
        return False

    def bring_to_front(self):
        pass

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))


class _FakeContext:
    def __init__(self, page):
        self.page = page
        self.pages = [page]
        self.scripts = []

    def new_page(self):
        return self.page

    def add_init_script(self, script):
        self.scripts.append(script)


class _FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.context = _FakeContext(page)

    def __enter__(self):
        return self.context

    def __exit__(self, *args):
        return False


class APKMirrorBrowserTests(unittest.TestCase):
    def test_dismisses_vignette_in_child_frame_once(self):
        close_clicks = []
        close_button = _FakeButton("", on_click=lambda: close_clicks.append(True))
        child = type("Frame", (), {
            "locator": lambda self, selector: _FakeLocator(close_button),
        })()
        page = _FakePage(_FakeButton("/download/"))
        page.frames = [page, child]
        self.assertTrue(dismiss_google_vignette(page, log=lambda message: None))
        self.assertEqual(len(close_clicks), 1)

    def test_browser_flow_clicks_once_and_promotes_part_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "base.apk"
            source_holder = {}

            def write_package(download_dir):
                source = Path(download_dir) / "package.apk.part"
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr("AndroidManifest.xml", b"manifest")
                    archive.writestr("classes.dex", b"x" * 2_000_000)
                source_holder["path"] = source

            button = _FakeButton(
                "https://www.apkmirror.com/apk/pinterest/variant-2-android-apk-download/download/",
                on_click=lambda: write_package(source_holder["dir"]),
            )
            page = _FakePage(button)
            browser = _FakeBrowser(page)

            def browser_factory(**kwargs):
                self.assertEqual(kwargs["headless"], False)
                self.assertEqual(kwargs["humanize"], True)
                source_holder["dir"] = kwargs["extra_prefs"]["browser.download.dir"]
                return browser

            result = download_with_invisible_playwright(
                "https://www.apkmirror.com/apk/pinterest/variant-2-android-apk-download/",
                destination,
                expected_extension=".apk",
                browser_factory=browser_factory,
                initial_wait=0,
                page_timeout=1,
                download_timeout=0.2,
                poll_interval=0.005,
                sleep=lambda seconds: __import__("time").sleep(seconds),
                log=lambda message: None,
            )
            self.assertEqual(button.clicks, 1)
            self.assertTrue(destination.is_file())
            self.assertEqual(result["bytes"], destination.stat().st_size)
            self.assertEqual(result["vignette_dismissed"], False)
            self.assertEqual(len(browser.context.scripts), 1)
            self.assertIn("attachShadow", browser.context.scripts[0])

    def test_browser_flow_rejects_non_apkmirror_url(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(APKMirrorBrowserError):
                download_with_invisible_playwright(
                    "https://example.test/not-apkmirror",
                    Path(directory) / "out.apk",
                    browser_factory=lambda **kwargs: None,
                )

    def test_selector_matches_real_download_button(self):
        from bs4 import BeautifulSoup

        html = (
            '<a rel="nofollow" class="accent_bg btn btn-flat downloadButton yxW" '
            'href="/apk/pinterest/pinterest-one-destination-for-a-world-of-inspiration/'
            "pinterest-14-34-0-release/pinterest-14-34-0-2-android-apk-download/"
            'download/?key=94fb85af6f67d79836d563a7d85b118ad5c6f7da&amp;forcebaseapk=true">'
            "Download APK</a>"
        )
        soup = BeautifulSoup(html, "html.parser")
        matches = soup.select("a.downloadButton[href*='/download/']")
        self.assertEqual(len(matches), 1)
        self.assertIn("forcebaseapk=true", matches[0]["href"])

    def test_url_template_replaces_version(self):
        mirror = {
            "browser_page_url": (
                "https://www.apkmirror.com/apk/o/r/app-{version_dashes}-"
                "2-android-apk-download/"
            )
        }
        self.assertEqual(
            _render_browser_page_url(mirror, "14.34.0"),
            "https://www.apkmirror.com/apk/o/r/app-14-34-0-2-android-apk-download/",
        )

    def test_browser_config_is_explicit_and_ci_is_opt_in(self):
        self.assertTrue(_apkmirror_browser_requested({"browser": "invisible_playwright"}))
        self.assertFalse(_apkmirror_browser_requested({"browser": False}))
        with patch.dict(os.environ, {"CI": "true"}, clear=False):
            self.assertFalse(_browser_allowed_in_ci())
        with patch.dict(os.environ, {"CI": "true", "APKMIRROR_BROWSER_ALLOW_CI": "1"}, clear=False):
            self.assertTrue(_browser_allowed_in_ci())

    def test_mirror_download_routes_apkmirror_to_browser_without_apkd(self):
        cfg = {
            "id": "pinterest",
            "package": "com.pinterest",
            "source": {
                "type": "mirrors",
                "file_type": "apk",
                "mirrors": [{
                    "type": "apkmirror",
                    "file_type": "apk",
                    "browser": "invisible_playwright",
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


    def test_download_scan_accepts_playwright_guid_files_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guid = root / "eee2d365-b3b8-41f7-b524-be6bf272558e"
            guid.write_bytes(b"x" * 2_000_000)
            (root / "notes.zip").write_bytes(b"x" * 2_000_000)
            (root / "small.apk").write_bytes(b"x")
            files = _download_files(root)
        self.assertEqual([path.name for path in files], [guid.name])


if __name__ == "__main__":
    unittest.main()
