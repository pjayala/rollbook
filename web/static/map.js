// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Minimal slippy map: Web Mercator, raster tiles through the local /tiles
 * proxy, canvas overlay layers. No dependencies.
 *
 * Coordinates: "m" = normalised mercator, x,y in [0,1], y down.
 * A layer is any object with draw(ctx, map); map.toScreen() converts.
 */

const TILE = 256;
// All basemaps are OSM raster tiles; "dark" is the same tiles through a CSS
// filter on their own canvas (canvas ctx.filter isn't supported everywhere).
const BASEMAPS = {
  dark: { label: "Dark", src: "osm", attr: "© OpenStreetMap contributors" },
  osm: { label: "Light", src: "osm", attr: "© OpenStreetMap contributors" },
  none: { label: "None", src: null, attr: "" },
};

function merc(lat, lon) {
  const s = Math.sin(Math.max(-85.0511, Math.min(85.0511, lat)) * Math.PI / 180);
  return [(lon + 180) / 360, 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)];
}

/** Decode server compact track -> Float64Array [mx,my,...]; NaN pair = pen up. */
function decodeTrack(flat) {
  const out = new Float64Array(flat.length);
  let la = 0, lo = 0, k = 0;
  for (let i = 0; i < flat.length; i += 2) {
    if (i > 0 && flat[i] === 0 && flat[i + 1] === 0) { out[k++] = NaN; out[k++] = NaN; continue; }
    la += flat[i]; lo += flat[i + 1];
    const m = merc(la / 1e5, lo / 1e5);
    out[k++] = m[0]; out[k++] = m[1];
  }
  return out;
}

function bboxOf(pts) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (let i = 0; i < pts.length; i += 2) {
    const x = pts[i], y = pts[i + 1];
    if (x !== x) continue;
    if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y;
  }
  return [x0, y0, x1, y1];
}

/** squared distance from point p to segment ab, all in screen px */
function segDist2(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay, L = dx * dx + dy * dy;
  let t = L ? ((px - ax) * dx + (py - ay) * dy) / L : 0;
  t = t < 0 ? 0 : t > 1 ? 1 : t;
  const x = ax + t * dx - px, y = ay + t * dy - py;
  return x * x + y * y;
}

class SlippyMap {
  constructor(host, opt = {}) {
    this.host = host;
    host.innerHTML = "";
    host.classList.add("smap");
    this.tcanvas = document.createElement("canvas");  // tiles
    this.tcanvas.className = "smap-tiles";
    this.tctx = this.tcanvas.getContext("2d");
    this.canvas = document.createElement("canvas");  // overlays + events
    this.canvas.className = "smap-over";
    this.ctx = this.canvas.getContext("2d");
    host.appendChild(this.tcanvas);
    host.appendChild(this.canvas);
    this.ui = document.createElement("div");
    this.ui.className = "smap-ui";
    this.ui.innerHTML = `<button data-a="in" title="zoom in">+</button><button data-a="out" title="zoom out">−</button>` +
      (opt.fitButton !== false ? `<button data-a="fit" title="fit">⤢</button>` : "") +
      `<select title="basemap">${Object.entries(BASEMAPS).map(([k, b]) => `<option value="${k}">${b.label}</option>`).join("")}</select>`;
    host.appendChild(this.ui);
    this.attr = document.createElement("div");
    this.attr.className = "smap-attr";
    host.appendChild(this.attr);
    this.layers = [];
    this.tiles = new Map();
    this.cx = 0.5; this.cy = 0.5; this.z = 2;
    this.minZ = 2; this.maxZ = 18;
    this.handlers = {};
    this.home = null;
    this.setBasemap(localStorage.basemap || "dark");

    this.ui.querySelector("select").onchange = e => { localStorage.basemap = e.target.value; this.setBasemap(e.target.value); };
    this.ui.addEventListener("click", e => {
      const a = e.target.dataset && e.target.dataset.a;
      if (a === "in") this.zoomAt(this.w / 2, this.h / 2, 1);
      if (a === "out") this.zoomAt(this.w / 2, this.h / 2, -1);
      if (a === "fit" && this.home) this.fit(this.home);
    });
    this.ui.addEventListener("pointerdown", e => e.stopPropagation());
    this.ui.addEventListener("dblclick", e => e.stopPropagation());
    this.ui.addEventListener("wheel", e => e.stopPropagation());
    this._events();
    this._ro = new ResizeObserver(() => this.resize());
    this._ro.observe(host);
    this.resize();
  }

