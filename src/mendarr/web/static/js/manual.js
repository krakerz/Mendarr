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

  const rfb = new RFB(box, url, {
    shared: true,
    wsProtocols: ["binary"],
  });
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.focusOnClick = true;

  rfb.addEventListener("connect", () => {
    clearTimeout(connectTimer);
    conn.className = "badge badge-success";
    conn.textContent = "connected";
    console.log("[Mendarr VNC] Connected successfully");
  });

  rfb.addEventListener("disconnect", (e) => {
    clearTimeout(connectTimer);
    conn.className = "badge badge-danger";
    conn.textContent = e.detail.clean ? "disconnected" : "connection lost";
    console.warn("[Mendarr VNC] Disconnected", e.detail);
  });

  document.body.addEventListener("manual-ended", () => rfb.disconnect());
}
