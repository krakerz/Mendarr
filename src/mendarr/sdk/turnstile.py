"""Locating the Turnstile widget and picking a click point."""

import random
from typing import Any

from mendarr.core import markers

INSET = 28  # px from the widget's left edge: where the checkbox sits


def frame_matches(url: str) -> bool:
    return any(h in url for h in markers.TURNSTILE_FRAME_HINTS)


def inset_point(box: dict, rng: Any = random) -> tuple[float, float]:
    x = box["x"] + min(INSET, box["width"] / 2) + rng.uniform(-3, 3)
    y = box["y"] + box["height"] / 2 + rng.uniform(-3, 3)
    return x, y


def center_point(box: dict, rng: Any = random) -> tuple[float, float]:
    dx = min(3, box["width"] / 4)
    dy = min(3, box["height"] / 4)
    return (
        box["x"] + box["width"] / 2 + rng.uniform(-dx, dx),
        box["y"] + box["height"] / 2 + rng.uniform(-dy, dy),
    )


async def widget_box(page: Any) -> dict | None:
    # The widget div is an ancestor of the response input; the shadow root stays untouched
    for depth in range(1, 5):
        try:
            loc = page.locator(f"{markers.TURNSTILE_RESPONSE_INPUT} >> xpath=ancestor::div[{depth}]")
            box = await loc.first.bounding_box(timeout=1000)
        except Exception:
            continue
        if box and box["width"] > 40 and 20 < box["height"] < 120:
            return box
    return None


async def click_point(page: Any, target: Any, selector: str | None) -> tuple[float, float] | None:
    if isinstance(target, tuple):
        return target
    if target is not None and hasattr(target, "bounding_box") and not hasattr(target, "frame_element"):
        try:
            box = await target.bounding_box()
            if box:
                return center_point(box)
        except Exception:
            pass
    elif target is not None:
        if selector:
            try:
                box = await target.locator(selector).first.bounding_box(timeout=1000)
                if box:
                    return center_point(box)
            except Exception:
                pass
        try:
            box = await (await target.frame_element()).bounding_box()
            if box:
                return inset_point(box)
        except Exception:
            pass
    box = await widget_box(page)
    return inset_point(box) if box else None
