"""Best-effort Cloudflare Turnstile handling for the APKMirror transport.

The APKMirror download page sits behind a Cloudflare interstitial.  A plain
navigation eventually renders the Turnstile widget, but it never advances on
its own: the checkbox has to be clicked, and Cloudflare re-renders the widget
while it verifies.  This module owns that interaction so the transport can
stay about APKMirror.

The flow is adapted from the Mornye solver:

* force ``attachShadow`` roots open before any page script runs, so the
  checkbox inside the widget's closed shadow root can be located;
* find the widget through its ``challenges.cloudflare.com`` frame;
* click the checkbox with a short, humanized mouse path (the left edge of the
  widget, not the centre of the label);
* re-locate and click again when the widget re-renders, up to a small budget;
* poll the page until the challenge page is gone, including the transitional
  "Loading https://..." state Cloudflare shows right before the real page.

Nothing here fabricates a token or talks to Cloudflare directly.  It drives the
same widget a person would, and the caller still validates every downloaded
byte.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable

CHALLENGE_FRAME_HOST = "challenges.cloudflare.com"
CHECKBOX_SELECTOR = 'input[type="checkbox"]'

CHALLENGE_MARKERS = (
    "just a moment",
    "performing security verification",
    "verify you are human",
    "verifying you are human",
    "checking if the site connection is secure",
    "checking your browser",
    "enable javascript and cookies",
    "attention required",
    "ray id",
)

SHADOW_ROOT_UNLOCK_JS = r"""
(() => {
  if (window.__apkmirrorShadowRootUnlocked) return 'already';
  window.__apkmirrorShadowRootUnlocked = true;
  const shadowRoots = new WeakMap();
  const originalAttachShadow = Element.prototype.attachShadow;
  Element.prototype.attachShadow = function (init) {
    const root = originalAttachShadow.call(this, { ...init, mode: 'open' });
    shadowRoots.set(this, root);
    return root;
  };
  const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'shadowRoot');
  if (descriptor && descriptor.get) {
    const originalGetter = descriptor.get;
    Object.defineProperty(Element.prototype, 'shadowRoot', {
      get: function () {
        try {
          return originalGetter.call(this) || shadowRoots.get(this) || null;
        } catch (error) {
          return shadowRoots.get(this) || null;
        }
      },
      configurable: true,
      enumerable: descriptor.enumerable,
    });
  }
  return 'installed';
})();
"""


@dataclass(frozen=True)
class TurnstileWidget:
    """Where the widget is, in top-level viewport coordinates."""

    frame: Any
    checkbox_box: dict[str, float] | None
    frame_box: dict[str, float] | None

    def click_point(self) -> tuple[float, float] | None:
        # The checkbox input is often stretched over the whole label.  Click
        # near its left edge, where the actual box is drawn, exactly like a
        # person aiming at the square.  A small input (the usual 24x24 case)
        # is clicked in its centre.
        if self.checkbox_box and self.checkbox_box.get("width", 0) < 60:
            box = self.checkbox_box
            return (
                box["x"] + box["width"] / 2,
                box["y"] + box["height"] / 2,
            )
        if self.frame_box:
            return (
                self.frame_box["x"] + 28,
                self.frame_box["y"] + self.frame_box["height"] / 2,
            )
        if self.checkbox_box:
            box = self.checkbox_box
            return (box["x"] + 18, box["y"] + box["height"] / 2)
        return None


@dataclass(frozen=True)
class TurnstileOutcome:
    cleared: bool
    clicks: int
    state: str
    reloads: int = 0

    def summary(self) -> str:
        return (
            f"cleared={self.cleared} clicks={self.clicks} "
            f"reloads={self.reloads} state={self.state}"
        )


def install_shadow_root_unlock(
    context: Any, *, log: Callable[[str], None] = print
) -> bool:
    """Register the shadow-root patch for every frame in ``context``.

    InvisiblePlaywright and Playwright both expose ``add_init_script`` on the
    context; a test double may not, so a missing hook is not fatal.
    """
    add_init_script = getattr(context, "add_init_script", None)
    if not callable(add_init_script):
        return False
    try:
        add_init_script(SHADOW_ROOT_UNLOCK_JS)
        return True
    except Exception as exc:  # pragma: no cover - browser-specific
        log(f"[APKMirror solver] could not install shadow-root patch: {exc}")
        return False


def _page_title(page: Any) -> str:
    try:
        return str(page.title() or "")
    except Exception:
        return ""


def _page_url(page: Any) -> str:
    try:
        return str(page.url or "")
    except Exception:
        return ""


def _body_text(page: Any) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=1000) or "")
    except Exception:
        return ""


def challenge_frames(page: Any) -> list[Any]:
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    result = []
    for frame in frames:
        try:
            url = str(frame.url or "")
        except Exception:
            continue
        if CHALLENGE_FRAME_HOST in url:
            result.append(frame)
    return result


def _has_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def challenge_state(page: Any) -> str:
    """Classify the page as ``challenge``, ``transitional`` or ``clear``.

    Cloudflare shows ``Loading https://...`` and keeps ``__cf_chl_rt_tk`` in
    the URL for a moment after a successful click.  Treating that as a
    challenge makes a solver click a widget that is no longer there; treating
    it as clear makes the caller race the redirect.  Hence the third state.
    """
    title = _page_title(page)
    if _has_marker(title):
        return "challenge"
    if challenge_frames(page):
        return "challenge"
    url = _page_url(page)
    if "__cf_chl_rt_tk" in url:
        body = _body_text(page)
        if _has_marker(body):
            return "challenge"
        return "transitional"
    if title.lower().startswith("loading http"):
        return "transitional"
    if _has_marker(_body_text(page)):
        return "challenge"
    return "clear"


def _bounding_box(locator: Any) -> dict[str, float] | None:
    try:
        box = locator.bounding_box()
    except Exception:
        return None
    if not box or not box.get("width") or not box.get("height"):
        return None
    return box


def find_turnstile_widget(page: Any) -> TurnstileWidget | None:
    """Locate the first Turnstile checkbox inside the challenge frames."""
    for frame in challenge_frames(page):
        try:
            checkboxes = frame.locator(CHECKBOX_SELECTOR)
            if not checkboxes.count():
                continue
        except Exception:
            continue
        checkbox_box = _bounding_box(checkboxes.first)
        frame_box = None
        try:
            frame_box = _bounding_box(frame.frame_element())
        except Exception:
            pass
        if checkbox_box or frame_box:
            return TurnstileWidget(
                frame=frame,
                checkbox_box=checkbox_box,
                frame_box=frame_box,
            )
    return None


def _human_move(
    page: Any,
    x: float,
    y: float,
    *,
    rng: random.Random,
    sleep: Callable[[float], None],
) -> None:
    """Approach the target with a short, slightly jittered mouse path."""
    start_x = x - rng.uniform(70.0, 130.0)
    start_y = y - rng.uniform(18.0, 45.0)
    try:
        page.mouse.move(start_x, start_y)
    except Exception:
        return
    sleep(rng.uniform(0.15, 0.3))
    steps = rng.randint(10, 18)
    for index in range(1, steps + 1):
        fraction = index / steps
        px = start_x + (x - start_x) * fraction + rng.uniform(-1.5, 1.5)
        py = start_y + (y - start_y) * fraction + rng.uniform(-1.5, 1.5)
        try:
            page.mouse.move(px, py)
        except Exception:
            return
        sleep(rng.uniform(0.008, 0.03))
    try:
        page.mouse.move(x, y)
    except Exception:
        return
    sleep(rng.uniform(0.08, 0.18))


def click_turnstile(
    page: Any,
    widget: TurnstileWidget,
    *,
    rng: random.Random | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    point = widget.click_point()
    if point is None:
        return False
    x, y = point
    _human_move(
        page,
        x,
        y,
        rng=rng or random.Random(),
        sleep=sleep,
    )
    try:
        page.mouse.click(x, y)
        return True
    except Exception:
        return False


def solve_turnstile_challenge(
    page: Any,
    *,
    timeout: float,
    poll_interval: float = 0.5,
    max_clicks: int = 4,
    click_interval: float = 8.0,
    reload_after: float = 30.0,
    max_reloads: int = 1,
    seed: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> TurnstileOutcome:
    """Drive the Turnstile widget until the challenge page goes away.

    Returns an outcome instead of raising: the caller may still find the
    download button in a page that changed during the last poll, and the
    failure state is more useful in the transport's diagnostics than a bare
    exception.  ``timeout`` bounds the whole loop including reloads.
    """
    rng = random.Random(seed)
    started = clock()
    deadline = started + max(0.0, timeout)
    clicks = 0
    reloads = 0
    last_click = 0.0
    last_progress = started
    state = "challenge"

    while clock() < deadline:
        state = challenge_state(page)
        if state == "clear":
            return TurnstileOutcome(True, clicks, state, reloads)

        now = clock()
        if (
            state == "challenge"
            and clicks < max_clicks
            and (clicks == 0 or now - last_click >= click_interval)
        ):
            widget = find_turnstile_widget(page)
            if widget is not None:
                clicked = click_turnstile(
                    page,
                    widget,
                    rng=rng,
                    sleep=sleep,
                )
                if clicked:
                    clicks += 1
                    last_click = now
                    last_progress = now
                    log(
                        "[APKMirror solver] clicked the Turnstile checkbox "
                        f"({clicks}/{max_clicks})"
                    )
                    sleep(1.0)
                    continue

        if (
            state == "challenge"
            and reloads < max_reloads
            and now - last_progress >= reload_after
        ):
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                log(f"[APKMirror solver] challenge reload failed: {exc}")
                break
            reloads += 1
            clicks = 0
            last_click = 0.0
            last_progress = clock()
            log(f"[APKMirror solver] reloaded the challenge page ({reloads}/{max_reloads})")
            sleep(2.0)
            continue

        sleep(poll_interval)

    return TurnstileOutcome(False, clicks, state, reloads)


def describe_challenge(page: Any) -> dict[str, Any]:
    """Small, read-only snapshot for transport failure messages."""
    state: dict[str, Any] = {"state": challenge_state(page)}
    title = _page_title(page)
    if title:
        state["title"] = title
    url = _page_url(page)
    if url:
        state["url"] = url
    frames = challenge_frames(page)
    if frames:
        state["challenge_frames"] = len(frames)
    return state
