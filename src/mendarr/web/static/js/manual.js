import RFB from "/ui/static/vendor/novnc/core/rfb.js";

const box = document.getElementById("vnc");
const conn = document.getElementById("vnc-conn");
if (box && box.dataset.active === "1") {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const url = `${scheme}://${location.host}/ui/vnc/${box.dataset.slot}`;
  console.log("[Mendarr VNC] Connecting to", url);

  const connectTimer = setTimeout(() => {
    if (conn && conn.textContent.includes("connecting")) {
      conn.className = "badge badge-warning";
      conn.textContent = "connecting (slow)... check reverse proxy websocket support";
      console.warn("[Mendarr VNC] Connection still pending after 10s. If behind a reverse proxy, ensure WebSocket upgrade is enabled.");
    }
  }, 10000);

  const t0 = performance.now();
  const since = () => ((performance.now() - t0) / 1000).toFixed(1) + "s";
  const timings = {};
  const mark = (what) => {
    if (timings[what]) return;
    timings[what] = since();
    console.log(`[Mendarr VNC] ${what} after ${timings[what]}`);
  };

  const rfb = new RFB(box, url, {
    shared: true,
    wsProtocols: ["binary"],
  });
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.focusOnClick = true;

  // timing probes: extra listeners on noVNC's socket (its own handlers stay in place)
  let received = 0;
  let afterHandshake = false; // set a tick after "connect", so the ServerInit message itself doesn't count as a frame
  const ws = rfb._sock && rfb._sock._websocket;
  if (ws) {
    ws.addEventListener("open", () => mark("websocket open"));
    ws.addEventListener("message", (e) => {
      if (!received) mark("first server bytes");
      received += e.data.byteLength || e.data.size || 0;
      // the first data after ServerInit is the first framebuffer update (compressed, so size varies)
      if (afterHandshake && !timings["first frame"]) {
        mark("first frame");
        if (conn.textContent.startsWith("connected")) conn.textContent = `connected (frame ${timings["first frame"]})`;
      }
    });
  }

  rfb.addEventListener("connect", () => {
    clearTimeout(connectTimer);
    mark("handshake done");
    setTimeout(() => { afterHandshake = true; }, 0);
    conn.className = "badge badge-success";
    conn.textContent = `connected in ${timings["handshake done"]}`;
    console.log("[Mendarr VNC] Connected successfully", timings);
  });

  rfb.addEventListener("disconnect", (e) => {
    clearTimeout(connectTimer);
    conn.className = "badge badge-danger";
    conn.textContent = e.detail.clean ? "disconnected" : "connection lost";
    console.warn("[Mendarr VNC] Disconnected after", since(), e.detail, timings);
  });

  document.body.addEventListener("manual-ended", () => rfb.disconnect());
}
