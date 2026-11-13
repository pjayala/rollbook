// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Profile page (#/profile) and the max HR it implies. */

// ------------------------------------------------------------------ profile

async function viewProfile() {
  const app = $("#app");
  const p = await api("profile");
  const yr = new Date().getFullYear();
  const field = (k, label, attrs, help = "") => `<label class="pfield"><span>${label}</span><input id="pf-${k}" ${attrs} value="${esc(p[k] ?? "")}">${help ? `<small class="muted">${help}</small>` : ""}</label>`;
  app.innerHTML = `
    <div class="head"><div><h1>Profile</h1><div class="muted">Used for calories, heart-rate zones and resting energy. Stored only on this computer (<code>data/profile.json</code>).</div></div></div>
    <div class="grid g2 top">
      <div class="panel"><h2>You</h2>
        <form id="pform" class="pform">
          ${field("name", "Name", `type="text" maxlength="80" autocomplete="name"`)}
          <label class="pfield"><span>Sex</span><select id="pf-sex"><option value="">–</option><option value="male">male</option><option value="female">female</option><option value="other">other</option></select>
            <small class="muted">used by the heart-rate and resting-energy formulas</small></label>
          ${field("birth_year", "Birth year", `type="number" min="1900" max="${yr}" step="1"`, "heart-rate calories, resting energy")}
          ${field("height_cm", "Height (cm)", `type="number" min="100" max="250" step="0.5"`, "resting energy and BMI")}
          ${field("weight_kg", "Weight (kg)", `type="number" min="25" max="300" step="0.1"`, "<b>needed for calories</b>")}
          ${field("hr_rest", "Resting HR (bpm)", `type="number" min="25" max="120" step="1"`, "optional")}
          ${field("ftp", "FTP (W)", `type="number" min="50" max="600" step="1"`, "optional threshold power for power zones and intensity; otherwise the one your training app used (ROUVY), or 95% of your best 20 min")}
          ${field("hr_max", "Max HR (bpm)", `type="number" min="100" max="240" step="1"`, "optional; empty = the suggestion below")}
          <label class="pfield"><span></span><span class="small"><input type="checkbox" id="pf-hr_all"> use this max HR for all past sessions too</span>
            <small class="muted">otherwise a change applies from today, and earlier sessions keep the one they had</small></label>
          <label class="pfield"><span>Suggest from</span><select id="pf-hr_months">${[3, 6, 12, 24, 36].map(n => `<option value="${n}">last ${n} months</option>`).join("")}</select>
            <small class="muted">of sessions with heart rate</small></label>
          <div class="hrsug" id="hr-sug" aria-live="polite"></div>
          <div class="toolbar"><button class="btn primary" type="submit" id="psave">Save</button><span id="pmsg" class="small"></span></div>
        </form>
        ${p.from_export ? `<div class="note">Prefilled from your Strava export's profile. Check it and save.</div>` : ""}
      </div>
      <div class="panel"><h2>Derived</h2><div id="pderived"></div>
        <h2 style="margin-top:18px">How calories are estimated</h2>
        <table class="small"><tbody>
          <tr><td class="l">Indoor ride with power</td><td class="l">watts × time ÷ 24% muscle efficiency (the most accurate)</td></tr>
          <tr><td class="l">Run, walk, hike</td><td class="l">ACSM oxygen-cost equations from speed and uphill grade × weight</td></tr>
          <tr><td class="l">Inline skate, outdoor ride</td><td class="l">MET by speed (Compendium of Physical Activities) × weight × time</td></tr>
          <tr><td class="l">Other</td><td class="l">a fixed MET per activity × weight × time</td></tr>
          <tr><td class="l">Heart-rate cross-check</td><td class="l">Keytel 2005 from avg HR, weight, age and sex (needs the birth year)</td></tr>
        </tbody></table>
        <div class="note">Strava's and the watches' own calorie numbers aren't used: without heart rate, Strava's estimate for skating is about twice the measured rate. All figures are gross (they include what you'd burn at rest during that time) and use your current weight for every session.</div>
      </div>
    </div>`;
  $("#pf-sex").value = p.sex || "";
  $("#pf-hr_months").value = String(p.hr_months || 12);
  // max-HR suggestion for the chosen window (previewed before saving)
  // one request per window; typing in Max HR only redraws (cur changes)
  let last = null, ask = 0;
  const sug = async (refetch = true) => {
    if (refetch || !last) { const n = ++ask, r = await api("hrmax?months=" + $("#pf-hr_months").value); if (n !== ask) return; last = r; }
    const r = last, e = r.estimate, box = $("#hr-sug");
    if (!e || !e.bpm) { box.innerHTML = `<span class="muted">No sessions with heart rate in that window.</span>`; return; }
    const top = e.top[0], cur = +$("#pf-hr_max").value || null;
    const spike = isNum(e.recorded) && e.recorded - e.bpm >= 4;
    box.innerHTML = `<div><b class="num">${e.bpm} bpm</b> suggested max HR <span class="muted">· the highest you held for 30 s in one session, ${fdate(dayOf(e.from))} → ${fdate(dayOf(e.to))} (${e.n} sessions)</span></div>
      <div class="muted small">On <a href="#/session/${top.id}">${fdate(dayOf(top.date))}</a> (${esc(top.type || "")})${e.top.slice(1).map(t => ` · next ${t.held} on <a href="#/session/${t.id}">${fdate(dayOf(t.date))}</a>`).join("")}.
        ${spike ? `The recorded peak, ${f0(e.recorded)} bpm, lasted under 30 s: most likely a sensor spike.` : ""}
        ${isNum(r.age) ? `Age formula (Tanaka): ${r.age} bpm, ±10 for any one person.` : ""}
        It is only as high as your hardest effort in the window; a race or an all-out interval gives the truest value.</div>
      ${cur && cur < e.bpm ? `<div class="small down">Your max HR (${cur}) is below what you held (${e.bpm}): zones read too high.</div>` : ""}
      ${S.readonly || cur === e.bpm ? "" : `<button class="btn small" type="button" id="hr-use">Use ${e.bpm} bpm</button>`}`;
    const b = $("#hr-use"); if (b) b.onclick = () => { $("#pf-hr_max").value = e.bpm; sug(false); $("#psave").focus(); };
  };
  $("#pf-hr_months").onchange = () => sug().catch(() => {});
  $("#pf-hr_max").oninput = () => { clearTimeout(sug.t); sug.t = setTimeout(() => sug(false).catch(() => {}), 150); };
  sug().catch(() => {});
  const derived = q => {
    const rows = [["Age", q.age ? `${q.age}` : "–"], ["BMI", isNum(q.bmi) ? q.bmi.toFixed(1) : "–"],
      ["Resting energy (BMR)", isNum(q.bmr) ? `${f0(q.bmr)} kcal/day <span class="muted">(Mifflin–St Jeor)</span>` : `<span class="muted">needs sex, birth year, height and weight</span>`],
      ["Max HR for zones", `${S.hrmax ?? "?"} bpm <span class="muted">(${hrSrc()})</span>`],
      ...(((S.hrmaxInfo || {}).history || []).length ? [["Max HR history", S.hrmaxInfo.history.slice().reverse().map(e =>
        `${e.from === "0000-01-01" ? "before" : `from ${fdate(dayOf(e.from))}`}: ${e.bpm ? `${e.bpm} bpm` : `<span class="muted">none (suggestion)</span>`}`).join("<br>")
        + `<div class="muted small">each session's zones use the value from its date</div>`]] : [])];
    $("#pderived").innerHTML = `<table><tbody>${rows.map(([k, v]) => `<tr><td class="l">${k}</td><td class="l">${v}</td></tr>`).join("")}</tbody></table>`;
  };
  derived(p);
  $("#pform").onsubmit = async e => {
    e.preventDefault();
    if (S.readonly) return;
    const body = {};
    for (const k of ["name", "sex", "birth_year", "height_cm", "weight_kg", "hr_rest", "hr_max", "ftp", "hr_months"]) body[k] = $("#pf-" + k).value.trim();
    if ($("#pf-hr_all").checked) body.hr_max_all = true;
    const msg = $("#pmsg"); msg.className = "small muted"; msg.textContent = "saving…";
    try {
      const r = await fetch("/api/profile", { method: "POST", headers: { "X-Rollbook": "1", "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.status);
      msg.className = "small up"; msg.textContent = "Saved. Calories and zones updated.";
      S.profile = j;
      S.acts = {};  // recompute calories in every view
      await loadHrmax();
      $("#pf-hr_all").checked = false;
      derived(j); setProfileBtn(j); sug().catch(() => {});
    } catch (err) { msg.className = "small down"; msg.textContent = "Could not save: " + err.message; }
  };
}
/** Max HR for zones from the server: the profile's, else the held peak of the last months. */
async function loadHrmax() {
  const r = await api("hrmax").catch(() => ({}));
  S.hrmaxInfo = r;
  S.hrmax = r.hrmax || 185;
}
/** Where a max HR came from; src given = a session's (as of its date), else today's. */
const hrSrc = src => { const r = S.hrmaxInfo || {}, x = src === undefined ? r.source : src, mo = r.months || 12;
  return x === "profile" ? (src === undefined ? "from your profile" : "your profile's on that date")
    : x === "estimate" ? (src === undefined ? `held 30 s, last ${mo} months` : `held 30 s in the ${mo} months before`)
    : x === "recorded" ? "highest recorded" : "default"; };
function setProfileBtn(p) {
  S.profile = p || null;  // weight for W/kg
  const b = $("#profile-btn"); if (!b) return;
  const name = p && p.name ? p.name.trim() : "";
  $("#profile-name").textContent = name || "Your profile";
  $("#profile-init").textContent = (name || "?").slice(0, 1).toUpperCase();
  $("#profile-sub").textContent = p && p.weight_kg ? `${f1(p.weight_kg)} kg${p.ftp ? ` · FTP ${f0(p.ftp)} W` : ""}` : "Add your weight";
  b.classList.toggle("warn", !(p && p.weight_kg));
  b.title = p && p.weight_kg ? "Your profile" : "Add your weight to estimate calories";
}
