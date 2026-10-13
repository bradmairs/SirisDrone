/* SirisDrone Studio – front end (no build step, plain JS). */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const clone = (o) => JSON.parse(JSON.stringify(o));
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

const state = {
  cfg: null,
  items: [],
  filter: "all",
  current: null,       // media item
  edit: null,          // its edit document
  selected: new Set(),
  luts: [],
  cropMode: false,
  before: false,
  t: 0,                // playhead (video)
  frame: null,         // {w, h} of the last "frame" preview (straightened, uncropped)
  clipboard: null,
  jobs: [],
  stampVersion: Date.now(),
};

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) { /* not json */ }
    throw new Error(msg);
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r;
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 3500);
}

function fmtTime(s) {
  if (s == null || isNaN(s)) return "–";
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(1).padStart(4, "0")}`;
}
function fmtBytes(b) {
  if (b > 1e9) return (b / 1e9).toFixed(2) + " GB";
  if (b > 1e6) return (b / 1e6).toFixed(1) + " MB";
  return Math.round(b / 1e3) + " KB";
}
function getPath(obj, path) { return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj); }
function setPath(obj, path, v) {
  const ks = path.split("."); const last = ks.pop();
  ks.reduce((o, k) => o[k], obj)[last] = v;
}
function debounce(fn, ms) {
  let t; const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  d.flush = () => { clearTimeout(t); return fn(); };
  return d;
}

// Same maths as edits.straighten_scale on the server.
function straightenScale(w, h, deg) {
  const a = Math.abs(deg) * Math.PI / 180;
  if (!a) return 1;
  const c = Math.cos(a), s = Math.sin(a);
  return Math.min(w / (w * c + h * s), h / (w * s + h * c));
}
function frameSize(item, edit) {
  let w = item.width, h = item.height;
  if (edit.rotate % 180) [w, h] = [h, w];
  const k = straightenScale(w, h, edit.straighten);
  return { w: w * k, h: h * k };
}

// ---------------------------------------------------------------------------
// preview
// ---------------------------------------------------------------------------
let previewBusy = false, previewDirty = false, previewSeq = 0, previewURL = null;

function previewSize() {
  const st = $("#stage");
  const px = Math.max(st.clientWidth, st.clientHeight) * Math.min(window.devicePixelRatio || 1, 2);
  return Math.round(clamp(px, 400, 2000));
}

function requestPreview() {
  previewDirty = true;
  if (!previewBusy) pumpPreview();
}

async function pumpPreview() {
  if (!previewDirty || !state.current) return;
  previewDirty = false;
  previewBusy = true;
  const seq = ++previewSeq;
  const item = state.current;
  $("#spinner").hidden = false;
  try {
    const r = await fetch(`/api/media/${item.id}/preview`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        edit: state.edit, size: previewSize(), t: state.t,
        mode: state.cropMode ? "frame" : "final", original: state.before,
      }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    const blob = await r.blob();
    if (seq === previewSeq && state.current === item) {
      const url = URL.createObjectURL(blob);
      const img = $("#previewImg");
      img.src = url;
      await img.decode().catch(() => {});
      if (previewURL) URL.revokeObjectURL(previewURL);
      previewURL = url;
      if (state.cropMode) state.frame = { w: +r.headers.get("X-Width"), h: +r.headers.get("X-Height") };
      $("#stageMsg").hidden = true;
      layoutCrop();
      drawHistogram(img);
    }
  } catch (e) {
    toast(e.message, "error");
  } finally {
    previewBusy = false;
    $("#spinner").hidden = true;
    if (previewDirty) pumpPreview();
  }
}

function drawHistogram(img) {
  const cv = $("#histogram"), ctx = cv.getContext("2d");
  const w = 160, h = Math.max(1, Math.round(160 * img.naturalHeight / img.naturalWidth));
  const off = document.createElement("canvas"); off.width = w; off.height = h;
  const octx = off.getContext("2d", { willReadFrequently: true });
  octx.drawImage(img, 0, 0, w, h);
  const d = octx.getImageData(0, 0, w, h).data;
  const bins = [new Uint32Array(256), new Uint32Array(256), new Uint32Array(256)];
  for (let i = 0; i < d.length; i += 4) { bins[0][d[i]]++; bins[1][d[i + 1]]++; bins[2][d[i + 2]]++; }
  let max = 1;
  for (const b of bins) for (let i = 2; i < 254; i++) max = Math.max(max, b[i]);
  cv.width = cv.clientWidth * 2; cv.height = cv.clientHeight * 2;
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.globalCompositeOperation = "lighter";
  ["#ff4d4d", "#4dff6a", "#4d8bff"].forEach((col, c) => {
    ctx.fillStyle = col + "99";
    ctx.beginPath(); ctx.moveTo(0, cv.height);
    for (let i = 0; i < 256; i++) {
      ctx.lineTo(i / 255 * cv.width, cv.height - Math.min(1, bins[c][i] / max) * (cv.height - 4));
    }
    ctx.lineTo(cv.width, cv.height); ctx.fill();
  });
  ctx.globalCompositeOperation = "source-over";
}

// ---------------------------------------------------------------------------
// edit state
// ---------------------------------------------------------------------------
const saveEdit = debounce(async () => {
  if (!state.current) return;
  const id = state.current.id, edit = clone(state.edit);
  try { await api("PUT", `/api/media/${id}/edit`, edit); } catch (e) { toast("Could not save: " + e.message, "error"); }
}, 400);

function editChanged() {
  syncControls();
  playerEditChanged();
  requestPreview();
  saveEdit();
  updateInfo();
}

// ---------------------------------------------------------------------------
// controls panel
// ---------------------------------------------------------------------------
const controls = [];   // {sync()} objects refreshed from state.edit

function slider(path, label, min = -100, max = 100, step = 1, opts = {}) {
  const wrap = document.createElement("div");
  wrap.className = "slider";
  wrap.innerHTML = `<label title="Double-click to reset">${label}</label><input type="number" step="${step}" min="${min}" max="${max}"><input type="range" min="${min}" max="${max}" step="${step}">`;
  const [lab, num, rng] = [wrap.children[0], wrap.children[1], wrap.children[2]];
  if (opts.track) rng.style.setProperty("--track", opts.track);
  // Grade sliders reset to neutral (0); everything else to the app default.
  const def = () => (path.startsWith("grade.") ? state.cfg.neutral_grade[path.slice(6)]
    : getPath(state.cfg.default_edit, path)) ?? 0;
  const set = (v) => {
    v = clamp(parseFloat(v), min, max);
    if (isNaN(v)) return;
    setPath(state.edit, path, v);
    (opts.onChange || editChanged)();
  };
  rng.addEventListener("input", () => set(rng.value));
  num.addEventListener("change", () => set(num.value));
  const reset = () => set(def());
  lab.addEventListener("dblclick", reset);
  rng.addEventListener("dblclick", reset);
  controls.push({
    sync() {
      if (!state.edit) return;
      const v = getPath(state.edit, path);
      rng.value = v; if (document.activeElement !== num) num.value = +(+v).toFixed(2);
      wrap.classList.toggle("changed", Math.abs(v - def()) > 1e-9);
    },
  });
  return wrap;
}

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

function section(id, title, open = false) {
  const d = el(`<details class="sec" data-sec="${id}"><summary>${title}</summary><div class="sec-body"></div></details>`);
  d.open = open;
  $("#sections").append(d);
  return $(".sec-body", d);
}

const ASPECTS = [
  ["original", "Original"], ["free", "Free"], ["16:9", "16:9"], ["9:16", "9:16 (Reels / TikTok)"],
  ["4:3", "4:3"], ["3:2", "3:2"], ["1:1", "1:1 (Square)"], ["4:5", "4:5 (Instagram)"],
  ["2.39:1", "2.39:1 (Cinema)"], ["21:9", "21:9"],
];

function aspectRatio(edit, item) {
  const a = edit.aspect;
  if (a === "free") return null;
  if (a === "original") { const f = frameSize(item, edit); return f.w / f.h; }
  const [w, h] = a.split(":").map(Number);
  return w / h;
}

function setAspect(a) {
  state.edit.aspect = a;
  const r = aspectRatio(state.edit, state.current);
  if (r) {
    const f = frameSize(state.current, state.edit);
    const rn = r * f.h / f.w;              // ratio in normalised units
    let w = 1, h = 1 / rn;
    if (h > 1) { h = 1; w = rn; }
    state.edit.crop = { x: (1 - w) / 2, y: (1 - h) / 2, w, h };
  }
  editChanged();
}

function buildPanel() {
  $("#sections").innerHTML = "";
  controls.length = 0;
  const g = (k) => `grade.${k}`;

  // Presets
  const pre = section("presets", "Presets", true);
  pre.append(el(`<div class="presets" id="presetGrid"></div>`));
  const saveP = el(`<button class="link" style="margin-top:6px">+ Save current look as preset</button>`);
  saveP.onclick = savePreset;
  pre.append(saveP);

  // Crop & rotate
  const cr = section("crop", "Crop &amp; Rotate");
  const asp = el(`<div class="field"><label>Aspect ratio</label><select id="aspectSel">${ASPECTS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select></div>`);
  $("select", asp).onchange = (e) => { setCropMode(true); setAspect(e.target.value); };
  cr.append(asp);
  const btns = el(`<div class="btn-grid">
      <button class="btn sm" data-a="rl" title="Rotate left">⟲ 90°</button>
      <button class="btn sm" data-a="rr" title="Rotate right">⟳ 90°</button>
      <button class="btn sm" data-a="fh" title="Flip horizontal">⇋ Flip</button>
      <button class="btn sm" data-a="fv" title="Flip vertical">⇵ Flip</button></div>`);
  btns.onclick = (e) => {
    const a = e.target.closest("button")?.dataset.a; if (!a) return;
    const ed = state.edit;
    if (a === "rl" || a === "rr") {
      ed.rotate = (ed.rotate + (a === "rr" ? 90 : 270)) % 360;
      ed.crop = { x: 0, y: 0, w: 1, h: 1 };
      if (ed.aspect !== "free" && ed.aspect !== "original") {
        const [w, h] = ed.aspect.split(":"); ed.aspect = `${h}:${w}`;
        if (!ASPECTS.some(([v]) => v === ed.aspect)) ed.aspect = "original";
        return setAspect(ed.aspect);
      }
    }
    if (a === "fh") ed.flip_h = !ed.flip_h;
    if (a === "fv") ed.flip_v = !ed.flip_v;
    editChanged();
  };
  cr.append(btns);
  cr.append(slider("straighten", "Straighten (°)", -45, 45, 0.1, {
    onChange: () => { if (!state.cropMode) setCropMode(true); editChanged(); },
  }));
  const lv = el(`<button class="btn sm" title="Find the horizon and level it">⟂ Auto-level horizon</button>`);
  lv.onclick = () => runAuto({ horizon: true, exposure: false });
  cr.append(lv);
  cr.append(el(`<p class="hint">Drag the box on the image to crop. Straightening zooms in just enough to hide the corners.</p>`));
  const rc = el(`<button class="btn sm">Reset crop &amp; rotation</button>`);
  rc.onclick = () => {
    Object.assign(state.edit, { rotate: 0, flip_h: false, flip_v: false, straighten: 0, aspect: "original", crop: { x: 0, y: 0, w: 1, h: 1 } });
    editChanged();
  };
  cr.append(rc);
  controls.push({ sync() { $("#aspectSel").value = state.edit.aspect; } });

  // Light
  const li = section("light", "Light", true);
  const ab = el(`<div class="btn-row"><button class="btn sm primary" title="Level the horizon and set exposure">✨ Auto fix</button>
      <button class="btn sm" title="Set exposure only">Auto exposure</button></div>`);
  ab.children[0].onclick = () => runAuto({ horizon: true, exposure: true });
  ab.children[1].onclick = () => runAuto({ horizon: false, exposure: true });
  li.append(ab);
  li.append(slider(g("exposure"), "Exposure (EV)", -3, 3, 0.05));
  li.append(slider(g("contrast"), "Contrast"));
  li.append(slider(g("highlights"), "Highlights"));
  li.append(slider(g("shadows"), "Shadows"));
  li.append(slider(g("whites"), "Whites"));
  li.append(slider(g("blacks"), "Blacks"));
  li.append(slider(g("dehaze"), "Dehaze"));
  li.append(slider(g("fade"), "Fade (matte)", 0, 100));

  // Colour
  const co = section("colour", "Colour", true);
  co.append(slider(g("temperature"), "Temperature", -100, 100, 1, { track: "linear-gradient(90deg,#3b82f6,#9ca3af,#f59e0b)" }));
  co.append(slider(g("tint"), "Tint", -100, 100, 1, { track: "linear-gradient(90deg,#22c55e,#9ca3af,#d946ef)" }));
  co.append(slider(g("vibrance"), "Vibrance"));
  co.append(slider(g("saturation"), "Saturation"));
  co.append(el(`<div class="sec-sub">Blues (sky &amp; water)</div>`));
  co.append(slider(g("blue_sat"), "Blues saturation", -100, 100, 1, { track: "linear-gradient(90deg,#6b7c8f,#1e90ff)" }));
  co.append(slider(g("blue_lum"), "Blues brightness", -100, 100, 1, { track: "linear-gradient(90deg,#0b2a55,#1e6fd0,#8fd3ff)" }));
  const hues = "linear-gradient(90deg,#f00,#ff0,#0f0,#0ff,#00f,#f0f,#f00)";
  co.append(el(`<div class="sec-sub">Split toning</div>`));
  co.append(slider(g("shadow_hue"), "Shadow hue", 0, 360, 1, { track: hues }));
  co.append(slider(g("shadow_sat"), "Shadow amount", 0, 100));
  co.append(slider(g("highlight_hue"), "Highlight hue", 0, 360, 1, { track: hues }));
  co.append(slider(g("highlight_sat"), "Highlight amount", 0, 100));
  co.append(el(`<div class="sec-sub">LUT (.cube)</div>`));
  const lutRow = el(`<div class="row"><select id="lutSel"></select>
      <label class="btn sm" title="Upload a .cube LUT, e.g. a D-Cinelike → Rec.709 LUT">Upload<input type="file" accept=".cube" hidden id="lutFile"></label>
      <button class="btn sm" id="lutDel" title="Delete this LUT">✕</button></div>`);
  co.append(lutRow);
  co.append(slider(g("lut_strength"), "LUT strength", 0, 100));
  co.append(el(`<p class="hint">Mini 3 Pro D-Cinelike footage: use the “D-Cinelike → BRM Blue” preset, or upload a .cube LUT here and fine-tune with the sliders.</p>`));
  $("#lutSel", lutRow).onchange = (e) => { state.edit.grade.lut = e.target.value || null; editChanged(); };
  $("#lutFile", lutRow).onchange = uploadLut;
  $("#lutDel", lutRow).onclick = deleteLut;
  controls.push({
    sync() {
      const sel = $("#lutSel");
      sel.innerHTML = `<option value="">None</option>` + state.luts.map((l) => `<option value="${l.id}">${escapeHtml(l.name)}</option>`).join("");
      sel.value = state.edit.grade.lut || "";
      $("#lutDel").disabled = !state.edit.grade.lut;
    },
  });

  // Effects
  const fx = section("effects", "Detail &amp; Effects");
  fx.append(slider("sharpen", "Sharpen", 0, 100));
  fx.append(slider("vignette", "Vignette", -100, 100, 1, { track: "linear-gradient(90deg,#000,#3a4553,#fff)" }));

  const sky = section("sky", "Sky (graduated filter)");
  sky.append(slider("grad.strength", "Strength", 0, 100));
  sky.append(slider("grad.height", "Fade out by (% from top)", 5, 100));
  const gs = el(`<div class="field"><label>Filter colour</label><div class="swatches" id="gradSw">
      <button class="swatch" data-c="#000000" style="background:#000" title="Neutral (darken)"></button>
      <button class="swatch" data-c="#1e4a8a" style="background:#1e4a8a" title="Deep blue (BRM)"></button>
      <button class="swatch" data-c="#0f6a80" style="background:#0f6a80" title="Teal"></button>
      <button class="swatch" data-c="#8a4a1e" style="background:#8a4a1e" title="Warm sunset"></button></div></div>`);
  gs.onclick = (e) => { const c = e.target.closest(".swatch")?.dataset.c; if (c) { state.edit.grad.color = c; editChanged(); } };
  sky.append(gs);
  sky.append(el(`<p class="hint">Darkens or deepens the top of the frame – great for bright, washed-out skies.</p>`));
  controls.push({ sync() { $$("#gradSw .swatch").forEach((b) => b.classList.toggle("on", b.dataset.c === state.edit.grad.color)); } });

  buildStampSection();
  buildLocationSection();
  buildVideoSection();

  // remember which sections are open
  $$(".sec").forEach((d) => {
    const key = "sec:" + d.dataset.sec;
    try { const v = localStorage.getItem(key); if (v !== null) d.open = v === "1"; } catch (_) { /* ignore */ }
    d.addEventListener("toggle", () => {
      try { localStorage.setItem(key, d.open ? "1" : "0"); } catch (_) { /* ignore */ }
      if (d.dataset.sec === "presets" && d.open) loadPresetThumbs();
    });
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function buildStampSection() {
  const st = section("stamp", "BRM Highlife Stamp", true);
  const tog = el(`<label class="toggle"><span>Add stamp</span><input type="checkbox" id="stampOn"></label>`);
  $("input", tog).onchange = (e) => { state.edit.stamp.enabled = e.target.checked; editChanged(); };
  st.append(tog);
  st.append(el(`<div class="stamp-preview"><img id="stampImg" alt="Stamp"></div>`));
  const pos = el(`<div class="field"><label>Position</label><div class="pos-grid">
      <button data-p="tl">↖</button><button data-p="tc">↑</button><button data-p="tr">↗</button>
      <button data-p="bl">↙</button><button data-p="bc">↓</button><button data-p="br">↘</button></div></div>`);
  pos.onclick = (e) => {
    const p = e.target.closest("button")?.dataset.p; if (!p) return;
    state.edit.stamp.position = p; editChanged();
  };
  st.append(pos);
  st.append(slider("stamp.size", "Size", 5, 90, 1));
  st.append(slider("stamp.margin", "Margin", 0, 20, 0.5));
  st.append(slider("stamp.opacity", "Opacity", 0, 100));
  st.append(slider("stamp.shadow", "Shadow", 0, 100));
  const sw = el(`<div class="field"><label>Colour</label><div class="swatches">
      <button class="swatch swatch-auto" data-c="auto" title="Auto: black or white, whichever stands out against the background">A</button>
      <button class="swatch" data-c="#ffffff" style="background:#fff" title="White"></button>
      <button class="swatch" data-c="#000000" style="background:#000" title="Black"></button>
      <button class="swatch" data-c="#d4af37" style="background:#d4af37" title="Gold"></button>
      <input type="color" id="stampColor" title="Custom colour"></div></div>`);
  sw.onclick = (e) => {
    const c = e.target.closest(".swatch")?.dataset.c; if (!c) return;
    state.edit.stamp.color = c; editChanged();
  };
  $("#stampColor", sw).oninput = (e) => { state.edit.stamp.color = e.target.value; editChanged(); };
  st.append(sw);
  const def = el(`<button class="btn sm" id="stampDefaults" title="Black, bottom-right, original size, margin and opacity">↺ Reset stamp to default</button>`);
  def.onclick = () => { state.edit.stamp = clone(state.cfg.default_edit.stamp); editChanged(); toast("Stamp reset to the BRM Highlife default"); };
  st.append(def);
  const up = el(`<div class="row" style="margin-top:8px"><label class="btn sm">Use a different logo…<input type="file" accept="image/png,image/jpeg" hidden id="stampFile"></label>
      <button class="link" id="stampReset">Restore BRM Highlife logo</button></div>`);
  $("#stampFile", up).onchange = uploadStamp;
  $("#stampReset", up).onclick = resetStamp;
  st.append(up);
  controls.push({
    sync() {
      const s = state.edit.stamp;
      $("#stampOn").checked = s.enabled;
      $$(".pos-grid button").forEach((b) => b.classList.toggle("on", b.dataset.p === s.position));
      $$("[data-sec=stamp] .swatch").forEach((b) => b.classList.toggle("on", b.dataset.c === s.color));
      if (s.color !== "auto") $("#stampColor").value = s.color;
      const img = $("#stampImg");
      const src = `/api/stamp?v=${state.stampVersion}`;
      if (img.getAttribute("src") !== src) img.src = src;
      $("#stampReset").hidden = !state.cfg.custom_stamp;
      $("#stampDefaults").disabled = JSON.stringify(s) === JSON.stringify(state.cfg.default_edit.stamp);
    },
  });
}

function buildLocationSection() {
  const ls = section("location", "Location Stamp", true);
  const tog = el(`<label class="toggle"><span>Add location</span><input type="checkbox" id="locOn"></label>`);
  $("input", tog).onchange = (e) => { state.edit.location.enabled = e.target.checked; editChanged(); };
  ls.append(tog);
  const txt = el(`<div class="field"><label>Text (bottom-left)</label><div class="row">
      <input id="locText" placeholder="City, Country" maxlength="80">
      <button class="btn sm" id="locDetect" title="Look up City, Country from the photo/video GPS">From GPS</button></div></div>`);
  $("#locText", txt).onchange = (e) => { state.edit.location.text = e.target.value.trim(); editChanged(); };
  $("#locDetect", txt).onclick = async () => {
    if (!state.current) return;
    try {
      await saveEdit.flush();
      const r = await api("POST", `/api/media/${state.current.id}/location`);
      state.edit.location = r.edit.location; editChanged(); toast(`Location: ${r.text}`);
    } catch (e) { toast(e.message, "error"); }
  };
  ls.append(txt);
  ls.append(slider("location.size", "Size", 0.5, 8, 0.1));
  ls.append(slider("location.margin", "Margin", 0, 20, 0.5));
  ls.append(slider("location.shadow", "Shadow", 0, 100));
  const sw = el(`<div class="field"><label>Colour</label><div class="swatches">
      <button class="swatch swatch-auto" data-c="auto" title="Auto: black or white, whichever stands out against the background">A</button>
      <button class="swatch" data-c="#ffffff" style="background:#fff" title="White"></button>
      <button class="swatch" data-c="#000000" style="background:#000" title="Black"></button></div></div>`);
  sw.onclick = (e) => {
    const c = e.target.closest(".swatch")?.dataset.c; if (!c) return;
    state.edit.location.color = c; editChanged();
  };
  ls.append(sw);
  controls.push({
    sync() {
      const l = state.edit.location;
      $("#locOn").checked = l.enabled;
      if (document.activeElement !== $("#locText")) $("#locText").value = l.text;
      $$("[data-sec=location] .swatch").forEach((b) => b.classList.toggle("on", b.dataset.c === l.color));
    },
  });
}

function buildVideoSection() {
  const vs = section("video", "Video", true);
  vs.append(el(`<div class="hint" id="videoInfo"></div>`));
  const sp = el(`<div class="field"><label>Speed</label><select id="speedSel">
      ${[0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4].map((v) => `<option value="${v}">${v}×${v < 1 ? " slow-mo" : v > 1 ? " faster" : ""}</option>`).join("")}</select></div>`);
  $("select", sp).onchange = (e) => { state.edit.video.speed = +e.target.value; editChanged(); updateTimeline(); };
  vs.append(sp);
  const mu = el(`<label class="chk"><input type="checkbox" id="muteChk"> Remove audio</label>`);
  $("input", mu).onchange = (e) => { state.edit.video.mute = e.target.checked; editChanged(); };
  vs.append(mu);
  vs.append(slider("video.stabilize", "Stabilise", 0, 100));
  vs.append(slider("video.denoise", "Noise reduction", 0, 100));
  vs.append(el(`<p class="hint">Stabilise and noise reduction show in “With edits” playback and the export, not in the still preview.</p>`));
  vs.append(el(`<p class="hint">Tip: slow-mo works best on 60/120 fps footage – export at 25 or 30 fps for smooth motion.</p>`));
  controls.push({
    sync() {
      const isVid = state.current?.kind === "video";
      $('[data-sec="video"]').hidden = !isVid;
      if (!isVid) return;
      const it = state.current, v = state.edit.video;
      $("#speedSel").value = String(v.speed);
      $("#muteChk").checked = v.mute;
      $("#muteChk").disabled = !it.has_audio;
      const hlg = it.color_transfer === "arib-std-b67";
      $("#videoInfo").innerHTML = `${it.width}×${it.height} · ${it.fps} fps · ${(it.codec || "").toUpperCase()} ${it.bit_depth}-bit · ${fmtTime(it.duration)}${it.has_audio ? "" : " · no audio"}`
        + (hlg ? `<br><span class="hint warn">HLG footage – add a HLG→Rec.709 LUT for normal-looking colour.</span>` : "");
    },
  });
}

function syncControls() {
  if (!state.edit) return;
  controls.forEach((c) => c.sync());
}

// ---------------------------------------------------------------------------
// presets
// ---------------------------------------------------------------------------
function allPresets() {
  const p = Object.entries(state.cfg.presets).map(([name, grade]) => ({ name, grade, user: false }));
  const u = Object.entries(state.cfg.settings.presets || {}).map(([name, grade]) => ({ name, grade, user: true }));
  return [...u, ...p];
}

function applyPreset(grade) {
  const keep = { lut: state.edit.grade.lut, lut_strength: state.edit.grade.lut_strength };
  state.edit.grade = { ...clone(state.cfg.neutral_grade), ...clone(grade), ...(grade.lut ? {} : keep) };
  editChanged();
}

function renderPresetGrid() {
  const grid = $("#presetGrid");
  grid.innerHTML = "";
  for (const p of allPresets()) {
    const b = el(`<button class="preset" title="${escapeHtml(p.name)}"><img alt=""><span>${escapeHtml(p.name)}</span>${p.user ? '<b class="x" title="Delete preset">×</b>' : ""}</button>`);
    b.onclick = (e) => {
      if (e.target.classList.contains("x")) return deletePreset(p.name);
      applyPreset(p.grade);
    };
    b._grade = p.grade;
    grid.append(b);
  }
  loadPresetThumbs();
}

let presetThumbFor = null;
async function loadPresetThumbs() {
  const item = state.current;
  if (!item || !$('[data-sec="presets"]').open) return;
  const key = item.id + JSON.stringify([state.edit.rotate, state.edit.flip_h, state.edit.flip_v, state.edit.straighten, state.edit.crop, state.t]);
  if (presetThumbFor === key) return;
  presetThumbFor = key;
  for (const b of $$("#presetGrid .preset")) {
    if (state.current !== item || presetThumbFor !== key) return;
    const edit = { ...clone(state.edit), grade: { ...clone(state.cfg.neutral_grade), ...b._grade }, stamp: { enabled: false }, vignette: 0, sharpen: 0 };
    try {
      const r = await fetch(`/api/media/${item.id}/preview`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ edit, size: 240, t: state.t }),
      });
      if (r.ok) $("img", b).src = URL.createObjectURL(await r.blob());
    } catch (_) { /* thumbnails are optional */ }
  }
}

async function savePreset() {
  const name = prompt("Name for this look:");
  if (!name) return;
  const presets = { ...(state.cfg.settings.presets || {}), [name.slice(0, 60)]: clone(state.edit.grade) };
  state.cfg.settings = await api("PUT", "/api/settings", { presets });
  presetThumbFor = null;
  renderPresetGrid();
  toast(`Saved preset “${name}”`);
}

async function deletePreset(name) {
  if (!confirm(`Delete preset “${name}”?`)) return;
  const presets = { ...(state.cfg.settings.presets || {}) };
  delete presets[name];
  state.cfg.settings = await api("PUT", "/api/settings", { presets });
  presetThumbFor = null;
  renderPresetGrid();
}

// ---------------------------------------------------------------------------
// LUTs & stamp
// ---------------------------------------------------------------------------
async function uploadLut(e) {
  const f = e.target.files[0]; e.target.value = "";
  if (!f) return;
  const fd = new FormData(); fd.append("file", f);
  try {
    const lut = await api("POST", "/api/luts", fd);
    state.luts = await api("GET", "/api/luts");
    state.edit.grade.lut = lut.id;
    editChanged();
    toast(`LUT “${lut.name}” added`);
  } catch (err) { toast(err.message, "error"); }
}

async function deleteLut() {
  const id = state.edit.grade.lut;
  const lut = state.luts.find((l) => l.id === id);
  if (!id || !confirm(`Delete LUT “${lut?.name}”? Items using it will lose it.`)) return;
  await api("DELETE", `/api/luts/${id}`);
  state.luts = await api("GET", "/api/luts");
  state.edit.grade.lut = null;
  editChanged();
}

async function uploadStamp(e) {
  const f = e.target.files[0]; e.target.value = "";
  if (!f) return;
  const fd = new FormData(); fd.append("file", f);
  try {
    await api("POST", "/api/stamp", fd);
    state.cfg.custom_stamp = true;
    state.stampVersion = Date.now();
    editChanged();
    toast("Stamp updated (a transparent PNG works best)");
  } catch (err) { toast(err.message, "error"); }
}

async function resetStamp() {
  await api("DELETE", "/api/stamp");
  state.cfg.custom_stamp = false;
  state.stampVersion = Date.now();
  editChanged();
}

// ---------------------------------------------------------------------------
// crop tool
// ---------------------------------------------------------------------------
function setCropMode(on) {
  if (state.cropMode === on) return;
  if (on) stopPlayback();
  state.cropMode = on;
  $("#cropToggle").classList.toggle("on", on);
  $("#cropLayer").hidden = true;
  if (on) $('[data-sec="crop"]').open = true;
  requestPreview();
}

function layoutCrop() {
  const layer = $("#cropLayer");
  layer.hidden = !state.cropMode || !state.frame;
  if (layer.hidden) return;
  const c = state.edit.crop, box = $("#cropBox");
  box.style.left = c.x * 100 + "%";
  box.style.top = c.y * 100 + "%";
  box.style.width = c.w * 100 + "%";
  box.style.height = c.h * 100 + "%";
}

function resizeCrop(s, handle, dx, dy, rn) {
  const min = 0.03;
  let w = s.w, h = s.h;
  if (handle.includes("e")) w = s.w + dx;
  if (handle.includes("w")) w = s.w - dx;
  if (handle.includes("s")) h = s.h + dy;
  if (handle.includes("n")) h = s.h - dy;
  w = Math.max(w, min); h = Math.max(h, min);
  const horiz = /[ew]/.test(handle), vert = /[ns]/.test(handle);
  if (rn) { if (horiz) h = w / rn; else w = h * rn; }
  const availW = handle.includes("w") ? s.x + s.w : handle.includes("e") ? 1 - s.x
    : 2 * Math.min(s.x + s.w / 2, 1 - s.x - s.w / 2);
  const availH = handle.includes("n") ? s.y + s.h : handle.includes("s") ? 1 - s.y
    : 2 * Math.min(s.y + s.h / 2, 1 - s.y - s.h / 2);
  if (w > availW) { w = availW; if (rn) h = w / rn; }
  if (h > availH) { h = availH; if (rn) w = h * rn; }
  const x = handle.includes("w") ? s.x + s.w - w : handle.includes("e") ? s.x : s.x + (s.w - w) / 2;
  const y = handle.includes("n") ? s.y + s.h - h : handle.includes("s") ? s.y : s.y + (s.h - h) / 2;
  if (!vert && !horiz) return s;
  return { x, y, w, h };
}

function initCropDrag() {
  const box = $("#cropBox");
  let drag = null;
  box.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    const layer = $("#cropLayer").getBoundingClientRect();
    drag = { handle: e.target.dataset.h || "move", x0: e.clientX, y0: e.clientY, start: { ...state.edit.crop }, W: layer.width, H: layer.height };
    box.setPointerCapture(e.pointerId);
  });
  box.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = (e.clientX - drag.x0) / drag.W, dy = (e.clientY - drag.y0) / drag.H;
    const s = drag.start;
    if (drag.handle === "move") {
      state.edit.crop = { ...s, x: clamp(s.x + dx, 0, 1 - s.w), y: clamp(s.y + dy, 0, 1 - s.h) };
    } else {
      const r = aspectRatio(state.edit, state.current);
      const rn = r ? r * state.frame.h / state.frame.w : null;
      state.edit.crop = resizeCrop(s, drag.handle, dx, dy, rn);
    }
    layoutCrop();
    updateInfo();
  });
  const end = () => { if (drag) { drag = null; saveEdit(); updateInfo(); } };
  box.addEventListener("pointerup", end);
  box.addEventListener("pointercancel", end);
}

// ---------------------------------------------------------------------------
// video timeline
// ---------------------------------------------------------------------------
function updateTimeline() {
  const it = state.current;
  const isVid = it?.kind === "video";
  $("#timeline").hidden = !isVid;
  $("#grabBtn").hidden = !isVid;
  if (!isVid) return;
  const d = it.duration || 1, v = state.edit.video;
  const a = v.trim_start, b = v.trim_end ?? d;
  $("#tlTrim").style.left = (a / d * 100) + "%";
  $("#tlTrim").style.width = ((b - a) / d * 100) + "%";
  $("#tlHead").style.left = (state.t / d * 100) + "%";
  $("#tlTime").textContent = fmtTime(state.t);
  const trimmed = a > 0 || v.trim_end != null;
  const outDur = (b - a) / v.speed;
  $("#tlRange").textContent = (trimmed ? `Trim ${fmtTime(a)} → ${fmtTime(b)}` : "Full clip") + ` · output ${fmtTime(outDur)}`;
}

function initTimeline() {
  const track = $("#tlTrack");
  let dragging = false;
  const seek = (e) => {
    const r = track.getBoundingClientRect();
    state.t = clamp((e.clientX - r.left) / r.width, 0, 1) * (state.current.duration || 0);
    updateTimeline();
    if (playerVisible()) seekVideoTo(state.t); else requestPreview();
  };
  track.addEventListener("pointerdown", (e) => { dragging = true; track.setPointerCapture(e.pointerId); seek(e); });
  track.addEventListener("pointermove", (e) => dragging && seek(e));
  track.addEventListener("pointerup", () => { dragging = false; presetThumbFor = null; loadPresetThumbs(); });
  $("#setInBtn").onclick = setIn;
  $("#setOutBtn").onclick = setOut;
  $("#clearTrimBtn").onclick = () => { state.edit.video.trim_start = 0; state.edit.video.trim_end = null; editChanged(); updateTimeline(); };
  $("#grabBtn").onclick = grabFrame;
  initPlayer();
}

// ---------------------------------------------------------------------------
// automatic adjustments
// ---------------------------------------------------------------------------
async function runAuto({ horizon, exposure }) {
  if (!state.current) return;
  const item = state.current;
  $("#spinner").hidden = false;
  try {
    const r = await api("POST", `/api/media/${item.id}/auto`, { edit: state.edit, horizon, exposure, t: state.t });
    if (state.current !== item) return;
    const msgs = [];
    if (horizon) {
      if (r.horizon_found) { state.edit.straighten = r.straighten; msgs.push(`horizon levelled (${r.straighten}°)`); }
      else msgs.push(r.top_down ? "camera pointing down – no horizon to level" : "no clear horizon found");
    }
    if (exposure) { state.edit.grade.exposure = r.exposure; msgs.push(`exposure ${r.exposure > 0 ? "+" : ""}${r.exposure.toFixed(2)} EV`); }
    editChanged();
    toast("Auto: " + msgs.join(", "));
  } catch (e) { toast(e.message, "error"); } finally { $("#spinner").hidden = true; }
}

// ---------------------------------------------------------------------------
// video playback
// ---------------------------------------------------------------------------
const player = { mode: "original", seq: 0, key: null, stale: false };
const videoEl = () => $("#previewVideo");
const playerVisible = () => !videoEl().hidden;

function setPlayStatus(text) { $("#playStatus").textContent = text || ""; }

// Map between the source clip's time and the playing file's time.
function toVideoTime(t) {
  if (player.mode === "original") return t;
  const v = state.edit.video;
  return Math.max(0, (t - v.trim_start) / v.speed);
}
function toSourceTime(vt) {
  if (player.mode === "original") return vt;
  const v = state.edit.video;
  return v.trim_start + vt * v.speed;
}
function seekVideoTo(t) {
  const vid = videoEl();
  if (vid.readyState >= 1) vid.currentTime = clamp(toVideoTime(t), 0, vid.duration || 0);
}

function showPlayer(on) {
  videoEl().hidden = !on;
  $("#previewImg").hidden = on;
  if (on) $("#cropLayer").hidden = true;
}

async function startPlayback() {
  const item = state.current;
  if (!item || item.kind !== "video") return;
  if (state.cropMode) setCropMode(false);
  const seq = ++player.seq;
  player.stale = false;
  setPlayStatus(player.mode === "edited" ? "Rendering preview with your edits…" : "Preparing video…");
  $("#playBtn").disabled = true;
  try {
    let st = await api("POST", `/api/media/${item.id}/playback`, { mode: player.mode, edit: state.edit });
    while (st.status === "queued" || st.status === "rendering") {
      if (seq !== player.seq) return;
      const pct = Math.round(st.progress * 100);
      setPlayStatus(player.mode === "edited" ? `Rendering preview with your edits… ${pct}%`
        : `Making a playable copy (H.265 / 10-bit clips need converting)… ${pct}%`);
      await new Promise((r) => setTimeout(r, 600));
      st = await api("GET", `/api/playback/${st.key}`);
    }
    if (seq !== player.seq) return;
    if (st.status !== "ready") throw new Error(st.error || "Preview was cancelled");
    const vid = videoEl();
    if (player.key !== st.key) {
      player.key = st.key;
      vid.src = st.url;
      await new Promise((res, rej) => {
        vid.onloadedmetadata = res;
        vid.onerror = () => rej(new Error("This browser can't play this video"));
      });
    }
    if (seq !== player.seq) return;
    showPlayer(true);
    const start = toVideoTime(state.t);
    vid.currentTime = start >= (vid.duration || 0) - 0.05 ? 0 : start;
    setPlayStatus(player.mode === "edited" ? "Preview with edits (lower resolution)" : "");
    await vid.play().catch(() => {});
  } catch (e) {
    setPlayStatus("");
    toast(e.message, "error");
  } finally {
    if (seq === player.seq) $("#playBtn").disabled = false;
  }
}

function stopPlayback(unload = false) {
  player.seq++;
  const vid = videoEl();
  vid.pause();
  if (unload) { player.key = null; vid.removeAttribute("src"); vid.load(); }
  if (playerVisible()) { showPlayer(false); requestPreview(); }
  setPlayStatus("");
  $("#playBtn").disabled = false;
}

function togglePlay() {
  const vid = videoEl();
  if (playerVisible() && !player.stale && vid.readyState >= 1) {
    vid.paused ? vid.play() : vid.pause();
  } else {
    startPlayback();
  }
}

// After an edit, an "edited" preview is out of date and a paused player hides the new still.
function playerEditChanged() {
  if (!playerVisible()) return;
  const vid = videoEl();
  if (player.mode === "edited") {
    player.stale = true;
    player.key = null;
    setPlayStatus("Edits changed – press Play to render them");
  }
  if (vid.paused) stopPlayback();
}

function initPlayer() {
  const vid = videoEl();
  vid.addEventListener("timeupdate", () => {
    if (!playerVisible()) return;
    state.t = clamp(toSourceTime(vid.currentTime), 0, state.current?.duration || 0);
    updateTimeline();
  });
  vid.addEventListener("play", () => { $("#playBtn").textContent = "❚❚ Pause"; });
  vid.addEventListener("pause", () => { $("#playBtn").textContent = "▶ Play"; });
  $("#playBtn").onclick = togglePlay;
  $("#playMode").onclick = (e) => {
    const m = e.target.closest("button")?.dataset.m;
    if (!m || m === player.mode) return;
    player.mode = m;
    player.key = null;
    $$("#playMode button").forEach((b) => b.classList.toggle("on", b.dataset.m === m));
    if (playerVisible()) startPlayback();
  };
}
function setIn() {
  const v = state.edit.video;
  v.trim_start = +state.t.toFixed(3);
  if (v.trim_end != null && v.trim_end <= v.trim_start) v.trim_end = null;
  editChanged(); updateTimeline();
}
function setOut() {
  const v = state.edit.video;
  if (state.t <= v.trim_start) return toast("Trim end must be after the start", "error");
  v.trim_end = +state.t.toFixed(3);
  editChanged(); updateTimeline();
}

async function grabFrame() {
  await saveEdit.flush();
  try {
    const r = await api("POST", "/api/export", { media_ids: [state.current.id], options: state.cfg.settings.export, frame_time: state.t });
    toast(`Saving frame at ${fmtTime(state.t)} → ${r.destination}`);
    pollJobs(true);
  } catch (e) { toast(e.message, "error"); }
}

// ---------------------------------------------------------------------------
// library
// ---------------------------------------------------------------------------
function visibleItems() {
  return state.items.filter((i) => state.filter === "all" || i.kind === state.filter);
}

function renderLibrary() {
  const box = $("#thumbs");
  box.innerHTML = "";
  for (const it of visibleItems()) {
    const t = el(`<div class="thumb" data-id="${it.id}" title="${escapeHtml(it.name)}">
        <img loading="lazy" src="/api/media/${it.id}/thumb" alt="">
        <span class="sel">✓</span>
        ${it.kind === "video" ? `<span class="kind">▶ ${fmtTime(it.duration).replace(/\.\d$/, "")}</span>` : ""}
        <div class="meta"><span>${escapeHtml(it.name)}</span>${it.source === "linked" ? "<span>NAS</span>" : ""}</div></div>`);
    t.classList.toggle("active", state.current?.id === it.id);
    t.classList.toggle("selected", state.selected.has(it.id));
    box.append(t);
  }
  $("#libEmpty").hidden = state.items.length > 0;
  $("#libFoot").hidden = state.selected.size === 0;
  $("#selCount").textContent = `${state.selected.size} selected`;
  $("#exportBtn").disabled = !state.current;
  $("#panel").classList.toggle("disabled", !state.current);
  $("#stageMsg").hidden = !!state.current;
  if (!state.current) {
    $("#previewImg").removeAttribute("src");
    $("#stageMsg").textContent = state.items.length ? "Pick something from the library." : "Upload or import some drone media to get started.";
  }
}

let lastClicked = null;
function initLibrary() {
  $("#thumbs").addEventListener("click", (e) => {
    const t = e.target.closest(".thumb"); if (!t) return;
    const id = t.dataset.id;
    if (e.target.classList.contains("sel") || e.metaKey || e.ctrlKey) {
      state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
    } else if (e.shiftKey && lastClicked) {
      const ids = visibleItems().map((i) => i.id);
      const [a, b] = [ids.indexOf(lastClicked), ids.indexOf(id)].sort((x, y) => x - y);
      ids.slice(a, b + 1).forEach((x) => state.selected.add(x));
    } else {
      selectItem(id);
    }
    lastClicked = id;
    renderLibrary();
  });
  $("#libFilter").onclick = (e) => {
    const f = e.target.dataset.f; if (!f) return;
    state.filter = f;
    $$("#libFilter button").forEach((b) => b.classList.toggle("on", b.dataset.f === f));
    renderLibrary();
  };
  $("#selectAllBtn").onclick = () => {
    const vis = visibleItems();
    const all = vis.every((i) => state.selected.has(i.id));
    vis.forEach((i) => (all ? state.selected.delete(i.id) : state.selected.add(i.id)));
    renderLibrary();
  };
  $("#removeBtn").onclick = async () => {
    const n = state.selected.size;
    if (!confirm(`Remove ${n} item(s) from the library?\n\nUploaded copies are deleted. Files imported from the NAS are left untouched.`)) return;
    for (const id of [...state.selected]) {
      await api("DELETE", `/api/media/${id}`).catch((e) => toast(e.message, "error"));
      state.selected.delete(id);
      if (state.current?.id === id) { state.current = null; state.edit = null; }
    }
    await loadLibrary();
  };
  $("#syncBtn").onclick = () => {
    if (!state.current) return toast("Open the item whose settings you want to copy first", "error");
    $("#syncTitle").textContent = `Apply settings to ${state.selected.size} item(s)`;
    $("#syncDlg").showModal();
  };
  $("#syncOk").onclick = async () => {
    const parts = $$("#syncDlg [data-part]").filter((c) => c.checked).map((c) => c.dataset.part);
    await saveEdit.flush();
    let n = 0;
    for (const id of state.selected) {
      if (id === state.current.id) continue;
      const target = await api("GET", `/api/media/${id}/edit`);
      await api("PUT", `/api/media/${id}/edit`, mergeParts(target, state.edit, parts));
      n++;
    }
    $("#syncDlg").close();
    toast(`Settings applied to ${n} item(s)`);
  };
}

function mergeParts(target, src, parts) {
  const out = clone(target);
  if (parts.includes("grade")) out.grade = clone(src.grade);
  if (parts.includes("effects")) { out.sharpen = src.sharpen; out.vignette = src.vignette; }
  if (parts.includes("stamp")) out.stamp = clone(src.stamp);
  if (parts.includes("geometry")) {
    for (const k of ["rotate", "flip_h", "flip_v", "straighten", "aspect", "crop"]) out[k] = clone(src[k]);
  }
  return out;
}

async function loadLibrary() {
  state.items = await api("GET", "/api/media");
  if (state.current && !state.items.some((i) => i.id === state.current.id)) state.current = null;
  renderLibrary();
  if (!state.current && state.items.length) await selectItem(state.items[0].id);
}

async function selectItem(id) {
  if (state.current?.id === id) return;
  await saveEdit.flush();
  const item = state.items.find((i) => i.id === id);
  if (!item) return;
  state.current = item;
  state.edit = await api("GET", `/api/media/${id}/edit`);
  state.t = item.kind === "video" ? Math.min(state.edit.video.trim_start || 0, item.duration || 0) : 0;
  state.frame = null;
  presetThumbFor = null;
  stopPlayback(true);
  if (state.cropMode) { state.cropMode = false; $("#cropToggle").classList.remove("on"); $("#cropLayer").hidden = true; }
  renderLibrary();
  syncControls();
  updateTimeline();
  updateInfo();
  requestPreview();
  loadPresetThumbs();
}

function updateInfo() {
  const it = state.current;
  if (!it || !state.edit) { $("#infoLine").textContent = ""; return; }
  const f = frameSize(it, state.edit), c = state.edit.crop;
  const w = Math.round(f.w * c.w), h = Math.round(f.h * c.h);
  const extra = it.kind === "video" ? fmtTime(it.duration) : fmtBytes(it.bytes || 0);
  $("#infoLine").textContent = `${it.name} · ${it.width}×${it.height}${(w !== it.width || h !== it.height) ? ` → ${w}×${h}` : ""} · ${extra}`;
}

function navigate(delta) {
  const vis = visibleItems();
  if (!vis.length) return;
  const i = vis.findIndex((x) => x.id === state.current?.id);
  selectItem(vis[clamp(i + delta, 0, vis.length - 1)].id);
}

// ---------------------------------------------------------------------------
// upload / import
// ---------------------------------------------------------------------------
function uploadOne(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const fd = new FormData();
    fd.append("files", file, file.name);
    xhr.open("POST", "/api/media/upload");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }
      xhr.status < 300 ? resolve(body) : reject(new Error(body.detail || xhr.statusText));
    };
    xhr.onerror = () => reject(new Error("Upload failed – connection lost"));
    xhr.send(fd);
  });
}

async function uploadFiles(files) {
  files = [...files];
  if (!files.length) return;
  const box = $("#uploadProgress"), bar = $(".bar i", box), label = $("span", box);
  box.hidden = false;
  const total = files.reduce((s, f) => s + f.size, 0) || 1;
  let done = 0, firstId = null;
  for (const [i, f] of files.entries()) {
    label.textContent = `Uploading ${i + 1} of ${files.length}: ${f.name}`;
    try {
      const res = await uploadOne(f, (p) => { bar.style.width = ((done + p * f.size) / total * 100) + "%"; });
      res.errors?.forEach((m) => toast(m, "error"));
      if (res.added?.length && !firstId) firstId = res.added[0].id;
      state.items = [...(res.added || []), ...state.items];
      renderLibrary();
    } catch (e) { toast(`${f.name}: ${e.message}`, "error"); }
    done += f.size;
  }
  box.hidden = true; bar.style.width = "0";
  if (firstId) await selectItem(firstId);
}

function initDrop() {
  let depth = 0;
  const ov = $("#dropOverlay");
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  window.addEventListener("dragenter", (e) => { if (hasFiles(e)) { depth++; ov.hidden = false; } });
  window.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; ov.hidden = true; } });
  window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault(); depth = 0; ov.hidden = true;
    uploadFiles(e.dataTransfer.files);
  });
  $("#fileInput").onchange = (e) => { uploadFiles(e.target.files); e.target.value = ""; };
}

// ---------------------------------------------------------------------------
// folder browser (export destination + NAS import)
// ---------------------------------------------------------------------------
const browser = { kind: "output", root: "", path: "", resolve: null, picked: new Set() };

async function openBrowser(kind, root, path) {
  Object.assign(browser, { kind, root, path: path || "", picked: new Set() });
  $("#browseTitle").textContent = kind === "output" ? "Choose export folder" : "Import from NAS";
  $("#browseNewRow").hidden = kind !== "output";
  $("#browseOk").textContent = kind === "output" ? "Use this folder" : "Import all media here";
  $("#browseDlg").showModal();
  await renderBrowser();
  return new Promise((res) => { browser.resolve = res; });
}

async function renderBrowser() {
  const list = $("#browseList"), crumbs = $("#browseCrumbs");
  list.innerHTML = `<li class="empty">Loading…</li>`;
  const roots = browser.kind === "output" ? state.cfg.output_roots : state.cfg.input_roots;
  crumbs.innerHTML = "";
  const rootSel = el(`<select>${roots.map((r) => `<option ${r.name === browser.root ? "selected" : ""}>${escapeHtml(r.name)}</option>`).join("")}</select>`);
  rootSel.onchange = () => { browser.root = rootSel.value; browser.path = ""; renderBrowser(); };
  crumbs.append(rootSel);
  const parts = browser.path ? browser.path.split("/") : [];
  const top = el(`<button type="button">/</button>`);
  top.onclick = () => { browser.path = ""; renderBrowser(); };
  crumbs.append(top);
  parts.forEach((p, i) => {
    const b = el(`<button type="button">${escapeHtml(p)}</button>`);
    b.onclick = () => { browser.path = parts.slice(0, i + 1).join("/"); renderBrowser(); };
    crumbs.append(b, document.createTextNode("/"));
  });
  try {
    const q = new URLSearchParams({ kind: browser.kind, root: browser.root, path: browser.path });
    const res = await api("GET", "/api/browse?" + q);
    list.innerHTML = "";
    if (browser.path) {
      const up = el(`<li>⤴ ..</li>`);
      up.onclick = () => { browser.path = parts.slice(0, -1).join("/"); renderBrowser(); };
      list.append(up);
    }
    for (const d of res.dirs) {
      const li = el(`<li>📁 ${escapeHtml(d)}</li>`);
      li.onclick = () => { browser.path = [browser.path, d].filter(Boolean).join("/"); renderBrowser(); };
      list.append(li);
    }
    for (const f of res.files) {
      const li = el(`<li class="file"><input type="checkbox"> ${f.kind === "video" ? "🎬" : "🖼"} ${escapeHtml(f.name)}<span class="sz">${fmtBytes(f.bytes)}</span></li>`);
      $("input", li).onchange = (e) => {
        e.target.checked ? browser.picked.add(f.name) : browser.picked.delete(f.name);
        $("#browseOk").textContent = browser.picked.size ? `Import ${browser.picked.size} selected` : "Import all media here";
      };
      list.append(li);
    }
    if (!res.dirs.length && !res.files.length) list.append(el(`<li class="empty">Empty folder</li>`));
  } catch (e) {
    list.innerHTML = `<li class="empty">${escapeHtml(e.message)}</li>`;
  }
}

function initBrowser() {
  $("#browseOk").onclick = async () => {
    if (browser.kind === "output") {
      $("#browseDlg").close();
      browser.resolve?.({ root: browser.root, path: browser.path });
      return;
    }
    $("#browseOk").disabled = true;
    try {
      const res = await api("POST", "/api/media/import", { root: browser.root, path: browser.path, files: browser.picked.size ? [...browser.picked] : null });
      res.errors.forEach((m) => toast(m, "error"));
      toast(`Imported ${res.added.length} file(s)`);
      $("#browseDlg").close();
      await loadLibrary();
      if (res.added[0]) selectItem(res.added[0].id);
    } catch (e) { toast(e.message, "error"); }
    $("#browseOk").disabled = false;
  };
  $("#browseNew").onclick = async () => {
    const name = $("#browseNewName").value.trim();
    if (!name) return;
    const path = [browser.path, name].filter(Boolean).join("/");
    try {
      await api("POST", "/api/mkdir", { root: browser.root, path });
      browser.path = path; $("#browseNewName").value = "";
      renderBrowser();
    } catch (e) { toast(e.message, "error"); }
  };
  $("#browseDlg").addEventListener("close", () => { browser.resolve?.(null); browser.resolve = null; });
  $("#importBtn").onclick = () => {
    const roots = state.cfg.input_roots;
    if (!roots.length) return toast("No NAS import folders are set up yet – see README (SIRIS_INPUT_ROOTS).", "error");
    openBrowser("input", roots[0].name, "");
  };
}

// ---------------------------------------------------------------------------
// export
// ---------------------------------------------------------------------------
let exportScope = "current";

function openExport() {
  const o = state.cfg.settings.export;
  $("#exRoot").innerHTML = state.cfg.output_roots.map((r) => `<option value="${escapeHtml(r.name)}">${escapeHtml(r.name)}${r.available ? "" : " (not connected)"}</option>`).join("");
  $("#exRoot").value = o.root;
  $("#exFolder").value = o.folder;
  $("#exPhotoFmt").value = o.photo_format;
  $("#exQuality").value = o.jpeg_quality;
  setSizeSelect("#exPhotoSize", o.photo_size);
  $("#exCodec").innerHTML = Object.entries(state.cfg.codecs).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  $("#exCodec").value = o.video_codec;
  $("#exVQual").value = o.video_quality;
  setSizeSelect("#exVSize", o.video_size);
  $("#exFps").value = String(o.video_fps === "source" ? "source" : Math.round(o.video_fps));
  $("#exMeta").checked = o.keep_metadata;
  const g = state.cfg.gpu;
  $("#exGpuRow").hidden = !g.available;
  $("#exGpu").checked = o.use_gpu;
  $("#exGpuName").textContent = g.name || "NVIDIA";
  renderQuick();
  $("#exGps").checked = o.strip_gps;
  $("#exOverwrite").checked = o.overwrite;
  $("#exName").value = o.name_pattern;
  $("#exSelN").textContent = state.selected.size;
  $("#exAllN").textContent = state.items.length;
  setScope(state.selected.size > 1 ? "selected" : "current");
  updateExportHints();
  $("#exportDlg").showModal();
}

function exportIds() {
  return exportScope === "current" ? [state.current?.id].filter(Boolean)
    : exportScope === "selected" ? [...state.selected] : state.items.map((i) => i.id);
}

function renderQuick() {
  const row = $("#quickRow");
  row.innerHTML = "";
  const icons = { appletv: "📺", full: "⬛" };
  for (const [key, r] of Object.entries(state.cfg.recipes)) {
    const b = el(`<button type="button" class="btn"><span>${icons[key] || ""} ${escapeHtml(r.label)}</span><small>${escapeHtml(r.help)}</small></button>`);
    b.onclick = () => quickExport(key);
    row.append(b);
  }
}

async function quickExport(recipe) {
  const ids = exportIds();
  if (!ids.length) return toast("Nothing to export", "error");
  await saveEdit.flush();
  try {
    const res = await api("POST", "/api/quick-export", { recipe, media_ids: ids, root: $("#exRoot").value, folder: $("#exFolder").value.trim() });
    if (res.skipped.length) toast(`Skipped ${res.skipped.length} item(s) that don't suit “${state.cfg.recipes[recipe].label}”`);
    if (!res.jobs.length) return;
    $("#exportDlg").close();
    toast(`${state.cfg.recipes[recipe].label}: exporting ${res.jobs.length} item(s) to ${res.destination}`);
    $("#jobsDrawer").hidden = false;
    pollJobs(true);
    if (state.current) { state.edit = await api("GET", `/api/media/${state.current.id}/edit`); syncControls(); requestPreview(); }
  } catch (e) { toast(e.message, "error"); }
}