  on(name, fn) { this.handlers[name] = fn; return this; }
  emit(name, ...a) { const f = this.handlers[name]; return f && f(...a); }
  get W0() { return TILE * 2 ** this.z; }
  toScreen(mx, my) { const W = this.W0; return [(mx - this.cx) * W + this.w / 2, (my - this.cy) * W + this.h / 2]; }
  fromScreen(sx, sy) { const W = this.W0; return [this.cx + (sx - this.w / 2) / W, this.cy + (sy - this.h / 2) / W]; }
  /** visible bbox in mercator, padded by `pad` screen px */
  view(pad = 0) { const a = this.fromScreen(-pad, -pad), b = this.fromScreen(this.w + pad, this.h + pad); return [a[0], a[1], b[0], b[1]]; }

  setBasemap(b) {
    this.basemap = BASEMAPS[b] ? b : "dark";
    this.ui.querySelector("select").value = this.basemap;
    this.attr.textContent = BASEMAPS[this.basemap].attr;
    this.attr.hidden = !BASEMAPS[this.basemap].attr;
    this.host.dataset.basemap = this.basemap;
    this.redraw();
  }

  resize() {
    const r = this.host.getBoundingClientRect();
    if (!r.width || !r.height) return;
    this.dpr = window.devicePixelRatio || 1;
    this.w = r.width; this.h = r.height;
    for (const c of [this.canvas, this.tcanvas]) {
      c.width = Math.round(this.w * this.dpr);
      c.height = Math.round(this.h * this.dpr);
      c.style.width = this.w + "px";
      c.style.height = this.h + "px";
    }
    this.draw();
  }

  fit(b, pad = 28) {
    this.home = this.home || b;
    if (!this.w) { this._pendingFit = b; return; }
    const dx = Math.max(b[2] - b[0], 1e-7), dy = Math.max(b[3] - b[1], 1e-7);
    const z = Math.min(Math.log2((this.w - 2 * pad) / TILE / dx), Math.log2((this.h - 2 * pad) / TILE / dy));
    this.z = Math.max(this.minZ, Math.min(this.maxZ, z));
    this.cx = (b[0] + b[2]) / 2; this.cy = (b[1] + b[3]) / 2;
    this.redraw(); this.emit("view");
  }

  zoomAt(sx, sy, dz) {
    const [mx, my] = this.fromScreen(sx, sy);
    this.z = Math.max(this.minZ, Math.min(this.maxZ, this.z + dz));
    const W = this.W0;
    this.cx = mx - (sx - this.w / 2) / W;
    this.cy = my - (sy - this.h / 2) / W;
    this.redraw(); this.emit("view");
  }

