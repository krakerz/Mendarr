"""Browser language shared by every backend: the configured tag, else the exit country's main language."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from mendarr.config import Settings

log = logging.getLogger(__name__)


def main_locale(ip: str) -> str | None:
    """The exit country's most spoken language, e.g. en-AU; None when the IP or country is unknown.

    Camoufox's geoip picks the language at random, weighted by population share, so an Australian exit
    sometimes came up as zh-Hant-AU and pages rendered in Chinese. Pinning the main language keeps every
    launch the same while the timezone and position still follow the IP.
    """
    try:
        from camoufox.geolocation import get_geolocation
        from camoufox.locales import SELECTOR, normalize_locale

        region = get_geolocation(ip).locale.region
        if not region:
            return None
        languages, shares = SELECTOR._load_territory_data(region)
        language = str(languages[int(shares.argmax())]).replace("_", "-")
        return normalize_locale(f"{language}-{region}").as_string
    except Exception as e:  # the geoip database or Camoufox internals: fall back to the backend's own default
        log.warning("no main locale for %s: %s", ip, e)
        return None


def explicit_locale(settings: Settings, wanted: str | None = None) -> str:
    """The Settings-page tag the API sent (`wanted`), else `settings.locale`; empty = none set."""
    return (wanted or "").strip() or settings.locale.strip()


async def locale_for_ip(settings: Settings, ip: str | None, wanted: str | None = None) -> str | None:
    """The explicit tag when set, else the main language of a known exit IP; None = backend default."""
    explicit = explicit_locale(settings, wanted)
    if explicit:
        return explicit
    if settings.geoip and ip and ip not in ("default", "unknown"):
        return await asyncio.to_thread(main_locale, ip)
    return None


async def resolve_locale(
    settings: Settings, exit_ip: Callable[[], Awaitable[str]] | None, wanted: str | None = None
) -> str | None:
    """Like `locale_for_ip`, looking the exit IP up only when no explicit locale is set."""
    explicit = explicit_locale(settings, wanted)
    if explicit:
        return explicit
    if settings.geoip and exit_ip is not None:
        return await locale_for_ip(settings, await exit_ip())
    return None