function setScope(s) {
  exportScope = s;
  $$("#exportScope button").forEach((b) => b.classList.toggle("on", b.dataset.s === s));
}

// Size options: "original", a long edge ("3840"), or a fit-inside box ("1080x1350", "x1080").
function setSizeSelect(sel, value) {
  const s = $(sel), box = $(s.dataset.custom);
  value = String(value);
  const known = [...s.options].some((o) => o.value === value);
  s.value = known ? value : "custom";
  box.hidden = known;
  if (!known && value !== "original") {
    const [w, h] = value.includes("x") ? value.split("x") : [value, value];
    $$("input", box)[0].value = w; $$("input", box)[1].value = h;
  }
}

function sizeValue(sel) {
  const s = $(sel);
  if (s.value !== "custom") return s.value;
  const [w, h] = $$("input", $(s.dataset.custom)).map((i) => i.value.trim());
  return w || h ? `${w}x${h}` : "original";
}

function outputDims(w, h, spec, even) {
  let k = 1;
  if (/^\d+$/.test(spec)) k = +spec / Math.max(w, h);
  else if (spec.includes("x")) {
    const [bw, bh] = spec.split("x").map((v) => (v ? +v : null));
    const ks = [bw && bw / w, bh && bh / h].filter(Boolean);
    if (ks.length) k = Math.min(...ks);
  }
  let ow = Math.max(2, Math.round(w * k)), oh = Math.max(2, Math.round(h * k));
  if (even) { ow -= ow % 2; oh -= oh % 2; }
  return `${ow} × ${oh} px`;
}

