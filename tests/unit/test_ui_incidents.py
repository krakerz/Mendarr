import base64
from urllib.parse import unquote

from fakeworker import snapshot

from mendarr.rpc import SolveResult, TraceData

PNG = b"\x89PNG\r\n\x1a\nfake"
URL = "http://example.com/x"


def _seed(client):
    st = client.app.state
    data = TraceData(
        screenshot_b64=base64.b64encode(PNG).decode(),
        dom="<p>hi</p>",
        network=[{"url": "https://x/cdn-cgi/a", "status": 403, "type": "xhr", "cf": "challenge"}],
        steps=["clicked"],
    )
    res = SolveResult(challenge_seen=True, snapshot=snapshot(URL, status=403, title="t"), trace=data)
    tid = st.traces.save(domain="example.com", url=URL, kind="failure", result=res, failure="challenge_loop")
    iid = st.store.note_failure("example.com", kind="challenge_loop", url=URL, trace_id=tid, plugin="_default@v1")
    st.store.update_incident(iid, trace_id=tid)
    return iid, tid


def test_list_and_detail(ui_client):
    iid, tid = _seed(ui_client)
    assert "example.com" in ui_client.get("/ui/incidents").text
    body = ui_client.get(f"/ui/incidents/{iid}").text
    assert URL in body and f"/ui/incidents/{iid}/manual" in body
    assert "cdn-cgi" in body and "clicked" in body
    assert ui_client.get("/ui/incidents/9999").status_code == 404


def test_trace_routes(ui_client):
    _, tid = _seed(ui_client)
    r = ui_client.get(f"/ui/traces/{tid}/screenshot.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert "sandbox" in ui_client.get(f"/ui/traces/{tid}/dom").headers["content-security-policy"]
    assert ui_client.get(f"/ui/traces/{tid}").status_code == 200
    assert ui_client.get("/ui/traces/nope/dom").status_code == 404
    assert ui_client.get("/ui/traces/nope").status_code == 404


def test_retry_and_close(ui_client):
    iid, _ = _seed(ui_client)
    store = ui_client.app.state.store
    assert ui_client.post(f"/ui/incidents/{iid}/retry", follow_redirects=False).status_code == 403
    r = ui_client.post(f"/ui/incidents/{iid}/retry", data={"csrf": ui_client.csrf}, follow_redirects=False)
    assert r.status_code == 303
    row = store.incident(iid)
    assert row["state"] == "open" and row["heal_window"] == 0
    r = ui_client.post(
        f"/ui/incidents/{iid}/close", data={"csrf": ui_client.csrf, "state": "gave_up"}, follow_redirects=False
    )
    assert r.status_code == 303
    row = store.incident(iid)
    assert row["state"] == "gave_up" and row["note"] == "closed from the dashboard"
    r = ui_client.post(f"/ui/incidents/{iid}/close", data={"csrf": ui_client.csrf, "state": "open"})
    assert r.status_code == 400


def test_transcript(ui_client, tmp_path):
    iid, _ = _seed(ui_client)
    d = tmp_path / "heal" / str(iid)
    d.mkdir(parents=True)
    (d / "a1-prompt.json").write_text('[{"role":"user","content":"PROMPTTEXT"}]')
    (d / "a1-response.md").write_text("RESPONSETEXT")
    store = ui_client.app.state.store
    a = store.add_attempt(
        iid, tier=1, result="parse", prompt_ref=f"heal/{iid}/a1-prompt.json", response_ref=f"heal/{iid}/a1-response.md"
    )
    body = ui_client.get(f"/ui/incidents/{iid}/attempts/{a}/transcript").text
    assert "PROMPTTEXT" in body and "RESPONSETEXT" in body
    bad = store.add_attempt(iid, tier=1, result="parse", prompt_ref="../../etc/passwd")
    assert "Transcript not saved" in ui_client.get(f"/ui/incidents/{iid}/attempts/{bad}/transcript").text


def test_unauthenticated(client):
    assert client.get("/ui/incidents", follow_redirects=False).status_code == 303


def test_clear_closed_and_delete_incident(ui_client):
    iid, _ = _seed(ui_client)
    store = ui_client.app.state.store

    # Initially open - clear button not visible
    page = ui_client.get("/ui/incidents?state=all").text
    assert "/ui/incidents/clear" not in page

    # Close incident
    ui_client.post(f"/ui/incidents/{iid}/close", data={"csrf": ui_client.csrf, "state": "solved"})

    # Clear button visible
    page = ui_client.get("/ui/incidents?state=solved").text
    assert "/ui/incidents/clear" in page

    # Delete incident button visible on closed detail page
    detail = ui_client.get(f"/ui/incidents/{iid}").text
    assert f"/ui/incidents/{iid}/delete" in detail

    # CSRF checks
    assert ui_client.post("/ui/incidents/clear", follow_redirects=False).status_code == 403
    assert ui_client.post(f"/ui/incidents/{iid}/delete", follow_redirects=False).status_code == 403

    # Delete non-existent incident 404
    assert ui_client.post("/ui/incidents/9999/delete", data={"csrf": ui_client.csrf}).status_code == 404

    # Clear closed incidents
    r = ui_client.post("/ui/incidents/clear", data={"csrf": ui_client.csrf}, follow_redirects=False)
    assert r.status_code == 303 and "closed incident(s) cleared" in unquote(r.headers["location"])
    assert store.incident(iid) is None

    # Delete specific incident
    iid2, _ = _seed(ui_client)
    r = ui_client.post(f"/ui/incidents/{iid2}/delete", data={"csrf": ui_client.csrf}, follow_redirects=False)
    assert r.status_code == 303 and f"Incident #{iid2} deleted" in unquote(r.headers["location"])
    assert store.incident(iid2) is None
