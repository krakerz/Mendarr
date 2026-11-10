import json

import httpx
import pytest

from mendarr.healer.config import LlmProfile
from mendarr.healer.llm import LlmClient, LlmUnavailable, match_model


def test_match_model():
    ids = ["pub/Gemma-4-12B-it", "other/qwen"]
    assert match_model("", ids) == "pub/Gemma-4-12B-it"
    assert match_model("other/qwen", ids) == "other/qwen"
    assert match_model("GEMMA", ids) == "pub/Gemma-4-12B-it"
    assert match_model("llama", ids) is None
    assert match_model("", []) is None


def client(handler):
    return LlmClient(transport=httpx.MockTransport(handler))


async def test_pick_skips_unreachable():
    def handler(req):
        if req.url.host == "down":
            raise httpx.ConnectError("no")
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    c = client(handler)
    profiles = {"a": LlmProfile(base_url="http://down/v1"), "b": LlmProfile(base_url="http://up/v1")}
    ep = await c.pick(["ghost", "a", "b"], profiles)
    assert ep is not None and ep.name == "b" and ep.model == "m1"
    assert await c.pick(["a"], profiles) is None
    await c.aclose()


async def test_probe_bad_json_or_status():
    c = client(lambda req: httpx.Response(200, text="nope"))
    assert await c.probe("a", LlmProfile(base_url="http://x/v1")) is None
    c2 = client(lambda req: httpx.Response(503))
    assert await c2.probe("a", LlmProfile(base_url="http://x/v1")) is None
    await c.aclose()
    await c2.aclose()


async def test_complete_body_and_headers():
    seen = {}

    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m1"}]})
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": {"total_tokens": 3}})

    c = client(handler)
    p = LlmProfile(base_url="http://x/v1/", api_key="k", reasoning_effort="none")
    ep = await c.probe("a", p)
    reply = await c.complete(ep, [{"role": "user", "content": "q"}])
    assert reply.text == "hi" and reply.usage == {"total_tokens": 3}
    assert seen["auth"] == "Bearer k"
    b = seen["body"]
    assert b["model"] == "m1" and b["stream"] is False and b["reasoning_effort"] == "none"
    assert b["temperature"] == 1.0 and b["top_p"] == 0.95 and b["top_k"] == 64

    p2 = LlmProfile(base_url="http://x/v1", temperature=None, top_p=None, top_k=None)
    await c.complete(await c.probe("a", p2), [])
    assert seen["auth"] is None
    assert not {"temperature", "top_p", "top_k", "reasoning_effort"} & set(seen["body"])
    await c.aclose()


async def test_complete_errors():
    def models_then_500(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m1"}]})
        return httpx.Response(500, text="boom")

    c = client(models_then_500)
    ep = await c.probe("a", LlmProfile(base_url="http://x/v1"))
    with pytest.raises(LlmUnavailable, match="HTTP 500"):
        await c.complete(ep, [])
    c2 = client(lambda req: httpx.Response(200, json={"nothing": 1}))
    with pytest.raises(LlmUnavailable, match="malformed"):
        await c2.complete(ep, [])

    def down(req):
        raise httpx.ConnectError("down")

    c3 = client(down)
    with pytest.raises(LlmUnavailable):
        await c3.complete(ep, [])
    for x in (c, c2, c3):
        await x.aclose()