function updateExportHints() {
  for (const sel of ["#exPhotoSize", "#exVSize"]) $($(sel).dataset.custom).hidden = $(sel).value !== "custom";
  const it = state.current;
  if (it && state.edit) {
    const f = frameSize(it, state.edit), c = state.edit.crop;
    const w = f.w * c.w, h = f.h * c.h;
    $("#exPhotoResult").textContent = it.kind === "photo" ? `This photo → ${outputDims(w, h, sizeValue("#exPhotoSize"), false)}` : "";
    $("#exVideoResult").textContent = it.kind === "video" ? `This video → ${outputDims(w, h, sizeValue("#exVSize"), true)}` : "";
  }
  const stem = (it?.name || "DJI_0001.JPG").replace(/\.[^.]+$/, "");
  const today = new Date().toISOString().slice(0, 10);
  const example = ($("#exName").value.trim() || "{name}").replaceAll("{name}", stem)
    .replaceAll("{date}", today).replaceAll("{n}", "001").replace(/[\\/:*?"<>|]/g, "");
  $("#exNameExample").textContent = example + (it?.kind === "video" ? ".mp4" : ".jpg");
  $("#exQualOut").textContent = $("#exQuality").value;
  $("#exQualRow").hidden = $("#exPhotoFmt").value !== "jpeg";
  const root = state.cfg.output_roots.find((r) => r.name === $("#exRoot").value);
  const folder = $("#exFolder").value.trim().replace(/^\/+|\/+$/g, "");
  $("#exDestHint").textContent = root ? `Saves to ${root.path}${folder ? "/" + folder : ""}` + (root.available ? "" : " – this share is not reachable right now") : "";
  $("#exDestHint").classList.toggle("warn", !!root && !root.available);
}

function exportOptions() {
  return {
    root: $("#exRoot").value, folder: $("#exFolder").value.trim(),
    photo_format: $("#exPhotoFmt").value, jpeg_quality: +$("#exQuality").value,
    photo_size: sizeValue("#exPhotoSize"), video_codec: $("#exCodec").value,
    video_quality: $("#exVQual").value, video_size: sizeValue("#exVSize"), video_fps: $("#exFps").value,
    keep_metadata: $("#exMeta").checked, strip_gps: $("#exGps").checked,
    overwrite: $("#exOverwrite").checked, name_pattern: $("#exName").value, use_gpu: $("#exGpu").checked,
  };
}

function initExport() {
  $("#exportBtn").onclick = openExport;
  $("#exportScope").onclick = (e) => { const s = e.target.closest("button")?.dataset.s; if (s) setScope(s); };
  ["#exPhotoFmt", "#exQuality", "#exRoot", "#exFolder", "#exName", "#exPhotoSize", "#exVSize", "#exPhotoCustom", "#exVideoCustom"]
    .forEach((s) => $(s).addEventListener("input", updateExportHints));
  $("#exBrowse").onclick = async () => {
    $("#exportDlg").close();
    const pick = await openBrowser("output", $("#exRoot").value, $("#exFolder").value.trim());
    $("#exportDlg").showModal();
    if (pick) { $("#exRoot").value = pick.root; $("#exFolder").value = pick.path; updateExportHints(); }
  };
  $("#exStart").onclick = async () => {
    const ids = exportIds();
    if (!ids.length) return toast("Nothing to export", "error");
    await saveEdit.flush();
    try {
      const res = await api("POST", "/api/export", { media_ids: ids, options: exportOptions() });
      state.cfg.settings.export = { ...state.cfg.settings.export, ...exportOptions() };
      $("#exportDlg").close();
      toast(`Exporting ${res.jobs.length} item(s) to ${res.destination}`);
      $("#jobsDrawer").hidden = false;
      pollJobs(true);
    } catch (e) { toast(e.message, "error"); }
  };
}

// ---------------------------------------------------------------------------
// jobs
// ---------------------------------------------------------------------------
let jobTimer = null;
const seenDone = new Set();

async function pollJobs(force) {
  clearTimeout(jobTimer);
  try { state.jobs = await api("GET", "/api/jobs"); } catch (_) { /* server restarting */ }
  renderJobs();
  const active = state.jobs.some((j) => j.status === "queued" || j.status === "running");
  if (active || force || !$("#jobsDrawer").hidden) jobTimer = setTimeout(() => pollJobs(false), active ? 800 : 3000);
}

function renderJobs() {
  const active = state.jobs.filter((j) => j.status === "queued" || j.status === "running").length;
  $("#jobsCount").hidden = !active;
  $("#jobsCount").textContent = active;
  const list = $("#jobsList");
  list.innerHTML = state.jobs.length ? "" : `<li class="empty">No exports yet</li>`;
  for (const j of [...state.jobs].reverse()) {
    const pct = Math.round(j.progress * 100);
    const st = { queued: "Waiting", running: `${pct}%`, done: "Done", error: "Failed", cancelled: "Cancelled" }[j.status];
    const li = el(`<li><div class="top"><span class="name">${({ frame: "🖼 frame · ", hdr: "◐ ", hyperlapse: "⏩ " })[j.kind] || ""}${escapeHtml(j.name)}</span>
        <span class="st ${j.status}">${st}${(j.status === "queued" || j.status === "running") ? ` <button class="link" data-cancel="${j.id}">cancel</button>` : ""}</span></div>
        ${j.status === "running" ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ""}
        ${j.output ? `<div class="out">${escapeHtml(j.output)}</div>` : ""}
        ${j.error ? `<div class="err">${escapeHtml(j.error)}</div>` : ""}</li>`);
    list.append(li);
    if (j.status === "done" && !seenDone.has(j.id)) {
      seenDone.add(j.id);
      if (Date.now() / 1000 - j.finished < 10) {
        toast(j.kind === "hdr" ? j.output : `Saved ${j.output.split(/[\\/]/).pop()}`);
        if (j.kind === "hdr") loadLibrary();
      }
    }
    if (j.status === "error" && !seenDone.has(j.id)) { seenDone.add(j.id); toast(`Export failed: ${j.name}`, "error"); }
  }
}

function initJobs() {
  $("#jobsBtn").onclick = () => { $("#jobsDrawer").hidden = !$("#jobsDrawer").hidden; pollJobs(); };
  $("#closeJobs").onclick = () => { $("#jobsDrawer").hidden = true; };
  $("#clearJobs").onclick = async () => { await api("DELETE", "/api/jobs"); pollJobs(); };
  $("#jobsList").onclick = async (e) => {
    const id = e.target.dataset.cancel;
    if (id) { await api("POST", `/api/jobs/${id}/cancel`); pollJobs(); }
  };
}

// ---------------------------------------------------------------------------
// misc wiring
// ---------------------------------------------------------------------------
function setBefore(on) {
  if (state.before === on || !state.current) return;
  if (on) stopPlayback();
  state.before = on;
  $("#beforeBadge").hidden = !on;
  $("#beforeBtn").classList.toggle("on", on);
  requestPreview();
}

function initViewer() {
  const bb = $("#beforeBtn");
  bb.addEventListener("pointerdown", () => setBefore(true));
  ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => bb.addEventListener(ev, () => setBefore(false)));
  $("#cropToggle").onclick = () => setCropMode(!state.cropMode);
  $("#prevBtn").onclick = () => navigate(-1);
  $("#nextBtn").onclick = () => navigate(1);
  $("#copyBtn").onclick = () => { state.clipboard = clone(state.edit); $("#pasteBtn").disabled = false; toast("Settings copied – open another item and press Paste"); };
  $("#pasteBtn").onclick = () => {
    if (!state.clipboard) return;
    state.edit = mergeParts(state.edit, state.clipboard, ["grade", "effects", "stamp"]);
    editChanged();
  };
  $("#resetBtn").onclick = () => {
    if (!confirm("Reset all edits on this item?")) return;
    state.edit = { ...clone(state.cfg.default_edit), stamp: clone(state.edit.stamp) };
    editChanged(); updateTimeline();
  };
  $("#defaultBtn").onclick = async () => {
    const d = mergeParts(state.cfg.default_edit, state.edit, ["grade", "effects", "stamp"]);
    state.cfg.settings = await api("PUT", "/api/settings", { default_edit: d });
    toast("New uploads will start with this look and stamp");
  };

  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, select, textarea") || $("dialog[open]")) return;
    if (e.key === "b" || e.key === "\\") { setBefore(true); e.preventDefault(); }
    else if (e.key === "c") setCropMode(!state.cropMode);
    else if (e.key === "Escape") { setCropMode(false); stopPlayback(); }
    else if (e.key === " " && state.current?.kind === "video") { e.preventDefault(); togglePlay(); }
    else if (e.key === "ArrowLeft") navigate(-1);
    else if (e.key === "ArrowRight") navigate(1);
    else if (e.key === "i" && state.current?.kind === "video") setIn();
    else if (e.key === "o" && state.current?.kind === "video") setOut();
  });
  document.addEventListener("keyup", (e) => { if (e.key === "b" || e.key === "\\") setBefore(false); });
  window.addEventListener("resize", debounce(() => state.current && requestPreview(), 250));
  window.addEventListener("beforeunload", () => saveEdit.flush());
}