  redraw() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = 0; this.draw(); });
  }

  draw() {
    if (!this.w) return;
    if (this._pendingFit) { const b = this._pendingFit; this._pendingFit = null; this.fit(b); }
    const tc = this.tctx;
    tc.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    tc.fillStyle = this.basemap === "none" ? "#0b0e12" : "#f2efe9";
    tc.fillRect(0, 0, this.w, this.h);
    if (BASEMAPS[this.basemap].src) this._drawTiles(tc);
    const ctx = this.ctx;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.clearRect(0, 0, this.w, this.h);
    for (const L of this.layers) {
      ctx.save();
      L.draw(ctx, this);
      ctx.restore();
    }
  }

  _drawTiles(ctx) {
    const tz = Math.max(0, Math.min(this.maxZ, Math.round(this.z)));
    const n = 2 ** tz, ts = TILE * 2 ** (this.z - tz);
    const [x0, y0] = this.fromScreen(0, 0), [x1, y1] = this.fromScreen(this.w, this.h);
    const ty0 = Math.max(0, Math.floor(y0 * n)), ty1 = Math.min(n - 1, Math.floor(y1 * n));
    const tx0 = Math.floor(x0 * n), tx1 = Math.floor(x1 * n);
    ctx.imageSmoothingEnabled = true;
    for (let ty = ty0; ty <= ty1; ty++) for (let tx = tx0; tx <= tx1; tx++) {
      const wx = ((tx % n) + n) % n;
      const [sx, sy] = this.toScreen(tx / n, ty / n);
      const img = this._tile(tz, wx, ty, true);
      if (img.complete && img.naturalWidth) { ctx.drawImage(img, sx, sy, ts + 0.6, ts + 0.6); continue; }
      // while loading, stretch the nearest already-loaded ancestor
      for (let k = 1; k <= 4 && tz - k >= 0; k++) {
        const p = this._tile(tz - k, wx >> k, ty >> k, false);
        if (p && p.complete && p.naturalWidth) {
          const f = 2 ** k, sub = TILE / f;
          ctx.drawImage(p, (wx % f) * sub, (ty % f) * sub, sub, sub, sx, sy, ts + 0.6, ts + 0.6);
          break;
        }
      }
    }
  }

  _tile(z, x, y, load) {
    const key = `${z}/${x}/${y}`;
    let img = this.tiles.get(key);
    if (img) { this.tiles.delete(key); this.tiles.set(key, img); return img; }  // LRU touch
    if (!load) return null;
    img = new Image();
    img.onload = () => this.redraw();
    img.src = `/tiles/osm/${key}.png`;
    this.tiles.set(key, img);
    if (this.tiles.size > 600) this.tiles.delete(this.tiles.keys().next().value);
    return img;
  }

  _events() {
    const c = this.canvas;
    let drag = null;
    const pos = e => { const r = c.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    c.addEventListener("pointerdown", e => {
      if (e.button !== 0) return;
      c.setPointerCapture(e.pointerId);
      const [x, y] = pos(e);
      drag = { x, y, cx: this.cx, cy: this.cy, moved: 0 };
      this.host.classList.add("dragging");
    });
    c.addEventListener("pointermove", e => {
      const [x, y] = pos(e);
      if (drag) {
        const W = this.W0;
        drag.moved = Math.max(drag.moved, Math.hypot(x - drag.x, y - drag.y));
        this.cx = drag.cx - (x - drag.x) / W;
        this.cy = Math.max(0, Math.min(1, drag.cy - (y - drag.y) / W));
        this.redraw();
        this.emit("hover", null, e);
        return;
      }
      this.emit("hover", { x, y }, e);
    });
    const up = e => {
      if (!drag) return;
      const [x, y] = pos(e), d = drag;
      drag = null;
      this.host.classList.remove("dragging");
      if (d.moved < 4) this.emit("click", { x, y }, e);
      else this.emit("view");
    };
    c.addEventListener("pointerup", up);
    c.addEventListener("pointercancel", up);
    c.addEventListener("pointerleave", e => { if (!drag) this.emit("hover", null, e); });
    c.addEventListener("wheel", e => {
      e.preventDefault();
      const [x, y] = pos(e);
      const dz = e.deltaMode === 1 ? -e.deltaY / 3 : -e.deltaY / 240;
      this.zoomAt(x, y, Math.max(-1, Math.min(1, dz)));
    }, { passive: false });
    c.addEventListener("dblclick", e => { const [x, y] = pos(e); this.zoomAt(x, y, e.shiftKey ? -1 : 1); });
  }

  destroy() { this._ro.disconnect(); }
}

// ------------------------------------------------------------------ layers

/** Frequency heatmap: each track adds once per pixel, then a colour ramp. */
class HeatLayer {
  constructor(tracks) { this.tracks = tracks; this.off = document.createElement("canvas"); this.lut = HeatLayer.ramp(); this.passes = null; }
  static ramp() {
    const stops = [[0, [70, 20, 140, 0]], [0.02, [90, 20, 160, 150]], [0.25, [200, 30, 90, 220]],
      [0.5, [255, 110, 0, 240]], [0.78, [255, 214, 0, 255]], [1, [255, 255, 255, 255]]];
    const lut = new Uint8ClampedArray(256 * 4);
    for (let v = 1; v < 256; v++) {
      const t = Math.pow(v / 255, 0.7);
      let j = 1; while (j < stops.length - 1 && stops[j][0] < t) j++;
      const [a, ca] = stops[j - 1], [b, cb] = stops[j], f = (t - a) / ((b - a) || 1);
      for (let k = 0; k < 4; k++) lut[v * 4 + k] = ca[k] + (cb[k] - ca[k]) * Math.max(0, Math.min(1, f));
    }
    return lut;
  }
  draw(ctx, map) {
    const W = Math.round(map.w * map.dpr), H = Math.round(map.h * map.dpr);
    const off = this.off;
    if (off.width !== W || off.height !== H) { off.width = W; off.height = H; this.octx = off.getContext("2d", { willReadFrequently: true }); }
    const o = this.octx;
    o.setTransform(1, 0, 0, 1, 0, 0);
    o.globalCompositeOperation = "source-over";
    o.clearRect(0, 0, W, H);
    const vb = map.view(8), vis = this.tracks.filter(t => t.bbox[2] >= vb[0] && t.bbox[0] <= vb[2] && t.bbox[3] >= vb[1] && t.bbox[1] <= vb[3]);
    // saturate after N overlapping tracks; N scales with how many are on screen
    const N = this.passes || Math.max(4, Math.min(80, Math.round(Math.sqrt(vis.length) * 2.2)));
    this.lastN = N;
    o.globalCompositeOperation = "lighter";
    o.strokeStyle = `rgba(255,255,255,${(1 / N).toFixed(4)})`;
    o.lineWidth = Math.max(1.2, Math.min(4, (map.z - 9) * 0.5)) * map.dpr;
    o.lineJoin = o.lineCap = "round";
    const Wd = map.W0 * map.dpr, ox = map.w * map.dpr / 2, oy = map.h * map.dpr / 2, cx = map.cx, cy = map.cy;
    for (const t of vis) {
      const p = t.pts;
      o.beginPath();
      let pen = false, lx = 0, ly = 0;
      for (let i = 0; i < p.length; i += 2) {
        if (p[i] !== p[i]) { pen = false; continue; }
        const x = (p[i] - cx) * Wd + ox, y = (p[i + 1] - cy) * Wd + oy;
        if (!pen) { o.moveTo(x, y); pen = true; lx = x; ly = y; continue; }
        if (Math.abs(x - lx) + Math.abs(y - ly) < 1) continue;
        o.lineTo(x, y); lx = x; ly = y;
      }
      o.stroke();
    }
    const img = o.getImageData(0, 0, W, H), d = img.data, lut = this.lut;
    // read the alpha channel: getImageData un-premultiplies, so RGB of a
    // white stroke is always 255 and only alpha carries the accumulation
    for (let i = 0; i < d.length; i += 4) {
      const v = d[i + 3];
      if (!v) continue;
      const j = v * 4;
      d[i] = lut[j]; d[i + 1] = lut[j + 1]; d[i + 2] = lut[j + 2]; d[i + 3] = lut[j + 3];
    }
    o.putImageData(img, 0, 0);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.drawImage(off, 0, 0);
  }
}

