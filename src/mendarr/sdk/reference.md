# Mendarr plugin reference

## 1. File shape

A plugin is one Python file:

```python
from mendarr.sdk import Ctx, Page

META = {"backend": "camoufox", "notes": "what this version does"}


async def solve(page: Page, ctx: Ctx) -> None:
    ...
```

- `META` is a literal dict (no calls). `backend` is `"camoufox"` (default) or `"patchright"`; `notes` is free text.
- `solve(page, ctx)` is async and takes exactly those two arguments. `page` is a Playwright async `Page`
  that has already loaded the target URL. Returning normally means "done"; raising an exception means failure.
- Mendarr decides success itself after `solve` returns (page no longer a challenge, clearance cookie set).
- Do not navigate away; the page is already on the challenge.

## 2. `ctx` (the Ctx object)

Time is limited: every wait is capped by the request deadline.

- `ctx.remaining() -> float`: seconds left. Loop only while `ctx.remaining() > 0`.
- `ctx.deadline -> float`: the deadline as a `time.monotonic()` value.
- `ctx.log(msg: str) -> None`: write a step to the trace. Log what you do; it is shown to you next time.
- `ctx.clicks -> int`: number of successful `human_click` calls so far.
- `await ctx.challenge_present() -> bool`: True while the page is a Cloudflare challenge.
- `await ctx.wait_challenge(timeout=15) -> bool`: wait until a challenge shows up; False if the page is clean.
- `await ctx.wait_clearance(timeout=20) -> bool`: wait until the challenge is gone; True when cleared.
- `await ctx.find_turnstile_frame(timeout=10) -> Frame | None`: the Turnstile iframe, or None.
- `await ctx.turnstile_token() -> str | None`: the token once Turnstile has verified (a click is then in
  progress or done, do not click again).
- `await ctx.human_click(target, selector=None) -> bool`: a human-like click. `target` is a `Frame` (clicks
  inside it; with `selector` the matching element, e.g. `"input[type=checkbox]"`, else the checkbox area of
  the frame element), a `Locator` (its center), an `(x, y)` tuple of page coordinates, or `None` (the
  Turnstile widget found on the page). Returns True when a click happened.
- `await ctx.human_delay(lo=0.3, hi=1.2) -> None`: a random pause in seconds. Use this instead of fixed sleeps.
- `await ctx.screenshot(label="") -> bytes`: PNG of the page (also kept for the trace).

All clicks must go through `ctx.human_click`. Never use `page.click`, `locator.click` or `page.mouse`.
After a click Turnstile needs a few seconds to verify; clicking again meanwhile restarts it, so wait with
`ctx.wait_clearance(timeout=4)` between clicks.

## 3. Playwright calls you may use

- `page.frames` (list of frames), `page.main_frame`, `frame.url`, `frame.name`
- `page.locator(sel)`, `frame.locator(sel)`, then `await locator.count()`, `locator.first`, `locator.nth(i)`,
  `await locator.bounding_box()`, `await locator.is_visible()`
- `await page.title()`, `page.url`
- `await page.wait_for_timeout(ms)` works, but prefer `ctx.human_delay`.
- Avoid `page.evaluate` on a challenge page: running scripts there can reset the challenge.

## 4. Guard rules (code that breaks them is rejected before it runs)

- Imports allowed: `asyncio` (sleep, wait_for, wait, gather, timeout, Event, Lock, create_task ...), `re`,
  `random`, `math`, `time` (only `time.monotonic`, `time.time`, `time.perf_counter`; never `time.sleep`) and
  `from mendarr.sdk import Ctx, Page, Frame, Locator`. Nothing else.
- No file, network, process or thread access; no `open`, `eval`, `exec`, `compile`, `globals`, `type`, `object`.
- No names or attributes starting with `_`; no dunder access.
- No decorators, no bare `except:`, and do not catch `BaseException`, `CancelledError` or `SystemExit`.
- No path arguments (`path=`, `user_data_dir=`), no `launch`, `connect`, `save_as`, `set_input_files`.
- Module level may only contain imports, constants (no calls, no await) and definitions.
- Keep the file under 64 KB.

## 5. Worked example (the builtin plugin)

```python
from mendarr.sdk import Ctx, Page

META = {"backend": "camoufox", "notes": "builtin"}

# Turnstile needs a few seconds to verify a click; clicking again meanwhile restarts it
CLICK_COOLDOWN = 4.0
PROBE = 0.5


async def solve(page: Page, ctx: Ctx) -> None:
    if not await ctx.wait_challenge(timeout=10):
        return
    while ctx.remaining() > 0:
        if await ctx.wait_clearance(timeout=CLICK_COOLDOWN if ctx.clicks else PROBE):
            return
        if await ctx.turnstile_token():
            continue
        frame = await ctx.find_turnstile_frame(timeout=1)
        await ctx.human_delay(0.3, 0.9)
        if await ctx.human_click(frame, "input[type=checkbox]"):
            ctx.log(f"clicked the checkbox ({ctx.clicks})")
```
