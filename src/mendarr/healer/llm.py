"""OpenAI-compatible chat client (LM Studio): endpoint probing, model matching, one completion."""

import logging
import time
from dataclasses import dataclass

import httpx

from mendarr.healer.config import LlmProfile

log = logging.getLogger(__name__)
PROBE_TIMEOUT_S = 3.0


class LlmUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Endpoint:
    name: str
    profile: LlmProfile
    model: str


@dataclass(frozen=True)
class LlmReply:
    text: str
    endpoint: Endpoint
    elapsed_s: float
    usage: dict


def match_model(want: str, ids: list[str]) -> str | None:
    if not ids:
        return None
    if want == "":
        return ids[0]
    if want in ids:
        return want
    low = want.lower()
    return next((i for i in ids if low in i.lower()), None)


def _headers(profile: LlmProfile) -> dict[str, str]:
    return {"Authorization": f"Bearer {profile.api_key}"} if profile.api_key else {}


class LlmClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(transport=transport)

    async def probe(self, name: str, profile: LlmProfile) -> Endpoint | None:
        url = f"{profile.base_url.rstrip('/')}/models"
        try:
            r = await self._http.get(url, headers=_headers(profile), timeout=PROBE_TIMEOUT_S)
            r.raise_for_status()
            ids = [str(m["id"]) for m in r.json()["data"]]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.info("llm %s unreachable: %s", name, e)
            return None
        model = match_model(profile.model, ids)
        if model is None:
            log.warning("llm %s: no model matches %r (have %s)", name, profile.model, ids)
            return None
        return Endpoint(name, profile, model)

    async def pick(self, order: list[str], profiles: dict[str, LlmProfile]) -> Endpoint | None:
        for name in order:
            profile = profiles.get(name)
            if profile is None:
                continue
            ep = await self.probe(name, profile)
            if ep is not None:
                return ep
        return None

    async def complete(self, ep: Endpoint, messages: list[dict]) -> LlmReply:
        p = ep.profile
        body: dict = {"model": ep.model, "messages": messages, "max_tokens": p.max_tokens, "stream": False}
        for key, value in (
            ("temperature", p.temperature),
            ("top_p", p.top_p),
            ("top_k", p.top_k),
            ("reasoning_effort", p.reasoning_effort),
        ):
            if value is not None:
                body[key] = value
        url = f"{p.base_url.rstrip('/')}/chat/completions"
        t0 = time.monotonic()
        try:
            r = await self._http.post(url, json=body, headers=_headers(p), timeout=p.timeout_s)
        except httpx.HTTPError as e:
            raise LlmUnavailable(f"{ep.name}: {type(e).__name__}: {e}") from e
        if r.status_code // 100 != 2:
            raise LlmUnavailable(f"{ep.name}: HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LlmUnavailable(f"{ep.name}: malformed reply") from e
        usage = data.get("usage") if isinstance(data, dict) else None
        return LlmReply(text, ep, time.monotonic() - t0, usage or {})

    async def aclose(self) -> None:
        await self._http.aclose()
