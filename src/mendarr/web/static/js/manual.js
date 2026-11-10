import RFB from "/ui/static/vendor/novnc/core/rfb.js";

const box = document.getElementById("vnc");
const conn = document.getElementById("vnc-conn");
if (box && box.dataset.active === "1") {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const rfb = new RFB(box, `${scheme}://${location.host}/ui/vnc/${box.dataset.slot}`, {
    shared: true,
    wsProtocols: ["binary"],
  });
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.focusOnClick = true;
  rfb.addEventListener("connect", () => { conn.textContent = "connected"; });
  rfb.addEventListener("disconnect", (e) => { conn.textContent = e.detail.clean ? "disconnected" : "connection lost"; });
  document.body.addEventListener("manual-ended", () => rfb.disconnect());
}
