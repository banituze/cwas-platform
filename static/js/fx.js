/* CWAS motion system. Original implementations, plain JS + CSS 3D, no libraries.
   Entrance orbit, gallery tunnel, liquid distortion and reveal, type reveal, 3D letters menu, five hover reveals
   (CSS), booking jerrycans, pointer tilt with border lamp, count-up numbers and growing bars. Every effect settles into a
   readable static state when the visitor prefers reduced motion. */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const A = () => (window.CWAS && window.CWAS.audio) || { play() {} };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const ease = t => 1 - Math.pow(1 - t, 3);
  const io = (cb, o) => ("IntersectionObserver" in window ? new IntersectionObserver(cb, o) : { observe: e => cb([{ isIntersecting: true, target: e }]), unobserve() {} });
  const safe = fn => { try { fn(); } catch (e) { if (window.console) console.warn("fx:", e); } };

  /* ── liquid reveal + type reveal + bars ── */
  function reveal() {
    /* dashboards show their content at once: no entrance animation on working pages */
    const els = $$("[data-reveal]"), bars = $$("[data-bars]");
    if (reduce) { els.forEach(e => e.classList.add("in", "done")); bars.forEach(b => b.classList.add("in")); return; }
    /* Scroll position, not IntersectionObserver: a target clipped to nothing (clip-path) is reported as not intersecting by
       Chrome, so an observer would never reveal it. Everything at or above the fold line is revealed, so a jump scroll is safe too. */
    const pending = new Set(els);
    const check = () => {
      const line = innerHeight * .94;
      pending.forEach(e => {
        if (e.getBoundingClientRect().top >= line) return;
        pending.delete(e); e.classList.add("in");
        const d = parseFloat(getComputedStyle(e).getPropertyValue("--d")) || 0; setTimeout(() => e.classList.add("done"), d + 1300);
      });
      if (!pending.size) { removeEventListener("scroll", check); removeEventListener("resize", check); }
    };
    addEventListener("scroll", check, { passive: true }); addEventListener("resize", check); check();
    const ob = io(es => es.forEach(en => { if (en.isIntersecting) { en.target.classList.add("in"); ob.unobserve(en.target); } }), { threshold: .3 });
    bars.forEach(b => ob.observe(b));
  }
  function splitType(el) {
    const text = el.textContent.trim(), frame = el.hasAttribute("data-frame"); let i = 0, framed = false;
    el.setAttribute("aria-label", text); el.textContent = "";
    text.split(/\s+/).forEach(word => {
      const w = document.createElement("span"); w.className = "tw"; w.setAttribute("aria-hidden", "true");
      Array.from(word).forEach(ch => { const c = document.createElement("span"); c.className = "tc"; c.textContent = ch; c.style.setProperty("--i", i++); w.appendChild(c); });
      el.appendChild(w); el.appendChild(document.createTextNode(" "));
      if (frame && !framed && /[.!?]$/.test(word)) { framed = true; const f = document.createElement("span"); f.className = "tframe"; f.setAttribute("aria-hidden", "true"); f.innerHTML = '<svg viewBox="0 0 48 48"><use href="#i-drop" style="fill:currentColor;stroke:none"/></svg>'; el.appendChild(f); el.appendChild(document.createTextNode(" ")); }
    });
  }
  /* exposed for the instant language switch (app.js), which re-splits translated words in place */
  window.CWASfx = Object.assign(window.CWASfx || {}, { splitType, splitLetters });
  function types() {
    const els = $$("[data-type]"); els.forEach(el => safe(() => splitType(el)));
    if (reduce) return els.forEach(e => e.classList.add("in"));
    const o = io(es => es.forEach(en => { if (en.isIntersecting) { en.target.classList.add("in"); o.unobserve(en.target); } }), { threshold: .2 });
    els.forEach(e => o.observe(e));
  }

  /* ── liquid distortion: turbulence displaces the element while the pointer is on it ── */
  function liquid() {
    const map = $("#liq-map"), turb = $("#liq-turb"); if (!map || reduce) return;
    let target = 0, cur = 0, raf = 0, phase = 0, active = null;
    const step = () => {
      cur += (target - cur) * .12; phase += .012;
      map.setAttribute("scale", cur.toFixed(2)); turb.setAttribute("baseFrequency", `${(0.008 + Math.sin(phase) * .003).toFixed(4)} ${(0.02 + Math.cos(phase * 1.3) * .006).toFixed(4)}`);
      if (Math.abs(target - cur) < .2 && target === 0) { map.setAttribute("scale", "0"); if (active) active.classList.remove("is-liquid"); active = null; raf = 0; return; }
      raf = requestAnimationFrame(step);
    };
    $$("[data-liquid]").forEach(el => {
      el.addEventListener("pointerenter", () => { if (active && active !== el) active.classList.remove("is-liquid"); active = el; el.classList.add("is-liquid"); target = 34; if (!raf) raf = requestAnimationFrame(step); });
      el.addEventListener("pointermove", e => { const r = el.getBoundingClientRect(); target = 26 + 30 * Math.abs((e.clientX - r.left) / r.width - .5) * 2; });
      el.addEventListener("pointerleave", () => { target = 0; if (!raf) raf = requestAnimationFrame(step); });
    });
  }

  /* ── 3D letters ── */
  function letters() {
    $$("[data-letters]").forEach(el => safe(() => splitLetters(el)));
  }
  function splitLetters(el) {
      const label = el.dataset.letters || el.textContent.trim(); el.setAttribute("aria-label", label); el.textContent = ""; el.classList.add("lts");
      /* letters are grouped per word and the words are joined by a real space, so a long label (French or Malagasy
         on a small phone) wraps between words instead of breaking in the middle of one */
      let i = 0;
      label.split(" ").forEach((word, w) => {
        if (w) el.appendChild(document.createTextNode(" "));
        const wrap = document.createElement("span"); wrap.className = "ltw"; wrap.setAttribute("aria-hidden", "true");
        Array.from(word).forEach(ch => { const s = document.createElement("span"); s.className = "lt"; s.style.setProperty("--i", i++); const b = document.createElement("b"), f = document.createElement("i"); b.textContent = ch; f.textContent = ch; s.append(b, f); wrap.appendChild(s); });
        el.appendChild(wrap);
      });
  }

  /* ── entrance orbit ── */
  function orbit(el) {
    const stage = $(".orbit-stage", el), cards = $$(".orb-card", el), n = cards.length; if (!n) return;
    let t0 = 0, last = 0, rot = 0, px = 0, py = 0, raf = 0, vis = false, started = false;
    const radius = () => clamp(el.clientWidth * .36, Math.min(150, el.clientWidth * .4), 430);  // a tighter orbit on the smallest phones
    const paint = (e, now) => {
      const rad = radius();
      cards.forEach((c, i) => {
        const s = ease(clamp(e * 1.7 - i * .13, 0, 1)), a = i * 360 / n + rot, r = a * Math.PI / 180;
        const x = Math.sin(r) * rad * s, z = Math.cos(r) * rad * s - (1 - s) * 260, y = Math.sin(i * 1.9 + now / 1800) * 14 * s, flip = (1 - s) * 180;
        c.style.transform = `translate3d(${x.toFixed(1)}px,${y.toFixed(1)}px,${z.toFixed(1)}px) rotateY(${(a * s + flip).toFixed(2)}deg) scale(${(.55 + .45 * s).toFixed(3)})`;
      });
      stage.style.transform = `rotateX(${(-5 - py * 7).toFixed(2)}deg) rotateY(${(px * 12).toFixed(2)}deg)`;
    };
    const loop = now => {
      if (!vis) { raf = 0; return; }
      const dt = Math.min(.05, (now - last) / 1000); last = now; rot += (11 + px * 34) * dt;
      paint(started ? clamp((now - t0) / 2000, 0, 1) : 0, now); raf = requestAnimationFrame(loop);
    };
    el.addEventListener("pointermove", ev => { const r = el.getBoundingClientRect(); px = clamp(((ev.clientX - r.left) / r.width - .5) * 2, -1, 1); py = clamp(((ev.clientY - r.top) / r.height - .5) * 2, -1, 1); });
    el.addEventListener("pointerleave", () => { px = 0; py = 0; });
    if (reduce) { el.classList.add("live"); paint(1, 0); return; }
    io(es => es.forEach(en => {
      vis = en.isIntersecting;
      if (vis) { if (!started) { started = true; t0 = performance.now(); el.classList.add("live"); A().play("success"); } last = performance.now(); if (!raf) raf = requestAnimationFrame(loop); }
    }), { threshold: .25 }).observe(el);
    paint(0, 0);
  }

  /* ── gallery tunnel: scroll flies the camera through the frames ── */
  function tunnel(el) {
    const frames = $$(".tn-frame", el), rings = $$(".tn-ring", el), bar = $("[data-tn-bar]", el), cnt = $("[data-tn-count]", el), n = frames.length; if (!n || reduce) return;
    const gap = 800, ringGap = 430, span = rings.length * ringGap; let ticking = false, lastIdx = -1;
    const update = () => {
      ticking = false; const r = el.getBoundingClientRect(), total = Math.max(1, el.offsetHeight - innerHeight), p = clamp(-r.top / total, 0, 1), cam = p * (n - 1) * gap, w = innerWidth;
      frames.forEach((f, i) => {
        const z = cam - i * gap, near = clamp((z + 1500) / 900, 0, 1) * clamp((420 - z) / 320, 0, 1), settle = clamp((z + gap) / gap, 0, 1);
        const ox = (i % 2 ? 1 : -1) * Math.min(w * .2, 300) * (1 - settle * .85), oy = (i % 3 - 1) * 40 * (1 - settle);
        f.style.transform = `translate3d(${ox.toFixed(1)}px,${oy.toFixed(1)}px,${z.toFixed(1)}px) rotateY(${((i % 2 ? -1 : 1) * (1 - settle) * 22).toFixed(2)}deg)`;
        f.style.opacity = near.toFixed(3); f.style.pointerEvents = near > .6 ? "auto" : "none";
      });
      rings.forEach((g, j) => {
        const z = (((cam - j * ringGap) % span) + span) % span - (span - 380), o = clamp((z + span * .8) / (span * .5), 0, 1) * clamp((300 - z) / 260, 0, 1) * .9;
        g.style.transform = `translateZ(${z.toFixed(1)}px)`; g.style.opacity = o.toFixed(3);
      });
      if (bar) bar.style.transform = `scaleX(${p.toFixed(4)})`;
      const idx = Math.round(p * (n - 1)); if (cnt && idx !== lastIdx) { cnt.textContent = `${idx + 1} / ${n}`; lastIdx = idx; }
    };
    const on = () => { if (!ticking) { ticking = true; requestAnimationFrame(update); } };
    addEventListener("scroll", on, { passive: true }); addEventListener("resize", on); update();
  }


  /* ── type line: the photo frame opens and closes as the block crosses the viewport ── */
  function typeline(el) {
    const fr = $(".tl-frame", el), img = $("img", fr); if (!fr) return;
    if (reduce) { fr.style.setProperty("--w", "2.6em"); fr.classList.add("open"); return; }
    let ticking = false;
    const update = () => {
      ticking = false; const r = el.getBoundingClientRect(), vh = innerHeight || 800, p = clamp((vh - r.top) / (vh + r.height), 0, 1);
      const open = ease(clamp((p - .2) / .25, 0, 1)) * (1 - ease(clamp((p - .72) / .22, 0, 1)));
      fr.style.setProperty("--w", (open * 2.7).toFixed(3) + "em"); fr.classList.toggle("open", open > .02);
      if (img) { img.style.setProperty("--s", (1.55 - .55 * open).toFixed(3)); img.style.setProperty("--y", ((p - .5) * -14).toFixed(2) + "%"); }
    };
    const on = () => { if (!ticking) { ticking = true; requestAnimationFrame(update); } };
    addEventListener("scroll", on, { passive: true }); addEventListener("resize", on); update();
  }

  /* ── grid deck: cards are dealt from a rotating stack into a 3D grid, which then dives through the camera ── */
  function griddeck(el) {
    const grid = $(".dk-grid", el), cards = $$(".dk-card", el), bar = $("[data-dk-bar]", el), n = cards.length; if (!n || reduce) return;
    const seed = cards.map((_, i) => ({ r: Math.sin(i * 12.9898) * 9, ox: Math.cos(i * 7.13) * 7 })); let ticking = false;
    const update = () => {
      ticking = false; const r = el.getBoundingClientRect(), total = Math.max(1, el.offsetHeight - innerHeight), p = clamp(-r.top / total, 0, 1), w = innerWidth;
      const cols = w < 560 ? 3 : 4, rows = Math.ceil(n / cols), cw = w < 560 ? 118 : w < 900 ? 190 : 226, ch = w < 560 ? 98 : w < 900 ? 138 : 164;
      const deal = clamp(p / .3, 0, 1), dive = clamp((p - .34) / .66, 0, 1) * .72, depth = 1000; // stops mid-dive with the cards still large in view, so the section never ends on an empty stage
      cards.forEach((c, i) => {
        const col = i % cols, row = Math.floor(i / cols), s = ease(clamp(deal * 1.9 - i * .075, 0, 1));
        const x = (col - (cols - 1) / 2) * cw * s + seed[i].ox * (1 - s), y = (row - (rows - 1) / 2) * ch * s - (1 - s) * i * 1.4;
        const z = -(1 - s) * i * 4 + Math.sin(col * .9 + row * 1.3 + dive * 5) * 46 * dive, zc = dive * depth + z;
        c.style.transform = `translate3d(${x.toFixed(1)}px,${y.toFixed(1)}px,${z.toFixed(1)}px) rotateZ(${(seed[i].r * (1 - s)).toFixed(2)}deg) rotateY(${((1 - s) * 180).toFixed(1)}deg)`;
        c.style.opacity = "1";
      });
      grid.style.transform = `translateZ(${(dive * depth).toFixed(1)}px) rotateX(${(10 + dive * 16).toFixed(2)}deg) rotateY(${(Math.sin(dive * 3.1) * 26).toFixed(2)}deg) rotateZ(${((1 - deal) * -18).toFixed(2)}deg)`;
      if (bar) bar.style.transform = `scaleX(${p.toFixed(4)})`;
    };
    const on = () => { if (!ticking) { ticking = true; requestAnimationFrame(update); } };
    addEventListener("scroll", on, { passive: true }); addEventListener("resize", on); update();
  }

  /* ── gallery rise: the stage holds while the photographs rise from below into an editorial field, one after another ── */
  function galleryRise(el) {
    const items = $$(".rg-item", el), imgs = items.map(f => $("img", f)); if (!items.length || reduce) return;
    el.classList.add("is-live");
    let ticking = false;
    const update = () => {
      ticking = false;
      const vh = innerHeight || 800, r = el.getBoundingClientRect(), total = Math.max(1, el.offsetHeight - vh), lead = vh * .2;
      const p = clamp((lead - r.top) / (total + lead), 0, 1), rise = vh * .9;
      items.forEach((f, i) => {
        const k = 1 - ease(clamp((p * 1.15 - i * .085) / .55, 0, 1));  // 1 = still below the stage, 0 = in place
        f.style.transform = k ? `translate3d(0,${(k * rise).toFixed(1)}px,${(-k * 240).toFixed(1)}px) rotateX(${(k * 14).toFixed(2)}deg)` : "none";
        f.style.opacity = clamp((1 - k) * 1.8, 0, 1).toFixed(3);
        if (imgs[i]) imgs[i].style.transform = k ? `scale(${(1 + k * .16).toFixed(4)})` : "none";
      });
    };
    const on = () => { if (!ticking) { ticking = true; requestAnimationFrame(update); } };
    addEventListener("scroll", on, { passive: true }); addEventListener("resize", on); update();
  }

  /* ── gallery depth: each real screen travels in from deep perspective and settles flat and sharp as it scrolls into view ── */
  function galleryDepth(el) {
    // each screen arrives from depth once, as it comes into view, then rests flat, sharp and whole
    const items = $$(".gd-item", el); if (!items.length || reduce || !("IntersectionObserver" in window)) return;
    el.classList.add("is-live");
    const io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { e.target.classList.add("is-in"); io.unobserve(e.target); } }), { rootMargin: "0px 0px 12% 0px" });
    items.forEach(f => io.observe(f));
  }


  /* ── jerrycans: real 20 L jerrycans fill from the bottom with the litres chosen; tap one to choose its total ── */
  function cans(el) {
    const form = el.closest("[data-book]") || document, radios = () => $$("input[name=litres]", form), list = $$(".can", el);  // the small 20 L cans
    const out = $("[data-cans-litres]", form), cnt = $("[data-cans-count]", form);
    const sync = () => {
      const c = radios().find(r => r.checked); if (!c) return; const l = +c.value;
      list.forEach((can, i) => can.style.setProperty("--f", clamp((l - i * 20) / 20, 0, 1).toFixed(3)));
      const big = $("[data-can-big]", form); if (big) big.style.setProperty("--f", clamp(l / (+el.dataset.max || 100), 0, 1).toFixed(3));
      const n = Math.round((l / 20) * 10) / 10;
      if (out) out.textContent = l + " L";
      if (cnt) cnt.textContent = n === 1 ? el.dataset.lOne : (el.dataset.lMany || "").replace("{n}", String(n).replace(".", ","));
    };
    form.addEventListener("change", sync);
    list.forEach(can => can.addEventListener("click", () => {
      const want = +can.dataset.can, r = radios(); if (!r.length) return;
      const best = r.reduce((a, b) => (Math.abs(+b.value - want) < Math.abs(+a.value - want) ? b : a));
      if (!best.checked) { best.checked = true; best.dispatchEvent(new Event("change", { bubbles: true })); A().play("tap"); }
    }));
    sync();
  }

  /* ── tilt with a lamp on the border, count-up numbers ── */
  function tilt() {
    if (reduce || matchMedia("(hover: none)").matches) return;
    $$(".kpi,[data-tilt]").forEach(el => {
      el.classList.add("glow", "tilt"); let raf = 0;
      el.addEventListener("pointermove", e => {
        if (el.hasAttribute("data-reveal") && !el.classList.contains("done")) return;
        const r = el.getBoundingClientRect(), x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
        cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { el.classList.add("on"); el.style.setProperty("--gx", x * 100 + "%"); el.style.setProperty("--gy", y * 100 + "%"); el.style.transform = `perspective(900px) rotateX(${((.5 - y) * 7).toFixed(2)}deg) rotateY(${((x - .5) * 9).toFixed(2)}deg) translateZ(0)`; });
      });
      el.addEventListener("pointerleave", () => { cancelAnimationFrame(raf); el.classList.remove("on"); el.style.transform = ""; });
    });
  }
  function counts() {
    const els = $$(".kpi b,[data-count]").filter(e => /^[^\d]*\d[\d,.\s]*[^\d]*$/.test(e.textContent.trim()) && !/[a-z]-?\d/i.test(e.textContent.trim()));
    if (reduce) return;
    const o = io(es => es.forEach(en => {
      if (!en.isIntersecting) return; const el = en.target; o.unobserve(el);
      const m = el.textContent.trim().match(/^(\D*)([\d][\d,.\s]*)(.*)$/); if (!m) return;
      const raw = m[2].replace(/[,\s]/g, ""), target = parseFloat(raw); if (!isFinite(target) || target === 0) return;
      const dec = (raw.split(".")[1] || "").length, comma = /,/.test(m[2]), t0 = performance.now();
      const fmt = v => { const s = v.toFixed(dec); return comma ? Number(s).toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec }) : s; };
      const tick = now => { const p = clamp((now - t0) / 1100, 0, 1); el.textContent = m[1] + fmt(target * ease(p)) + m[3]; if (p < 1) requestAnimationFrame(tick); else el.textContent = m[1] + m[2] + m[3]; };
      requestAnimationFrame(tick);
    }), { threshold: .4 });
    els.forEach(e => o.observe(e));
  }

  /* every bold page title rises letter by letter: page headings and section titles, on the site and in the dashboards */
  $$("main h1, main h2").forEach(h => { if (!h.children.length && !h.hasAttribute("data-type") && !h.closest("[data-notype],.gt-copy,dialog,.sr-only") && h.textContent.trim().length > 1) h.setAttribute("data-type", ""); });
  [reveal, types, liquid, letters, tilt, counts].forEach(f => safe(f));
  $$("[data-typeline]").forEach(e => safe(() => typeline(e)));
  $$("[data-griddeck]").forEach(e => safe(() => griddeck(e)));
  $$("[data-orbit]").forEach(e => safe(() => orbit(e)));
  $$("[data-tunnel]").forEach(e => safe(() => tunnel(e)));
  $$("[data-gallery-rise]").forEach(e => safe(() => galleryRise(e)));
  $$("[data-gallery-depth]").forEach(e => safe(() => galleryDepth(e)));
  $$("[data-cans]").forEach(e => safe(() => cans(e)));
  window.__fxReady = true;
})();