async function init() {
  state.cfg = await api("GET", "/api/config");
  state.luts = await api("GET", "/api/luts");
  $("#appVersion").textContent = "v" + state.cfg.version;
  buildPanel();
  renderPresetGrid();
  initLibrary(); initDrop(); initBrowser(); initExport(); initJobs(); initViewer(); initCropDrag(); initTimeline();
  initAutomation(); initBatchTools(); showGpu();
  const unavailable = state.cfg.output_roots.filter((r) => !r.available);
  if (unavailable.length) toast(`Export folder “${unavailable[0].name}” is not reachable – is the NAS mounted?`, "error");
  await loadLibrary();
  pollJobs();
}

// ---------------------------------------------------------------------------
// GPU, automation (watch folder), HDR merge, hyperlapse
// ---------------------------------------------------------------------------
function showGpu() {
  const g = state.cfg.gpu, b = $("#gpuBadge");
  b.hidden = false;
  b.classList.toggle("on", g.available);
  b.textContent = g.available ? `GPU: ${g.name}` : "CPU encoding";
  b.title = g.available
    ? `Video decoding${g.decode ? "" : " (CPU)"} and H.264/HEVC encoding run on the ${g.name}. Colour grading filters run on the CPU.`
    : `No usable NVIDIA GPU: ${g.reason}`;
}

