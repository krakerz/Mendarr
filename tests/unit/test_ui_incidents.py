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


def test_detail_stamp_changes_with_state_and_activity(ui_client):
    iid, _ = _seed(ui_client)
    st = ui_client.app.state
    first = ui_client.get(f"/ui/incidents/{iid}/stamp")
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert f'data-stamp="{first.text}"' in ui_client.get(f"/ui/incidents/{iid}").text
    tok = st.tracker.start("healer", "example.com", URL, description=f"incident #{iid}: round 0 via pc", incident_id=iid)
    second = ui_client.get(f"/ui/incidents/{iid}/stamp").text
    assert second != first.text
    assert "round 0 via pc" in ui_client.get(f"/ui/incidents/{iid}").text
    st.tracker.finish(tok)
    st.store.update_incident(iid, state="healing")
    assert ui_client.get(f"/ui/incidents/{iid}/stamp").text not in (first.text, second)
    assert ui_client.get("/ui/incidents/9999/stamp").status_code == 404


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

    # Delete specific incident: an open one is refused
    iid2, _ = _seed(ui_client)
    r = ui_client.post(f"/ui/incidents/{iid2}/delete", data={"csrf": ui_client.csrf}, follow_redirects=False)
    assert r.status_code == 409 and "still open" in r.text
    assert store.incident(iid2) is not None
    ui_client.post(f"/ui/incidents/{iid2}/close", data={"csrf": ui_client.csrf, "state": "gave_up"})
    r = ui_client.post(f"/ui/incidents/{iid2}/delete", data={"csrf": ui_client.csrf}, follow_redirects=False)
    assert r.status_code == 303 and f"Incident #{iid2} deleted" in unquote(r.headers["location"])
    assert store.incident(iid2) is None


def test_trace_meta_records_backend(ui_client):
    st = ui_client.app.state
    res = SolveResult(snapshot=snapshot(URL), backend="patchright")
    tid = st.traces.save(domain="example.com", url=URL, kind="failure", result=res)
    assert st.traces.load(tid).meta["backend"] == "patchright"
    tid = st.traces.save(domain="example.com", url=URL, kind="failure", result=None)
    assert st.traces.load(tid).meta["backend"] == ""


def test_incident_and_trace_show_backend(ui_client):
    st = ui_client.app.state
    res = SolveResult(snapshot=snapshot(URL, status=403), backend="obscura")
    tid = st.traces.save(domain="example.com", url=URL, kind="failure", result=res, failure="challenge_loop")
    iid = st.store.note_failure("example.com", kind="challenge_loop", url=URL, trace_id=tid, plugin="_default@v1")
    st.store.update_incident(iid, trace_id=tid)
    rtid = st.traces.save(
        domain="example.com", url=URL, kind="replay", result=SolveResult(snapshot=snapshot(URL), backend="patchright")
    )
    st.store.add_attempt(iid, tier=0, result="replay_failed", detail="d", trace_id=rtid)
    body = ui_client.get(f"/ui/incidents/{iid}").text
    assert "<dt>Backend</dt>" in body and "obscura" in body and "patchright" in body
    assert "inferred" not in body
    assert "obscura" in ui_client.get(f"/ui/traces/{tid}").text


def test_incident_backend_fallback_for_old_trace(ui_client):
    iid, tid = _seed(ui_client)  # SolveResult without backend: an old trace
    body = ui_client.get(f"/ui/incidents/{iid}").text
    assert "<dt>Backend</dt>" in body and "inferred" in body
    assert "unknown" in ui_client.get(f"/ui/traces/{tid}").text
