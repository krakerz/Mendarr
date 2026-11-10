"""Human-like clicks: real X events through xdotool, Playwright's mouse as the fallback."""

import asyncio
import os
import random
import shutil
from typing import Any

# Keep in step with the screen size in worker/display.py (the sdk stays independent of the worker)
SCREEN_W, SCREEN_H = 1366, 768


async def screen_origin(page: Any) -> tuple[float, float] | None:
    # Camoufox evaluates in an isolated world, so the page itself can't see these values
    try:
        v = await page.evaluate("() => [window.mozInnerScreenX, window.mozInnerScreenY]")
    except Exception:
        return None
    if (
        isinstance(v, list)
        and len(v) == 2
        and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in v)
    ):
        return float(v[0]), float(v[1])
    return None


async def _xdotool(display: int, *args: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "xdotool",
        *args,
        env={**os.environ, "DISPLAY": f":{display}"},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait() == 0


async def xdotool_click(display: int, sx: int, sy: int) -> bool:
    start_x = min(max(sx + random.choice((-1, 1)) * random.randint(80, 200), 0), SCREEN_W - 1)
    start_y = min(max(sy + random.choice((-1, 1)) * random.randint(80, 200), 0), SCREEN_H - 1)
    steps = random.randint(4, 7)
    bend = random.uniform(-25, 25)
    points = []
    for i in range(1, steps + 1):
        t = i / (steps + 1)
        arc = bend * 4 * t * (1 - t)  # slight curve, zero at both ends
        points.append(
            (
                round(start_x + (sx - start_x) * t + arc + random.uniform(-2, 2)),
                round(start_y + (sy - start_y) * t + random.uniform(-2, 2)),
            )
        )
    points += [(sx, sy)]
    for x, y in points:
        if not await _xdotool(display, "mousemove", "--sync", str(x), str(y)):
            return False
        await asyncio.sleep(random.uniform(0.015, 0.04))
    await asyncio.sleep(random.uniform(0.06, 0.18))
    if not await _xdotool(display, "mousedown", "1"):
        return False
    await asyncio.sleep(random.uniform(0.05, 0.12))
    return await _xdotool(display, "mouseup", "1")


async def mouse_click(page: Any, x: float, y: float) -> None:
    await page.mouse.move(x, y, steps=random.randint(8, 20))
    await asyncio.sleep(random.uniform(0.06, 0.18))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.05, 0.12))
    await page.mouse.up()


async def click(
    page: Any, x: float, y: float, *, display: int | None, backend: str
) -> tuple[bool, str]:
    if backend != "mouse" and display is not None and shutil.which("xdotool"):
        origin = await screen_origin(page)
        if origin is not None:
            sx, sy = round(origin[0] + x), round(origin[1] + y)
            if 0 <= sx < SCREEN_W and 0 <= sy < SCREEN_H:
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
                return await xdotool_click(display, sx, sy), "xdotool"
    if backend == "xdotool":
        return False, "xdotool"
    try:
        await mouse_click(page, x, y)
    except Exception:
        return False, "mouse"
    return True, "mouse"