let watchTimer = null, libCount = 0;
function fillRoots(sel, roots, value) {
  sel.innerHTML = roots.map((r) => `<option value="${escapeHtml(r.name)}">${escapeHtml(r.name)}${r.available ? "" : " (not connected)"}</option>`).join("");
  if (value) sel.value = value;
}

async function openAutomation() {
  const { settings: w, status } = await api("GET", "/api/watch");
  if (!state.cfg.input_roots.length) toast("No NAS import folders are set up yet – see README (SIRIS_INPUT_ROOTS).", "error");
  fillRoots($("#wRoot"), state.cfg.input_roots, w.root);
  fillRoots($("#wOutRoot"), state.cfg.output_roots, w.out_root || state.cfg.settings.export.root);
  $("#wEnabled").checked = w.enabled; $("#wFolder").value = w.folder; $("#wOutFolder").value = w.out_folder;
  $("#wPhotos").value = w.photos; $("#wVideos").value = w.videos;
  $("#wLevel").checked = w.auto_level; $("#wExposure").checked = w.auto_exposure;
  $("#wRecursive").checked = w.recursive; $("#wExisting").checked = w.process_existing;
  $("#wDelete").checked = w.delete_originals;
  renderWatchLog(status);
  $("#autoDlg").showModal();
}