/** One line per track, coloured by t.color; t.hi = highlighted. */
class LinesLayer {
  constructor(tracks, opt = {}) { this.tracks = tracks; this.width = opt.width || 2; this.alpha = opt.alpha ?? 0.55; this.halo = opt.halo; }
  draw(ctx, map) {
    const vb = map.view(8);
    const vis = this.tracks.filter(t => t.bbox[2] >= vb[0] && t.bbox[0] <= vb[2] && t.bbox[3] >= vb[1] && t.bbox[1] <= vb[3]);
    ctx.lineJoin = ctx.lineCap = "round";
    const path = t => {
      const p = t.pts; ctx.beginPath(); let pen = false;
      for (let i = 0; i < p.length; i += 2) {
        if (p[i] !== p[i]) { pen = false; continue; }
        const [x, y] = map.toScreen(p[i], p[i + 1]);
        if (pen) ctx.lineTo(x, y); else { ctx.moveTo(x, y); pen = true; }
      }
    };
    ctx.globalAlpha = this.alpha;
    for (const t of vis) if (!t.hi) { path(t); ctx.strokeStyle = t.color || "#ff7a1a"; ctx.lineWidth = this.width; ctx.stroke(); }
    ctx.globalAlpha = 1;
    for (const t of vis) if (t.hi) {
      path(t);
      ctx.strokeStyle = "#000"; ctx.lineWidth = this.width + 4; ctx.globalAlpha = .6; ctx.stroke();
      ctx.globalAlpha = 1; ctx.strokeStyle = "#fff"; ctx.lineWidth = this.width + 1.5; ctx.stroke();
    }
  }
}

/** Tracks whose line passes within r screen px of (sx, sy), nearest first. */
function hitTracks(map, tracks, sx, sy, r = 7) {
  const [mx, my] = map.fromScreen(sx, sy), mr = r / map.W0, r2 = r * r, out = [];
  for (const t of tracks) {
    const b = t.bbox;
    if (mx < b[0] - mr || mx > b[2] + mr || my < b[1] - mr || my > b[3] + mr) continue;
    const p = t.pts; let best = Infinity, px = null, py = null;
    for (let i = 0; i < p.length; i += 2) {
      if (p[i] !== p[i]) { px = null; continue; }
      const [x, y] = map.toScreen(p[i], p[i + 1]);
      if (px !== null) { const d = segDist2(sx, sy, px, py, x, y); if (d < best) best = d; }
      else { const d = (x - sx) ** 2 + (y - sy) ** 2; if (d < best) best = d; }
      px = x; py = y;
    }
    if (best <= r2) out.push({ t, d: best });
  }
  return out.sort((a, b) => a.d - b.d).map(h => h.t);
}
