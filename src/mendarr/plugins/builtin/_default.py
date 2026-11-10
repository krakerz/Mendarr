"""Builtin fallback: wait out the interstitial, click the Turnstile checkbox when one is offered.

Flow modelled on Byparr's src/challenge.py (github.com/ThePhaseless/Byparr) from its behaviour
only; Byparr is GPL-3.0, so no code was copied.
"""

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