function renderWatchLog(status) {
  const log = $("#wLog");
  const when = (t) => new Date(t * 1000).toLocaleTimeString();
  const rows = [
    ...status.errors.map((e) => `<li><span class="err">${when(e.at)} · ${escapeHtml(e.message)}</span></li>`),
    ...status.recent.map((r) => `<li><div class="top"><span class="name">${escapeHtml(r.name)}</span><span class="st ${r.error ? "error" : "done"}">${escapeHtml(r.error ? "failed" : (r.action || "") + (r.delete_after ? " · original deleted when done" : ""))}</span></div>${r.error ? `<div class="err">${escapeHtml(r.error)}</div>` : ""}</li>`),
  ];
  log.innerHTML = rows.length ? rows.join("") : `<li class="empty">${status.last_scan ? `Last checked ${when(status.last_scan)} – nothing new` : "Not running yet"}${status.waiting ? ` · ${status.waiting} file(s) still copying` : ""}</li>`;
  $("#autoDot").hidden = !state.cfg.settings.watch?.enabled;
}

function initAutomation() {
  $("#autoDot").hidden = !state.cfg.settings.watch?.enabled;
  $("#autoBtn").onclick = openAutomation;
  $("#wSave").onclick = async () => {
    try {
      const r = await api("PUT", "/api/watch", {
        enabled: $("#wEnabled").checked, root: $("#wRoot").value, folder: $("#wFolder").value.trim(),
        out_root: $("#wOutRoot").value, out_folder: $("#wOutFolder").value.trim(),
        photos: $("#wPhotos").value, videos: $("#wVideos").value, auto_level: $("#wLevel").checked,
        auto_exposure: $("#wExposure").checked, recursive: $("#wRecursive").checked,
        process_existing: $("#wExisting").checked, delete_originals: $("#wDelete").checked,
      });
      state.cfg.settings.watch = r.settings;
      renderWatchLog(r.status);
      toast(r.settings.enabled ? `Watching for new files${r.skipped_existing ? ` (${r.skipped_existing} existing file(s) left alone)` : ""}` : "Watch folder turned off");
    } catch (e) { toast(e.message, "error"); }
  };
  $("#wScan").onclick = async () => {
    await api("POST", "/api/watch/scan");
    setTimeout(async () => renderWatchLog((await api("GET", "/api/watch")).status), 1500);
  };
  // Pick up items the watch folder adds while the page is open.
  libCount = state.items.length;
  watchTimer = setInterval(async () => {
    if (!state.cfg.settings.watch?.enabled) return;
    try {
      const items = await api("GET", "/api/media");
      if (items.length !== libCount) { libCount = items.length; state.items = items; renderLibrary(); pollJobs(true); }
      if ($("#autoDlg").open) renderWatchLog((await api("GET", "/api/watch")).status);
    } catch (_) { /* server restarting */ }
  }, 15000);
}

