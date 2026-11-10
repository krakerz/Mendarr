"""Page state helpers shared by the executor and the browser cookie check."""

from typing import Any

from mendarr.rpc import Cookie, ProxySpec, Snapshot


class MainResponseTracker:
    """Keeps the last main-frame navigation response."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self._response: Any = None
        page.on("response", self._on_response)

    def _on_response(self, resp: Any) -> None:
        if resp.frame == self._page.main_frame and resp.request.is_navigation_request():
            self._response = resp

    async def headers(self) -> dict[str, str]:
        if self._response is None:
            return {}
        try:
            raw = await self._response.all_headers()
        except Exception:
            raw = self._response.headers
        return {k.lower(): v for k, v in raw.items()}

    def status(self) -> int:
        return self._response.status if self._response is not None else 200


def pw_to_cookie(c: dict) -> Cookie:
    return Cookie(
        name=c["name"],
        value=c["value"],
        domain=c.get("domain", ""),
        path=c.get("path", "/"),
        expires=c.get("expires", -1),
        httpOnly=c.get("httpOnly", False),
        secure=c.get("secure", False),
        sameSite=c.get("sameSite", "Lax"),
    )


def request_cookies_to_pw(cookies: list[dict], url: str) -> list[dict]:
    out = []
    for c in cookies:
        if "name" not in c or "value" not in c:
            continue
        if c.get("domain"):
            out.append(
                {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c["domain"],
                    "path": c.get("path") or "/",
                }
            )
        else:
            out.append({"name": c["name"], "value": c["value"], "url": url})
    return out


def pw_proxy(proxy: ProxySpec | None) -> dict | None:
    if proxy is None:
        return None
    d = {"server": proxy.url, "username": proxy.username, "password": proxy.password}
    return {k: v for k, v in d.items() if v is not None}


async def snapshot(page: Any, context: Any, tracker: MainResponseTracker) -> Snapshot:
    return Snapshot(
        url=page.url,
        status=tracker.status(),
        headers=await tracker.headers(),
        title=await page.title(),
        html=await page.content(),
        cookies=[pw_to_cookie(c) for c in await context.cookies([page.url])],
    )
