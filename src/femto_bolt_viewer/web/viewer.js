"use strict";
const byId = (id) => document.getElementById(id);
let socket = null;
let reconnect = null;
let generation = 0;
let lastMessage = 0;
const number = (value, digits = 0, unit = "") =>
  Number.isFinite(value) ? `${value.toFixed(digits)}${unit}` : "—";
function image(id, value) {
  const target = byId(id);
  target.hidden = !value;
  byId(`${id}-empty`).hidden = !!value;
  if (value) target.src = `data:image/jpeg;base64,${value}`;
  else target.removeAttribute("src");
}
function disconnected(message) {
  byId("connection").textContent = message;
  byId("status-dot").classList.remove("live");
  image("rgb", null); image("depth", null);
  for (const name of ["fps", "conversion", "age", "pair", "valid"]) byId(name).textContent = "—";
}
function render(frame) {
  lastMessage = Date.now();
  const live = !!frame.color_jpeg && !!frame.depth_jpeg;
  byId("demo-banner").hidden = !frame.demo;
  byId("connection").textContent = frame.demo ? "Synthetic demo" : live ? "Live · local" : "Waiting for frames";
  byId("status-dot").classList.toggle("live", live && !frame.demo);
  byId("error").hidden = !frame.error;
  byId("error").textContent = frame.error || "";
  byId("mode").textContent = frame.mode === "wfov-binned" ? "Wide field · 120° × 120° depth" : "Narrow field · 75° × 65° depth";
  image("rgb", frame.color_jpeg); image("depth", frame.depth_jpeg);
  byId("rgb-size").textContent = frame.color_width ? `${frame.color_width} × ${frame.color_height} · color sensor` : "Color sensor";
  byId("depth-size").textContent = `${frame.width} × ${frame.height} · millimeters`;
  byId("fps").textContent = number(frame.depth_fps, 1, " fps");
  byId("conversion").textContent = number(frame.conversion_ms, 1, " ms");
  byId("age").textContent = number(frame.depth_age_ms, 0, " ms");
  byId("pair").textContent = number(frame.pair_delta_ms, 0, " ms");
  byId("valid").textContent = number(Number.isFinite(frame.valid_fraction) ? frame.valid_fraction * 100 : null, 1, "%");
  byId("diagnostic").textContent = frame.demo ? "Demo mode does not test USB access or depth conversion." :
    `Frames skipped ${frame.dropped_depth || 0} · Rejected ${frame.rejected_depth || 0} · Restarts ${frame.restarts || 0} · Native macOS conversion`;
}
function connect() {
  clearTimeout(reconnect);
  const current = ++generation;
  if (socket) socket.close();
  disconnected("Connecting…");
  const max = Number(byId("range").value);
  byId("max-mm").textContent = `${max.toLocaleString()} mm`;
  socket = new WebSocket(`ws://${location.host}/frames?max_mm=${max}`);
  socket.onmessage = (event) => {
    if (current !== generation) return;
    try { render(JSON.parse(event.data)); }
    catch { disconnected("Invalid viewer response"); }
  };
  socket.onclose = () => {
    if (current !== generation) return;
    disconnected("Viewer disconnected · retrying");
    reconnect = setTimeout(connect, 1500);
  };
  socket.onerror = () => socket.close();
}
byId("range").addEventListener("change", connect);
setInterval(() => {
  if (lastMessage && Date.now() - lastMessage > 2500) disconnected("Waiting for fresh frames");
}, 500);
window.addEventListener("pagehide", () => { ++generation; clearTimeout(reconnect); if (socket) socket.close(); });
connect();
