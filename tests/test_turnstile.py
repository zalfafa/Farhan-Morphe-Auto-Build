import unittest
from pathlib import Path

from tools.turnstile import (
    SHADOW_ROOT_UNLOCK_JS,
    TurnstileWidget,
    challenge_state,
    click_turnstile,
    find_turnstile_widget,
    install_shadow_root_unlock,
    solve_turnstile_challenge,
)


class _FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(float(seconds), 0.01)


class _FakeMouse:
    def __init__(self, on_click=None):
        self.moves = []
        self.clicks = []
        self.on_click = on_click

    def move(self, x, y, **kwargs):
        self.moves.append((x, y))

    def click(self, x, y, **kwargs):
        self.clicks.append((x, y))
        if self.on_click:
            self.on_click()


class _FakeBody:
    def __init__(self, text=""):
        self.text = text

    def inner_text(self, **kwargs):
        return self.text


class _FakeBoxLocator:
    def __init__(self, box):
        self.box = box
        self.first = self

    def count(self):
        return 1

    def bounding_box(self):
        return self.box


class _FakeFrame:
    def __init__(self, url="https://challenges.cloudflare.com/widget", checkbox_box=None, frame_box=None):
        self.url = url
        self.checkbox_box = checkbox_box or {
            "x": 201.0,
            "y": 324.0,
            "width": 172.0,
            "height": 24.0,
        }
        self.frame_box = frame_box or {
            "x": 192.0,
            "y": 304.0,
            "width": 300.0,
            "height": 65.0,
        }

    def locator(self, selector):
        del selector
        return _FakeBoxLocator(self.checkbox_box)

    def frame_element(self):
        return _FakeBoxLocator(self.frame_box)


class _FakePage:
    def __init__(self, title="Just a moment...", url="https://www.apkmirror.com/x"):
        self._title = title
        self.url = url
        self.body = _FakeBody("Performing security verification")
        self.frames = [_FakeFrame()]
        self.mouse = _FakeMouse(on_click=self._on_click)
        self.reloads = 0
        self.clicks = 0
        self.clear_after_body_reads = 0
        self._body_reads = 0

    def title(self):
        return self._title

    def locator(self, selector):
        if selector == "body":
            self._body_reads += 1
            if self.clear_after_body_reads and self._body_reads >= self.clear_after_body_reads:
                self.clear()
            return self.body
        raise AssertionError(selector)

    def reload(self, **kwargs):
        self.reloads += 1

    def _on_click(self):
        self.clicks += 1
        if self.clear_after_body_reads:
            self._title = "Loading https://www.apkmirror.com/apk/pinterest/"
            self.url = "https://www.apkmirror.com/x?__cf_chl_rt_tk=abc"
            self.frames = []
            self.body.text = ""

    def clear(self):
        self._title = "APKMirror"
        self.url = "https://www.apkmirror.com/apk/pinterest/"
        self.body.text = "Download APK"
        self.frames = []


class _FakeContext:
    def __init__(self):
        self.scripts = []

    def add_init_script(self, script):
        self.scripts.append(script)


class TurnstileTests(unittest.TestCase):
    def test_install_shadow_unlock_is_registered(self):
        context = _FakeContext()
        self.assertTrue(install_shadow_root_unlock(context, log=lambda _: None))
        self.assertEqual(context.scripts, [SHADOW_ROOT_UNLOCK_JS])

    def test_install_shadow_unlock_tolerates_missing_context_hook(self):
        self.assertFalse(install_shadow_root_unlock(object(), log=lambda _: None))

    def test_state_classifies_challenge_transition_and_clear(self):
        challenge = _FakePage()
        self.assertEqual(challenge_state(challenge), "challenge")

        transitional = _FakePage(title="Loading https://www.apkmirror.com/x")
        transitional.url = "https://www.apkmirror.com/x?__cf_chl_rt_tk=abc"
        transitional.body.text = ""
        transitional.frames = []
        self.assertEqual(challenge_state(transitional), "transitional")

        clear = _FakePage(title="Pinterest APK", url="https://www.apkmirror.com/apk/pinterest/")
        clear.body.text = "Download APK"
        clear.frames = []
        self.assertEqual(challenge_state(clear), "clear")

    def test_finds_widget_and_clicks_left_edge_of_label(self):
        page = _FakePage()
        widget = find_turnstile_widget(page)
        self.assertIsNotNone(widget)
        self.assertTrue(click_turnstile(page, widget, sleep=lambda _: None))
        self.assertEqual(len(page.mouse.clicks), 1)
        # frame_box.x + 28, vertically centred in the frame.
        self.assertAlmostEqual(page.mouse.clicks[0][0], 220.0)
        self.assertAlmostEqual(page.mouse.clicks[0][1], 336.5)

    def test_solver_clicks_and_waits_through_loading_state(self):
        page = _FakePage()
        page.clear_after_body_reads = 4
        clock = _FakeClock()
        outcome = solve_turnstile_challenge(
            page,
            timeout=30,
            sleep=clock.sleep,
            clock=clock,
            log=lambda _: None,
        )
        self.assertTrue(outcome.cleared)
        self.assertEqual(outcome.clicks, 1)
        self.assertGreaterEqual(len(page.mouse.clicks), 1)

    def test_solver_returns_uncleared_when_no_widget_appears(self):
        page = _FakePage()
        page.frames = []
        clock = _FakeClock()
        outcome = solve_turnstile_challenge(
            page,
            timeout=5,
            max_clicks=1,
            reload_after=2,
            max_reloads=0,
            sleep=clock.sleep,
            clock=clock,
            log=lambda _: None,
        )
        self.assertFalse(outcome.cleared)
        self.assertEqual(outcome.clicks, 0)

    def test_widget_click_point_uses_small_checkbox_center(self):
        widget = TurnstileWidget(
            frame=object(),
            checkbox_box={"x": 210.0, "y": 330.0, "width": 24.0, "height": 24.0},
            frame_box={"x": 192.0, "y": 304.0, "width": 300.0, "height": 65.0},
        )
        self.assertEqual(widget.click_point(), (222.0, 342.0))


if __name__ == "__main__":
    unittest.main()
