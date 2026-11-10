import json

import httpx

from mendarr.healer.config import NotifyConfig, NotifyEvents
from mendarr.healer.notify import COLORS, Notice, Notifier

CFG = NotifyConfig(discord_webhook="http://discord.test/hook", webhook_url="http://hook.test/h")


def notifier(handler, public_url="http://m.lan:8191/"):
    return Notifier(public_url, transport=httpx.MockTransport(handler))


def recorder(status=200):
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(status)

    return seen, handler


async def test_discord_embed():
    seen, h = recorder(204)
    n = notifier(h)
    res = await n.send(CFG, Notice("healed", "e.com", "done", 7, "challenge_loop", "e.com@v2"), channels=("discord",))
    assert res == {"discord": True}
    body = json.loads(seen[0].content)
    e = body["embeds"][0]
    assert body["username"] == "Mendarr" and e["title"] == "Healed: e.com" and e["color"] == COLORS["healed"]
    assert e["url"] == "http://m.lan:8191/ui/incidents/7"
    assert {f["name"] for f in e["fields"]} == {"Failure", "Plugin"} and "thumbnail" not in e
    await n.aclose()


async def test_discord_minimal_fields_no_url():
    seen, h = recorder()
    n = notifier(h, public_url="")
    await n.send(CFG, Notice("manual", "e.com", "m"), channels=("discord",))
    e = json.loads(seen[0].content)["embeds"][0]
    assert e["fields"] == [] and "url" not in e
    await n.aclose()


async def test_discord_multipart_with_screenshot():
    seen, h = recorder()
    n = notifier(h)
    await n.send(CFG, Notice("manual", "e.com", "m", 1, screenshot=b"PNGDATA"), channels=("discord",))
    req = seen[0]
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b"payload_json" in req.content and b'name="files[0]"' in req.content and b"PNGDATA" in req.content
    assert b"attachment://screenshot.png" in req.content
    await n.aclose()


async def test_generic_webhook():
    seen, h = recorder()
    n = notifier(h)
    res = await n.send(CFG, Notice("rollback", "e.com", "m", 3, "k", "p"), channels=("webhook",))
    assert res == {"webhook": True}
    body = json.loads(seen[0].content)
    assert {"event", "domain", "incident_id", "kind", "plugin", "message", "url", "at"} <= set(body)
    assert body["event"] == "rollback" and body["incident_id"] == 3
    await n.aclose()


async def test_disabled_event_and_channel_subset():
    seen, h = recorder()
    n = notifier(h)
    off = NotifyConfig(discord_webhook="http://d.test/h", events=NotifyEvents(healed=False))
    assert await n.send(off, Notice("healed", "e.com", "m")) == {}
    assert seen == []
    res = await n.send(CFG, Notice("manual", "e.com", "m"), channels=("webhook",))
    assert list(res) == ["webhook"] and len(seen) == 1
    assert await n.send(NotifyConfig(), Notice("manual", "e.com", "m")) == {}
    await n.aclose()


async def test_failures_never_raise():
    n = notifier(lambda req: httpx.Response(500))
    assert await n.send(CFG, Notice("manual", "e.com", "m")) == {"discord": False, "webhook": False}
    await n.aclose()

    def boom(req):
        raise httpx.ConnectError("down")

    n2 = notifier(boom)
    assert await n2.send(CFG, Notice("manual", "e.com", "m")) == {"discord": False, "webhook": False}
    await n2.aclose()
