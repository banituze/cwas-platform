/* Elements the server rendered, captured before any script changes the page. The instant language switch uses it to
   tell server content (updated in place) from content that scripts added (kept as it is). */
window.CWAS_SERVER_NODES = new WeakSet(document.body ? document.body.querySelectorAll("*") : []);
/* CWAS front-end. Plain JavaScript, no framework. Everything is attached by data-attributes so the
   Content-Security-Policy can forbid inline scripts. */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const csrf = () => ($('meta[name="csrf-token"]') || {}).content || "";
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const post = (url, body) => fetch(url, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() }, body: JSON.stringify(body) }).then(r => r.json());
  const store = { get: (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } } };

  /* ── sound engine: every sound is synthesised, nothing to download ── */
  const Audio = (() => {
    let ctx = null, master = null;
    const on = () => store.get("cwas_sound", "1") !== "0";
    const vol = () => Math.max(0, Math.min(1, parseFloat(store.get("cwas_vol", "0.7"))));
    const ensure = () => {
      if (!ctx) { const C = window.AudioContext || window.webkitAudioContext; if (!C) return null; ctx = new C(); master = ctx.createGain(); master.connect(ctx.destination); }
      if (ctx.state === "suspended") ctx.resume();
      master.gain.value = vol();
      return ctx;
    };
    const tone = (f, d = .1, type = "sine", g = .2, at = 0, to = 0) => {
      const t0 = ctx.currentTime + at, o = ctx.createOscillator(), e = ctx.createGain();
      o.type = type; o.frequency.setValueAtTime(f, t0); if (to) o.frequency.exponentialRampToValueAtTime(to, t0 + d);
      e.gain.setValueAtTime(0.0001, t0); e.gain.exponentialRampToValueAtTime(g, t0 + .008); e.gain.exponentialRampToValueAtTime(0.0001, t0 + d);
      o.connect(e); e.connect(master); o.start(t0); o.stop(t0 + d + .03);
    };
    const noise = (d = .1, g = .1, at = 0, freq = 2000) => {
      const n = Math.floor(ctx.sampleRate * d), buf = ctx.createBuffer(1, n, ctx.sampleRate), data = buf.getChannelData(0);
      for (let i = 0; i < n; i++) data[i] = (Math.random() * 2 - 1) * (1 - i / n);
      const s = ctx.createBufferSource(), f = ctx.createBiquadFilter(), e = ctx.createGain(), t0 = ctx.currentTime + at;
      s.buffer = buf; f.type = "bandpass"; f.frequency.value = freq; e.gain.value = g; s.connect(f); f.connect(e); e.connect(master); s.start(t0);
    };
    const DTMF = { 1: [697, 1209], 2: [697, 1336], 3: [697, 1477], 4: [770, 1209], 5: [770, 1336], 6: [770, 1477], 7: [852, 1209], 8: [852, 1336], 9: [852, 1477], "*": [941, 1209], 0: [941, 1336], "#": [941, 1477] };
    const sounds = {
      tap: () => tone(1900, .03, "square", .05),
      key: k => { const f = DTMF[k]; if (f) { tone(f[0], .11, "sine", .09); tone(f[1], .11, "sine", .09); } else tone(1500, .05, "square", .05); },
      ring: () => { tone(440, .45, "sine", .09); tone(480, .45, "sine", .09); tone(440, .45, "sine", .09, 1.2); tone(480, .45, "sine", .09, 1.2); },
      connect: () => { tone(660, .07, "sine", .14); tone(880, .1, "sine", .14, .08); },
      receive: () => { tone(784, .09, "triangle", .16); tone(1047, .16, "triangle", .16, .1); },
      send: () => { noise(.16, .08, 0, 2400); tone(520, .09, "sine", .1, 0, 900); },
      error: () => { tone(220, .16, "sawtooth", .08); tone(170, .22, "sawtooth", .08, .15); },
      sms: () => { tone(1319, .12, "sine", .18); tone(1568, .12, "sine", .18, .13); tone(2093, .24, "sine", .18, .26); },
      notify: () => { tone(988, .14, "sine", .16); tone(1319, .28, "sine", .16, .14); },
      success: () => { tone(523, .1, "triangle", .16); tone(659, .1, "triangle", .16, .1); tone(784, .2, "triangle", .16, .2); },
      poweron: () => { tone(392, .12, "sine", .14); tone(523, .12, "sine", .14, .1); tone(784, .24, "sine", .14, .2); },
      poweroff: () => { tone(784, .12, "sine", .14); tone(523, .12, "sine", .14, .1); tone(330, .26, "sine", .14, .2); },
      vol: () => tone(1200, .05, "triangle", .12),
      print: () => { for (let i = 0; i < 24; i++) { noise(.05, .1, i * .15, i % 2 ? 900 : 1500); tone(180, .04, "square", .03, i * .15); } },
      tear: () => noise(.22, .18, 0, 3200),
    };
    return {
      on, ensure,
      play(name, ...a) { if (!on() || !ensure()) return; try { sounds[name](...a); } catch (e) { /* audio is optional */ } },
      set(v) { store.set("cwas_sound", v ? "1" : "0"); },
      unlock() { if (on()) ensure(); },  // browsers start audio only inside a gesture
    };
  })();

  /* ── toasts ── */
  const X = '<svg class="icon" aria-hidden="true"><use href="#i-x"/></svg>';
  const dismiss = el => { if (!el) return; el.style.transition = "opacity .35s, transform .35s"; el.style.opacity = "0"; el.style.transform = "translateY(-8px)"; setTimeout(() => el.remove(), 380); };
  const toast = (msg, kind = "ok", o = {}) => {
    const host = $("[data-toasts]"); if (!host) return;
    const el = document.createElement("div"); el.className = "flash pointer-events-auto" + (kind === "err" ? " flash-err" : ""); el.dataset.flash = "";
    const ico = kind === "err" ? "alert" : kind === "note" ? "bell" : "check";
    el.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#i-${ico}"/></svg>`;
    const t = document.createElement(o.href ? "a" : "span"); t.textContent = msg; if (o.href) { t.href = o.href; t.className = "no-underline"; } el.appendChild(t);
    const b = document.createElement("button"); b.type = "button"; b.className = "toast-x"; b.setAttribute("aria-label", "Close"); b.innerHTML = X; el.appendChild(b);
    host.appendChild(el); if (!o.sticky) setTimeout(() => dismiss(el), o.ms || 7000);
  };
  window.CWAS = { audio: Audio, toast, post, csrf, store, reduce };
  /* the first tap or key press opens the audio output, so every later sound plays at once */
  ["pointerdown", "keydown"].forEach(t => document.addEventListener(t, () => Audio.unlock(), { once: true, capture: true, passive: true }));
  $$("[data-flash]").forEach(f => { if (!f.querySelector(".select-all")) setTimeout(() => dismiss(f), 7000); });

  /* ── theme, favicon, sound toggle, menus ── */
  /* favicon and browser colour follow the theme: the CWAS mark on a tile of the theme's colour */
  const BRAND = { saina: "#FFFFFF", fotsy: "#FFFFFF", maitso: "#007E3A", mena: "#D42A20" };
  const applyBrand = th => {
    if (!BRAND[th]) th = "saina";
    const set = (k, url) => { const l = $(`link[data-brand="${k}"]`); if (l) l.href = url; };
    set("svg", `/static/img/brand/favicon-${th}.svg`); set("png", `/static/icons/${th}/favicon-32.png`); set("apple", `/static/icons/${th}/apple-touch-icon.png`);
    const m = $('meta[name="theme-color"]'); if (m) m.content = BRAND[th];
  };
  applyBrand(document.documentElement.dataset.theme);
  /* product screens (/platform) follow the theme at once: each image carries its set for every theme */
  const shots = th => $$("[data-shot]").forEach(el => { const s = el.getAttribute("data-shot-" + th); if (s && el.getAttribute("srcset") !== s) { el.srcset = s; if (el.tagName === "IMG") el.src = s.split(" ")[0]; } });
  new MutationObserver(() => shots(document.documentElement.dataset.theme || "saina")).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  /* once the page has loaded, the same-size files of the other themes are fetched and decoded in the background, so a switch
     finds them in memory and swaps in the same frame; a visitor saving data fetches them only when opening the theme menu */
  const warm = [];
  const warmShots = () => {
    if (warm.length) return;
    $$("img[data-shot]").forEach(img => {
      const cur = img.currentSrc || "", w = (cur.match(/-(\d+)\.(?:avif|webp)/) || [])[1];
      const holder = /\.avif/.test(cur) ? img.parentElement.querySelector("source[data-shot]") : img;
      if (!w || !holder) return;
      Object.keys(BRAND).forEach(th => {
        if (th === document.documentElement.dataset.theme) return;
        const url = (holder.getAttribute("data-shot-" + th) || "").split(",").map(x => x.trim().split(" ")[0]).find(x => x.includes("-" + w + "."));
        if (url) { const im = new Image(); im.src = url; if (im.decode) im.decode().catch(() => {}); warm.push(im); }
      });
    });
  };
  if ($("img[data-shot]")) {
    const conn = navigator.connection || {}, themeMenu = ($("[data-theme-set]") || document.body).closest("[data-menu]");
    if (!conn.saveData && !/2g/.test(conn.effectiveType || "")) addEventListener("load", () => (window.requestIdleCallback || setTimeout)(warmShots));
    if (themeMenu) ["pointerenter", "focusin", "touchstart"].forEach(ev => themeMenu.addEventListener(ev, warmShots, { once: true, passive: true }));
  }

  /* ── language: instant, no reload. The current page is fetched quietly in the other languages (X-CWAS-Lang);
     a switch swaps the words in place, so the scroll position, playing films and any open state all stay. ── */
  const I18N = new Map(), I18N_WAIT = new Map(), SERVER = window.CWAS_SERVER_NODES || new WeakSet();
  const langForms = () => Array.from(document.querySelectorAll("form")).filter(f => /\/lang$/.test(new URL(f.getAttribute("action") || "", location.href).pathname));
  const fetchLang = code => {
    if (I18N.has(code)) return Promise.resolve(I18N.get(code));
    if (I18N_WAIT.has(code)) return I18N_WAIT.get(code);
    const p = fetch(location.pathname + location.search, { headers: { "X-CWAS-Lang": code }, credentials: "same-origin", cache: "no-store" })
      .then(r => (r.ok && !r.redirected ? r.text() : Promise.reject(new Error("unavailable"))))
      .then(html => { I18N.set(code, html); return html; })
      .finally(() => I18N_WAIT.delete(code));
    I18N_WAIT.set(code, p);
    return p;
  };
  const KEEP = /^(class|style|hidden|value|checked|selected|open|disabled|aria-expanded|aria-hidden|tabindex|data-state|data-paused)$/;
  const kids = el => Array.from(el.childNodes).filter(n => n.nodeType === 1 || (n.nodeType === 3 && n.nodeValue.trim()));
  const same = (x, y) => x.nodeType === y.nodeType && (x.nodeType === 3 || (x.tagName === y.tagName && SERVER.has(x)));
  const drop = n => { if (n.nodeType === 3 || SERVER.has(n)) n.remove(); };
  const mark = n => { if (n.nodeType === 1) { SERVER.add(n); n.querySelectorAll("*").forEach(e => SERVER.add(e)); } };
  const syncAttrs = (x, y) => {
    Array.from(y.attributes).forEach(({ name, value }) => {
      if (KEEP.test(name) && !(name === "value" && /^(submit|button)$/i.test(y.getAttribute("type") || ""))) return;
      if (name === "aria-pressed" && y.getAttribute("name") !== "code") return;  // live state (hero, menus) stays
      if (x.getAttribute(name) !== value) x.setAttribute(name, value);
    });
  };
  const resplit = (x, y) => {  // headings and menu words that the effects script split into letters
    const fx = window.CWASfx || {};
    if (x.hasAttribute("data-type") && fx.splitType) { const txt = y.textContent.trim(); if (x.getAttribute("aria-label") !== txt) { x.textContent = txt; fx.splitType(x); } return true; }
    if (x.classList.contains("lts") && fx.splitLetters) { const txt = y.getAttribute("data-letters") || y.textContent.trim(); if (x.getAttribute("aria-label") !== txt) { x.dataset.letters = txt; fx.splitLetters(x); } return true; }
    return false;
  };
  const morph = (x, y) => {
    if (x.nodeType === 3) { if (x.nodeValue !== y.nodeValue) x.nodeValue = y.nodeValue; return; }
    syncAttrs(x, y);
    if (resplit(x, y) || x.tagName === "TEXTAREA" || (x.tagName === "SCRIPT" && x.type !== "application/json")) return;
    const ka = kids(x), kb = kids(y);
    let j = 0;
    kb.forEach(nb => {
      let k = j;
      while (k < ka.length && !same(ka[k], nb)) k++;
      if (k < ka.length) { for (let s = j; s < k; s++) drop(ka[s]); morph(ka[k], nb); j = k + 1; }
      else { const node = document.importNode(nb, true); x.insertBefore(node, ka[j] || null); mark(node); }
    });
    for (let s = j; s < ka.length; s++) drop(ka[s]);
  };
  const closeMenus = () => {
    $$(".menu.open").forEach(m => m.classList.remove("open"));
    $$('[data-menu-btn][aria-expanded="true"]').forEach(b => b.setAttribute("aria-expanded", "false"));
  };
  const swapLang = (html, code) => {
    const doc = new DOMParser().parseFromString(html, "text/html");
    if (!doc.body || !doc.body.firstElementChild) throw new Error("empty");
    morph(document.body, doc.body);
    document.documentElement.lang = code;
    /* an address that carries a language (?lang=, as search engines and shared links use it) follows the switch */
    try { const u = new URL(location.href); if (u.searchParams.has("lang")) { if (code === "en") u.searchParams.delete("lang"); else u.searchParams.set("lang", code); history.replaceState(history.state, "", u); } } catch (err) { /* very old browser */ }
    if (doc.title) document.title = doc.title;
    closeMenus();
    document.dispatchEvent(new CustomEvent("cwas:lang", { detail: { lang: code } }));
  };
  const prefetchLangs = () => {
    const cur = document.documentElement.lang;
    const codes = [...new Set(langForms().flatMap(f => Array.from(f.querySelectorAll('[name="code"]')).map(b => b.value)))].filter(c => c && c !== cur);
    const go = () => codes.reduce((p, c) => p.then(() => fetchLang(c).catch(() => {})), Promise.resolve());
    (window.requestIdleCallback || (fn => setTimeout(fn, 900)))(go, { timeout: 3000 });
  };
  document.addEventListener("submit", e => {
    const f = e.target, code = e.submitter && e.submitter.value;
    if (!(f instanceof HTMLFormElement) || !code || !langForms().includes(f)) return;
    e.preventDefault();
    if (code === document.documentElement.lang) { closeMenus(); return; }
    const body = new FormData(f); body.set("code", code);
    fetch(f.action, { method: "POST", body, credentials: "same-origin", redirect: "manual", keepalive: true }).catch(() => {});  // remembers the choice
    Audio.play("tap");
    fetchLang(code).then(html => { swapLang(html, code); prefetchLangs(); }).catch(() => {
      const i = document.createElement("input"); i.type = "hidden"; i.name = "code"; i.value = code; f.appendChild(i);
      HTMLFormElement.prototype.submit.call(f);  // last resort: the classic page load
    });
  });
  prefetchLangs();
  const syncSound = () => $$("[data-sound-toggle]").forEach(b => { const on = Audio.on(); b.dataset.sound = on ? "on" : "off"; b.setAttribute("aria-pressed", on ? "true" : "false"); });
  syncSound();
  document.addEventListener("click", e => {
    const th = e.target.closest("[data-theme-set]");
    if (th) {
      const root = document.documentElement;
      root.classList.add("theme-swap");  // the new colours land at once, with no fade
      root.dataset.theme = th.dataset.themeSet;
      requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove("theme-swap")));
      document.cookie = `cwas_visit_theme=${th.dataset.themeSet};path=/;SameSite=Lax`;  // lasts for this visit; the next one opens in Default
      $$("[data-theme-set]").forEach(b => b.setAttribute("aria-pressed", b === th ? "true" : "false"));
      applyBrand(th.dataset.themeSet); Audio.play("tap"); document.dispatchEvent(new CustomEvent("cwas:theme"));
    }
    if (e.target.closest("[data-sound-toggle]")) { Audio.set(!Audio.on()); syncSound(); Audio.play("tap"); }
    const x = e.target.closest(".toast-x"); if (x) dismiss(x.closest(".flash"));
    const btn = e.target.closest("[data-menu-btn]");
    $$("[data-menu].open").forEach(m => { if (!btn || m !== btn.closest("[data-menu]")) { m.classList.remove("open"); const b = $("[data-menu-btn]", m); b && b.setAttribute("aria-expanded", "false"); } });
    if (btn) { const m = btn.closest("[data-menu]"); const open = m.classList.toggle("open"); btn.setAttribute("aria-expanded", open ? "true" : "false"); }
    if (e.target.closest("[data-sheet-open]")) { $("[data-sheet]").hidden = false; document.body.style.overflow = "hidden"; }
    if (e.target.closest("[data-sheet-close]") || e.target.closest("[data-sheet] a")) { $("[data-sheet]").hidden = true; document.body.style.overflow = ""; }
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape") { $$("[data-menu].open").forEach(m => { m.classList.remove("open"); const b = $("[data-menu-btn]", m); if (b) { b.setAttribute("aria-expanded", "false"); if (m.contains(document.activeElement)) b.focus(); } }); const s = $("[data-sheet]"); if (s) { s.hidden = true; document.body.style.overflow = ""; } } });
  /* a menu opened from the keyboard closes once focus leaves it */
  document.addEventListener("focusout", e => { const m = e.target.closest && e.target.closest(".nav-item.open"); if (m && !(e.relatedTarget && m.contains(e.relatedTarget))) { m.classList.remove("open"); const b = $("[data-menu-btn]", m); if (b) b.setAttribute("aria-expanded", "false"); } });
  document.addEventListener("submit", e => { const m = e.target.dataset && e.target.dataset.confirm; if (m && !window.confirm(m)) e.preventDefault(); });
  document.addEventListener("change", e => { if (e.target.matches("[data-autosubmit]")) e.target.form.submit(); });


  /* the header is transparent glass; over a hero video it uses the light-on-dark recipe, then follows the theme */
  const bar = $(".hdr-bar"), hero = $("[data-hero]");
  if (bar && hero) { const upd = () => bar.classList.toggle("on-dark", hero.getBoundingClientRect().bottom > 92); addEventListener("scroll", upd, { passive: true }); addEventListener("resize", upd); upd(); }

  /* ── live notifications: a new one pops a toast and plays the chime ── */
  if (document.body.dataset.auth === "1") {
    let last = +document.body.dataset.lastNote || 0;
    const badge = () => $$("[data-badge]");
    const poll = () => {
      if (document.hidden) return;
      fetch(`/api/notifications/poll?after=${last}`, { credentials: "same-origin" }).then(r => r.ok ? r.json() : null).then(d => {
        if (!d) return;
        (d.items || []).forEach(n => toast(n.text, "note", { href: n.href, ms: 9000 }));
        if ((d.items || []).length) { const bl = $(".btn-icon[href*=\"notifications\"]"); if (bl) { bl.classList.add("ring"); setTimeout(() => bl.classList.remove("ring"), 1300); } }
        if ((d.items || []).length) Audio.play("notify");
        last = d.last || last;
        badge().forEach(b => { b.textContent = d.unread; b.hidden = !d.unread; });
      }).catch(() => {});
    };
    setInterval(poll, 20000); document.addEventListener("visibilitychange", poll);
  }

  /* ── videos: real footage that starts at once; a small button pauses it ── */
  const saveData = (navigator.connection || {}).saveData;
  $$("video.vid").forEach(v => {
    v.muted = true; v.defaultMuted = true; v.setAttribute("playsinline", "");
    const wrap = v.parentElement; let paused = false, visible = true;
    const btn = document.createElement("button"); btn.type = "button"; btn.className = "vid-toggle"; btn.setAttribute("aria-label", "Pause or play video");
    btn.innerHTML = '<svg class="icon" data-i="pause" aria-hidden="true"><use href="#i-pause"/></svg><svg class="icon" data-i="play" aria-hidden="true"><use href="#i-play"/></svg>';
    wrap.appendChild(btn);
    const set = p => { paused = p; btn.dataset.paused = p ? "true" : "false"; if (p) v.pause(); else if (visible) v.play().catch(() => { btn.dataset.paused = "true"; paused = true; }); };
    btn.addEventListener("click", () => set(!paused));
    if (reduce || saveData) { paused = true; btn.dataset.paused = "true"; } else v.play().catch(() => { btn.dataset.paused = "true"; paused = true; });
    /* a film further down the page gets its file once it nears the view (the HD one only where it shows); until then its
       poster stands in, and pressing play fetches it at once */
    const load = () => { if (v.getAttribute("src")) return; v.autoplay = !paused; v.preload = paused ? "metadata" : "auto"; v.src = v.dataset.hd && !saveData && v.clientWidth > 900 ? v.dataset.hd : v.dataset.src; };
    btn.addEventListener("click", load);
    if (!saveData) { if ("IntersectionObserver" in window) new IntersectionObserver((es, o) => es.forEach(en => { if (en.isIntersecting) { load(); o.disconnect(); } }), { rootMargin: "100% 0px" }).observe(v); else load(); }
    if ("IntersectionObserver" in window) new IntersectionObserver(es => es.forEach(en => { visible = en.isIntersecting; if (!visible) v.pause(); else if (!paused) v.play().catch(() => {}); }), { threshold: .05 }).observe(v);
    document.addEventListener("pointerdown", () => { if (!paused && v.paused && visible) v.play().catch(() => {}); }, { once: true });
  });

  /* ── receipt printer (metal thermal printer) ── */
  const initPrinter = p => {
    const paper = $(".paper", p), lcd = $("[data-lcd]", p), d = p.dataset;
    const say = k => { if (lcd) lcd.textContent = d[k] || ""; };
    const set = s => { p.dataset.state = s; say({ idle: "lReady", printing: "lPrint", done: "lTear", torn: "lTorn" }[s]); };
    const start = () => { if (p.dataset.state === "printing") return; set("idle"); void p.offsetWidth; if (reduce) { set("done"); return; } set("printing"); Audio.play("print"); };
    paper.addEventListener("animationend", () => { set("done"); Audio.play("success"); });
    const scope = p.closest("[data-printer-scope]") || document;
    $$("[data-print-start]", scope).forEach(b => b.addEventListener("click", start));
    $$("[data-tear]", scope).forEach(b => b.addEventListener("click", () => { if (p.dataset.state === "done") { set("torn"); Audio.play("tear"); } }));
    $$("[data-window-print]", scope).forEach(b => b.addEventListener("click", () => { set("done"); setTimeout(() => window.print(), 50); }));
    set("idle");
    if (p.hasAttribute("data-autoprint")) {
      if (!("IntersectionObserver" in window)) return start();
      const o = new IntersectionObserver(es => { if (es[0].isIntersecting) { start(); o.disconnect(); } }, { threshold: .4 }); o.observe(p);
    }
  };
  window.CWAS.initPrinter = initPrinter;
  $$("[data-printer]").forEach(initPrinter);

  /* receipt preview without leaving the page */
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-receipt]"); if (!b) return;
    e.preventDefault();
    fetch(b.dataset.receipt + "?partial=1", { credentials: "same-origin" }).then(r => r.ok ? r.text() : Promise.reject()).then(html => {
      let dlg = $("dialog[data-receipt-dialog]");
      if (!dlg) { dlg = document.createElement("dialog"); dlg.className = "modal"; dlg.dataset.receiptDialog = ""; document.body.appendChild(dlg); dlg.addEventListener("click", ev => { if (ev.target === dlg) dlg.close(); }); }
      dlg.innerHTML = html; dlg.showModal();
      $$("[data-modal-close]", dlg).forEach(c => c.addEventListener("click", () => dlg.close()));
      const p = $("[data-printer]", dlg); if (p) initPrinter(p);
    }).catch(() => toast("Receipt not available.", "err"));
  });

  /* ── booking form: live price and slot refresh ── */
  const book = $("[data-book]");
  if (book) {
    const quotes = JSON.parse(book.dataset.quotes || "{}"), price = $("[data-price]", book);
    const upd = () => { const l = ($("input[name=litres]:checked", book) || {}).value; if (price && quotes[l] !== undefined) price.textContent = `${String(quotes[l]).replace(/\B(?=(\d{3})+(?!\d))/g, ",")} MGA`; };
    book.addEventListener("change", e => { upd(); if (e.target.matches("input")) Audio.play("tap"); }); upd();
    const refresh = () => {
      if (document.hidden) return;
      fetch(`/api/slots?source=${book.dataset.source}&date=${book.dataset.date}`, { credentials: "same-origin" }).then(r => r.json()).then(d => {
        (d.slots || []).forEach(s => {
          const inp = $(`input[name=slot][value="${s.start_min}"]`, book); if (!inp) return;
          const lab = inp.nextElementSibling; lab.dataset.state = s.state; inp.disabled = s.state !== "open";
          const sm = $("small", lab); if (sm) sm.textContent = s.state === "open" ? `${s.free}/${s.capacity}` : s.state;
        });
      }).catch(() => {});
    };
    setInterval(refresh, 25000);
  }
})();