function selectedPhotos() {
  return state.items.filter((i) => state.selected.has(i.id) && i.kind === "photo");
}

function initBatchTools() {
  $("#hdrBtn").onclick = async () => {
    const photos = selectedPhotos();
    if (photos.length < 2) return toast("Select the 3 or 5 photos of one exposure bracket (AEB)", "error");
    try {
      await api("POST", "/api/hdr-merge", { media_ids: photos.map((p) => p.id) });
      toast(`Merging ${photos.length} brackets – the HDR photo will appear in the library`);
      $("#jobsDrawer").hidden = false; pollJobs(true);
    } catch (e) { toast(e.message, "error"); }
  };
  $("#hlBtn").onclick = () => {
    const photos = selectedPhotos();
    if (photos.length < 4) return toast("Select the photos of a sequence (at least 4)", "error");
    const lead = state.current && state.selected.has(state.current.id) ? state.current : photos[0];
    $("#hlInfo").textContent = `${photos.length} photos, in file-name order. Every frame gets the edit of “${lead.name}” (grade, crop, sky); the stamp is added once.`;
    $("#hlDlg").showModal();
  };
  $("#hlStart").onclick = async () => {
    const photos = selectedPhotos();
    const lead = state.current && state.selected.has(state.current.id) ? state.current : photos[0];
    await saveEdit.flush();
    try {
      const r = await api("POST", "/api/hyperlapse", {
        media_ids: photos.map((p) => p.id), edit_from: lead.id, fps: +$("#hlFps").value,
        recipe: $("#hlRecipe").value, stabilize: $("#hlStab").checked, deflicker: $("#hlFlicker").checked,
      });
      $("#hlDlg").close();
      toast(`Making hyperlapse → ${r.destination}`);
      $("#jobsDrawer").hidden = false; pollJobs(true);
    } catch (e) { toast(e.message, "error"); }
  };
}

init().catch((e) => toast("Failed to start: " + e.message, "error"));
