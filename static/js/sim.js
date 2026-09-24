/* Winebald handsets. On /simulator (the device lab) they talk to the real USSD and SMS engine. On the homepage and
   /access ([data-phones]) the very same devices, apps and screens play a script the engine itself rendered
   (ussd.demo_script), so what a visitor sees there is what a caller gets, pixel for pixel. */
(() => {
  "use strict";
  const lab = document.querySelector("[data-lab]"), demoRow = document.querySelector("[data-phones]"); if (!lab && !demoRow) return;
  const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const C = window.CWAS, readInit = () => JSON.parse($(lab ? "#sim-init" : "#phones-init").textContent);
  let init = readInit(), LB = init.labels, demoOn = true;
  const DIAL = lab ? lab.dataset.dial : init.dial;
  /* the demo phones make sounds only while they are on screen */
  const A = lab ? C.audio : { play: (...a) => { if (demoOn && !document.hidden) C.audio.play(...a); }, on: () => C.audio.on(), set: v => C.audio.set(v) };
  const rig = $("[data-rig]"), stage = $("[data-stage]"), phoneIn = $("[data-phone]"), consoleEl = $("[data-console]");
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const h = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html !== undefined) e.innerHTML = html; return e; };
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "Indian/Antananarivo", /* the visitor's own time zone, no location asked */ locale = { mg: "fr-FR", fr: "fr-FR", en: "en-GB" }[document.documentElement.lang] || "en-GB";
  const clock = () => new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: TZ }).format(new Date());
  const dateLong = () => new Intl.DateTimeFormat(locale, { weekday: "long", day: "numeric", month: "long", timeZone: TZ }).format(new Date());
  const LETTERS = { 2: "ABC", 3: "DEF", 4: "GHI", 5: "JKL", 6: "MNO", 7: "PQRS", 8: "TUV", 9: "WXYZ", 0: "+" };

  /* ── network layer: every hop is a real request; the console shows it ── */
  const Net = {
    last: null,
    log(kind, path, req, res, ms) {
      const li = h("li", "rounded-2xl bg-ink/[.07] p-2.5");
      const at = new Date().toLocaleTimeString("en-GB", { timeZone: TZ });
      li.innerHTML = `<div class="flex items-center justify-between gap-2"><b>${kind} <span class="opacity-70">${esc(path)}</span></b><span class="opacity-60">${at} · ${ms} ms</span></div><pre class="mt-1 whitespace-pre-wrap break-all opacity-80">${esc(req)}</pre><pre class="mt-1 whitespace-pre-wrap break-all font-bold">${esc(res)}</pre>`;
      if (kind === "USSD") { const b = h("button", "btn btn-ghost btn-sm mt-1.5", "cURL"); b.type = "button"; b.addEventListener("click", () => { const p = Net.last || {}; const cmd = `curl -X POST ${location.origin}/api/ussd -d 'sessionId=${p.sid}' -d 'phoneNumber=${p.phone}' -d 'text=${p.text}' -d 'serviceCode=${DIAL}' -d 'networkCode=99999'`; navigator.clipboard && navigator.clipboard.writeText(cmd); C.toast(LB.copied, "ok", { ms: 1500 }); }); li.appendChild(b); }
      consoleEl.prepend(li); while (consoleEl.children.length > 40) consoleEl.lastChild.remove();
    },
    async ussd(phone, sid, text) {
      const t0 = performance.now();
      const r = await C.post("/simulator/api/ussd", { phone, session: sid, text });
      Net.last = { sid, phone, text };
      Net.log("USSD", "POST /api/ussd", `sessionId=${sid}\nphoneNumber=${phone}\ntext=${text}\nserviceCode=${DIAL}\nnetworkCode=99999`, r.response || JSON.stringify(r), Math.round(performance.now() - t0));
      return r;
    },
    async sms(phone, text) {
      const t0 = performance.now();
      const r = await C.post("/simulator/api/sms", { phone, text });
      Net.log("SMS", "POST /api/sms/inbound", `from=${phone}\nto=7380\ntext=${text}`, (r.messages || []).map(m => m.body).join("\n---\n") || r.reply || "(no reply)", Math.round(performance.now() - t0));
      return r;
    },
  };
  if (lab) $("[data-clear]").addEventListener("click", () => { consoleEl.innerHTML = ""; });

  /* ── one phone identity: number, SMS threads, USSD session ── */
  class Phone {
    constructor(number) { this.number = number; this.threads = { "7380": [] }; this.unread = 0; this.last = 0; this.ready = false; this.subs = new Set(); this.timer = setInterval(() => this.poll(), 3000); this.sid = ""; this.active = false; this.tokens = []; this.poll(); }
    destroy() { clearInterval(this.timer); }
    emit(ev, d) { this.subs.forEach(f => f(ev, d)); }
    add(dir, body, at, addr = "7380") { (this.threads[addr] = this.threads[addr] || []).push({ dir, body, at: at || clock() }); if (dir === "in") this.unread++; }
    async poll(soon) {
      if (document.hidden && !soon) return;
      try {
        const r = await fetch(`/simulator/api/inbox?phone=${encodeURIComponent(this.number)}&after=${this.last}`, { credentials: "same-origin" }).then(x => x.ok ? x.json() : null);
        if (!r) return; this.last = r.last || this.last;
        (r.messages || []).forEach(m => { this.add("in", m.body, m.at); if (this.ready) this.emit("sms", m); });
        if ((r.messages || []).length) this.emit("threads");
        this.ready = true;
      } catch (e) { /* offline */ }
    }
    pollSoon() { setTimeout(() => this.poll(true), 350); setTimeout(() => this.poll(true), 1600); }
    async sendSms(text) {
      this.add("out", text); this.emit("threads"); A.play("send");
      const r = await Net.sms(this.number, text);
      if (r.error) { this.add("in", LB.invalid); this.emit("threads"); return; }
      this.last = Math.max(this.last, r.last || 0);
      (r.messages || []).forEach(m => { this.add("in", m.body, m.at); this.emit("sms", m); });
      this.emit("threads");
    }
    async dial() { this.sid = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random())).slice(0, 18); this.tokens = []; this.active = true; A.play("connect"); await this.hop(""); }
    async send(v) { if (!this.active) return; this.tokens.push(v); A.play("send"); await this.hop(this.tokens.join("*")); }
    async hop(text) {
      let r; try { r = await Net.ussd(this.number, this.sid, text); } catch (e) { r = { error: 1 }; }
      if (r.error) { this.active = false; A.play("error"); this.emit("ussd", { text: LB.invalid, ended: true, secret: false }); return; }
      const ended = !!r.ended; this.active = !ended;
      A.play(/Invalid choice|Tsy mety|Choix invalide/i.test(r.screen) ? "error" : "receive");
      this.emit("ussd", { text: r.screen, ended, secret: !ended && /PIN|kaody|code|Confirm PIN|Hamafiso ny PIN/i.test(r.screen.split("\n")[0]) });
      if (ended) this.pollSoon();
    }
    cancel() { this.active = false; this.emit("ussd", { text: "", ended: true, gone: true }); }
  }

  /* ── shared bits ── */
  const ICONS = {
    phone: ['#25b35a', '<path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A15 15 0 0 1 3 6a2 2 0 0 1 2-2z"/>'],
    messages: ['#2f7bff', '<path d="M4 5h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H9l-5 4V6a1 1 0 0 1 1-1z"/><path d="M8 10h8M8 13h5"/>'],
    contacts: ['#f59e0b', '<circle cx="12" cy="9" r="3.6"/><path d="M5 20a7 7 0 0 1 14 0"/>'],
    cwas: ['#ffffff', '<use href="#logo"/>'],
  };
  const icon = (id, size) => { const [bg, svg] = ICONS[id]; return `<span class="app-ico${id === "cwas" ? " cw-ico" : ""}" style="${id === "cwas" ? "" : `background:${bg};`}${size ? `width:${size}px;height:${size}px` : ""}">${id === "cwas" ? `<svg viewBox="0 0 48 48" style="width:36px;height:36px;stroke:none;fill:none">${svg}</svg>` : `<svg viewBox="0 0 24 24">${svg}</svg>`}</span>`; };
  const sigBars = '<svg viewBox="0 0 18 12" width="16" height="11" fill="currentColor"><rect x="0" y="8" width="3" height="4" rx="1"/><rect x="5" y="5" width="3" height="7" rx="1"/><rect x="10" y="2.5" width="3" height="9.5" rx="1"/><rect x="15" y="0" width="3" height="12" rx="1"/></svg>';
  const IMEI = (() => { let v = C.store.get("cwas_imei", ""); if (!v) { v = "35" + Math.floor(Math.random() * 1e13).toString().padStart(13, "0"); C.store.set("cwas_imei", v); } return v; })();

  /* the sender of every CWAS message: the logo tile and the name, never a bare initial */
  const CWAS_LOGO = '<span class="logo-tile sms-logo"><svg viewBox="0 0 64 64" aria-hidden="true"><use href="#logo"/></svg></span>';

  /* ── smartphone OS (Nova and Max) ── */
  class SmartOS {
    constructor(screen, phone, model) {
      this.screen = screen; this.phone = phone; this.model = model; this.stack = []; this.timers = [];
      screen.innerHTML = `<div class="os locked"><div class="os-wall"><i></i><i></i><i></i></div><div class="os-status"><span data-clk>${clock()}</span><span class="r">${sigBars}<span style="font-size:10px">4G</span><span class="batt"><b></b></span></span></div>
        <div class="os-home" data-home></div><div class="dock" data-dock></div><button class="home-bar" data-bar aria-label="Home"></button>
        <div class="os-lock" data-lock><div class="time" data-lt>${clock()}</div><div class="date">${esc(dateLong())}</div><div class="hint">${esc(LB.lock)}</div></div><div class="banner" data-banner></div></div><div class="glare"></div>`;
      this.$ = s => $(s, screen); this.os = this.$(".os");
      this.tick = setInterval(() => { this.$("[data-clk]").textContent = clock(); this.$("[data-lt]").textContent = clock(); this.$("[data-wt]").textContent = clock(); }, 10000); this.timers.push(this.tick);
      const home = this.$("[data-home]");
      home.appendChild(h("div", "os-widget", `<b data-wt>${clock()}</b><span>${esc(dateLong())}</span>`));
      ["phone", "messages", "contacts", "cwas"].forEach(id => this.$("[data-dock]").appendChild(this.appBtn(id)));
      this.$("[data-bar]").addEventListener("click", () => { if (this.stack.length) this.close(); A.play("tap"); });
      const lock = this.$("[data-lock]"); let y0 = null;
      lock.addEventListener("pointerdown", e => { y0 = e.clientY; }); lock.addEventListener("pointerup", e => { if (y0 !== null) this.unlock(); y0 = null; });
      this.offSub = phone.subs.add(this.onPhone = (ev, d) => this.handle(ev, d)); this.locked = true; this.on = true;
    }
    appBtn(id) { const b = h("button", "app", icon(id) + `<span>${esc(LB[id] || id)}</span>`); b.type = "button"; b.dataset.app = id; if (id === "messages") { const dot = h("span"); dot.dataset.dot = ""; dot.style.cssText = "position:absolute;top:-4px;right:6px;min-width:18px;height:18px;border-radius:9px;background:#ef4444;color:#fff;font:700 11px/18px system-ui;text-align:center;display:none;padding:0 4px"; b.style.position = "relative"; b.firstChild.style.position = "relative"; b.appendChild(dot); } b.addEventListener("click", () => this.open(id, b)); return b; }
    destroy() { this.phone.subs.delete(this.onPhone); this.timers.forEach(clearInterval); this.stack.forEach(v => v._end && v._end()); }
    setBadge() { const d = this.$("[data-dot]"); if (d) { d.textContent = this.phone.unread; d.style.display = this.phone.unread ? "block" : "none"; } }
    unlock() { if (!this.locked) return; this.locked = false; this.$("[data-lock]").classList.add("gone"); this.$(".os").classList.remove("locked"); A.play("connect"); }
    wake() { this.on = true; this.screen.classList.remove("off"); this.locked = true; this.$(".os").classList.add("locked"); this.$("[data-lock]").classList.remove("gone"); A.play("poweron"); }
    sleep() { this.on = false; this.screen.classList.add("off"); A.play("poweroff"); }
    power() { this.on ? this.sleep() : this.wake(); }
    banner(m) {
      const b = this.$("[data-banner]"); b.innerHTML = `${CWAS_LOGO}<div style="min-width:0"><b>CWAS</b><span>${esc(m.body.slice(0, 90))}</span></div>`; b.onclick = () => { b.classList.remove("show"); this.unlock(); this.open("messages"); };
      b.classList.add("show"); clearTimeout(this.bt); this.bt = setTimeout(() => b.classList.remove("show"), 5000);
    }
    handle(ev, d) {
      if (ev === "sms") { if (!this.on) this.wake(); A.play("sms"); this.banner(d); this.setBadge(); }
      if (ev === "threads") this.setBadge();
      if (ev === "ussd") this.sheet(d);
    }
    view(title, dark) {
      const v = h("div", "os-view" + (dark ? " dark" : ""));
      v.innerHTML = `<div class="app-head"><button class="bk" type="button" aria-label="${esc(LB.back)}"><span style="font-size:22px;line-height:1;margin-top:-2px">&#8249;</span></button><span>${esc(title)}</span></div>`;
      $(".bk", v).addEventListener("click", () => this.close()); return v;
    }
    open(id, origin) {
      if (this.locked) return;
      const v = this.apps[id].call(this); v.classList.add("away");
      if (origin) { const r = origin.getBoundingClientRect(), s = this.screen.getBoundingClientRect(); v.style.setProperty("--ox", (r.left + r.width / 2 - s.left) + "px"); v.style.setProperty("--oy", (r.top + r.height / 2 - s.top) + "px"); }
      this.os.appendChild(v); requestAnimationFrame(() => requestAnimationFrame(() => v.classList.remove("away"))); this.stack.push(v); this.os.classList.add("in-app"); A.play("tap"); if (id === "messages") { this.phone.unread = 0; this.setBadge(); }
    }
    close() { const v = this.stack.pop(); if (!v) return; if (v._end) v._end(); v.classList.add("away"); setTimeout(() => v.remove(), 360); if (!this.stack.length) this.os.classList.remove("in-app"); }
    typeUssd(v) { const i = this.$(".ussd-card input"); if (i) i.value = v; }
    sheet(d) {
      let s = this.$(".ussd-sheet"); if (d.gone) { if (s) s.remove(); return; }
      if (!s) { s = h("div", "ussd-sheet"); this.os.appendChild(s); }
      s.innerHTML = `<div class="ussd-card"><pre></pre>${d.ended ? "" : `<input type="${d.secret ? "password" : "text"}" inputmode="text" autocomplete="off" aria-label="${esc(LB.send)}">`}<div class="row">${d.ended ? "" : `<button type="button" data-x>${esc(LB.cancel)}</button>`}<button type="button" data-ok>${esc(d.ended ? LB.ok : LB.send)}</button></div></div>`;
      $("pre", s).textContent = d.text; const inp = $("input", s);
      const go = () => { const v = inp.value.trim(); if (!v) return; inp.value = ""; this.phone.send(v); };
      $("[data-ok]", s).addEventListener("click", () => d.ended ? s.remove() : go()); const x = $("[data-x]", s); if (x) x.addEventListener("click", () => { this.phone.cancel(); A.play("tap"); });
      if (inp) { inp.addEventListener("keydown", e => { if (e.key === "Enter") go(); }); if (lab) inp.focus({ preventScroll: true }); }
    }
    call(num, name) {
      if (/^\*[\d*]+#$/.test(num)) { if (num === DIAL) { this.phone.dial(); return; } this.sheet({ text: "Connection problem or invalid MMI code.", ended: true }); A.play("error"); return; }
      const o = h("div", "ussd-sheet"); o.style.cssText = "background:#0d1512;flex-direction:column;color:#fff;text-align:center"; o.innerHTML = `<div style="margin-top:90px;font:700 26px 'Bricolage Grotesque Variable',system-ui">${esc(name || num)}</div><div style="opacity:.7;margin-top:6px" data-st>${esc(LB.calling)}</div><button class="callbtn" style="background:#e0483d;margin-top:auto;margin-bottom:60px" type="button" aria-label="End"><svg viewBox="0 0 24 24" style="transform:rotate(135deg)"><path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A15 15 0 0 1 3 6a2 2 0 0 1 2-2z"/></svg></button>`;
      this.os.appendChild(o); A.play("ring"); const ring = setInterval(() => A.play("ring"), 3000);
      const end = () => { clearInterval(ring); clearTimeout(to); o.remove(); A.play("poweroff"); }; $("button", o).addEventListener("click", end);
      const to = setTimeout(() => { $("[data-st]", o).textContent = LB.noanswer; setTimeout(end, 1400); }, 6500);
    }
  }
  SmartOS.prototype.apps = {
    phone(pre) {
      const v = this.view(LB.phone), me = this; let num = typeof pre === "string" ? pre : "";
      const out = h("div", "dial-out", esc(num)); const pad = h("div", "pad"), row = h("div", "dial-row");
      const del = h("button", "dial-del", "&#9003;"); del.type = "button"; del.setAttribute("aria-label", LB.clear);
      const show = () => { out.textContent = num; del.hidden = !num; };
      "123456789*0#".split("").forEach(k => { const b = h("button", "", `${k}${LETTERS[k] ? `<small>${LETTERS[k]}</small>` : ""}`); b.type = "button"; b.dataset.key = k; b.addEventListener("click", () => { A.play("key", k); num += k; show(); }); pad.appendChild(b); });
      const cb = h("button", "callbtn", '<svg viewBox="0 0 24 24"><path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A15 15 0 0 1 3 6a2 2 0 0 1 2-2z"/></svg>'); cb.type = "button"; cb.setAttribute("aria-label", LB.call);
      cb.addEventListener("click", () => { if (!num) return; me.call(num); });
      del.addEventListener("click", () => { num = num.slice(0, -1); show(); A.play("tap"); });
      row.append(out, del); show(); v.append(row, pad, cb); v._num = () => num; v._set = s => { num = s; show(); }; return v;
    },
    cwas() { return this.apps.phone.call(this, DIAL); },  // the CWAS icon opens the dialer with the service code ready
    messages() {
      const v = this.view(LB.messages), me = this, body = h("div", "", ""); body.style.cssText = "display:flex;flex-direction:column;flex:1;min-height:0";
      v.appendChild(h("div", "sms-head", `${CWAS_LOGO}<span><b>CWAS</b><small>7380</small></span>`)); v.appendChild(body);
      const draw = () => {
        body.innerHTML = ""; const th = me.phone.threads["7380"] || [];
        const list = h("div", "thread"); if (!th.length) list.appendChild(h("p", "", esc(LB.nomsg))), list.lastChild.style.cssText = "opacity:.6;text-align:center;margin-top:30px";
        th.forEach(m => { const b = h("div", "sms-b " + (m.dir === "in" ? "sms-in" : "sms-out")); b.textContent = m.body; b.title = m.at; list.appendChild(b); });
        const f = h("form", "compose", `<input placeholder="${esc(LB.typeMsg)}" maxlength="160" aria-label="${esc(LB.typeMsg)}"><button aria-label="${esc(LB.send)}"><svg class="icon" aria-hidden="true" style="stroke:#fff;fill:none;width:16px;height:16px"><use href="#i-send"/></svg></button>`);
        f.addEventListener("submit", e => { e.preventDefault(); const i = $("input", f), t = i.value.trim(); if (!t) return; i.value = ""; me.phone.sendSms(t); });
        body.append(list, f); list.scrollTop = list.scrollHeight;
      };
      draw(); const sub = (ev) => { if (ev === "threads") { draw(); me.phone.unread = 0; me.setBadge(); } }; me.phone.subs.add(sub); v._end = () => me.phone.subs.delete(sub); return v;
    },
    contacts() {
      const v = this.view(LB.contacts), me = this, rows = [["CWAS Water", DIAL, "#25b35a"], ["CWAS SMS", "7380", "#2f7bff"], [LB.coord, "+261340000001", "#f59e0b"], ...init.demo.map((p, i) => ["Household " + (i + 1), p, "#7c8582"])];
      v.appendChild(h("p", "", esc(LB.contactsHint))).style.cssText = "padding:0 16px 8px;font-size:12px;opacity:.6";
      rows.forEach(([n, p, c]) => { const b = h("button", "list-i", `${/^CWAS/.test(n) ? CWAS_LOGO : `<span class="avatar" style="background:${c}">${esc(n[0])}</span>`}<span><b>${esc(n)}</b><small>${esc(p)}</small></span>`); b.type = "button"; b.addEventListener("click", () => { if (p === "7380") { me.close(); me.open("messages"); } else { me.close(); me.open("phone"); const pv = me.stack[me.stack.length - 1]; pv._set(p); setTimeout(() => me.call(p, n), 500); } }); v.appendChild(b); });
      v.style.overflowY = "auto"; return v;
    },
  };

  /* ── feature phone (Lite 2) ── */
  const MT = { 1: ".,?!1", 2: "abc2", 3: "def3", 4: "ghi4", 5: "jkl5", 6: "mno6", 7: "pqrs7", 8: "tuv8", 9: "wxyz9", 0: " 0" };
  class LiteOS {
    constructor(screen, phone) {
      this.screen = screen; this.phone = phone; this.state = "home"; this.sel = 0; this.buf = ""; this.msg = ""; this.mt = { k: null, n: 0, t: 0 }; this.on = true; this.flash = ""; this.calc = { cur: "0", acc: null, op: null, fresh: true }; this.ussd = null; this.setting = 0;
      screen.innerHTML = `<div class="lite-os"><div class="bar"><span data-t></span><span>${sigBars.replace('width="16" height="11"', 'width="12" height="9"')} 4G</span></div><div class="main" data-main></div><div class="foot"><span data-l></span><span data-r></span></div></div>`;
      this.phone.subs.add(this.onPhone = (ev, d) => this.handle(ev, d)); this.tick = setInterval(() => this.render(), 15000); this.render();
    }
    destroy() { this.phone.subs.delete(this.onPhone); clearInterval(this.tick); }
    handle(ev, d) {
      if (ev === "sms") { if (!this.on) this.power(); A.play("sms"); this.flash = LB.newMsg; setTimeout(() => { this.flash = ""; this.render(); }, 3500); }
      if (ev === "ussd") { if (d.gone) { this.state = "home"; } else { this.state = "ussd"; this.ussd = d; this.buf = ""; } }
      this.render();
    }
    power() { this.on = !this.on; this.screen.classList.toggle("off", !this.on); A.play(this.on ? "poweron" : "poweroff"); }
    items() { return [["phone", LB.phone], ["messages", LB.messages], ["contacts", LB.contacts], ["cwas", "CWAS"]]; }  // the same four apps as the smartphones' dock
    contacts() { return [["CWAS Water", DIAL], ["CWAS SMS", "7380"], [LB.coord, "+261340000001"], ...init.demo.map((p, i) => ["Household " + (i + 1), p])]; }
    render() {
      if (!this.on) return; const S = this.state, m = this.screen.querySelector("[data-main]"); let main = "", l = "", r = "";
      this.screen.querySelector(".lite-os").dataset.state = S;
      this.screen.querySelector("[data-t]").textContent = clock();
      if (S === "home") { main = `<div class="big">${clock()}</div><div style="text-align:center">${esc(dateLong())}</div><div style="text-align:center;margin-top:14px;opacity:.8">WINEBALD</div>${this.flash ? `<div style="text-align:center;margin-top:8px" class="sel">${esc(this.flash)}</div>` : ""}`; l = LB.menu; r = LB.contacts; }
      else if (S === "menu") {  // app icons in a 2 x 2 grid, the chosen one named on top, like the smartphones' home screen
        const it = this.items();
        main = `<div class="lite-title">${esc(it[this.sel][1])}</div><div class="lite-apps">${it.map((x, i) => `<span class="lite-app${i === this.sel ? " on" : ""}">${icon(x[0], 34)}<small>${esc(x[1])}</small></span>`).join("")}</div>`;
        l = LB.select; r = LB.back;
      }
      else if (S === "dial") { main = `${esc(LB.phone)}\n\n<span style="font-size:18px">${esc(this.buf)}_</span>`; l = LB.call; r = LB.clear; }
      else if (S === "ussd") { main = esc(this.ussd.text) + (this.ussd.ended ? "" : `\n> ${this.ussd.secret ? "*".repeat(this.buf.length) : esc(this.buf)}_`); l = this.ussd.ended ? LB.ok : LB.send; r = this.ussd.ended ? "" : LB.cancel; }
      else if (S === "msgs") { const th = (this.phone.threads["7380"] || []).slice(-7); main = th.length ? th.map(x => `${x.dir === "in" ? "<" : ">"} ${esc(x.body.slice(0, 120))}`).join("\n") : esc(LB.nomsg); l = LB.newMsg; r = LB.back; }
      else if (S === "compose") { main = `${esc(LB.typeMsg)}\n\n${esc(this.buf)}_`; l = LB.send; r = LB.clear; }
      else if (S === "contacts") { main = this.contacts().map((c, i) => (i === this.sel ? `<span class="sel">${esc(c[0])}</span>` : esc(c[0]))).join("\n"); l = LB.call; r = LB.back; }
      else if (S === "calc") { main = `${esc(LB.calc)}\n\n<div style="text-align:right;font-size:22px">${esc(this.calc.cur)}</div>\n\n▲ +  ▼ −  ◀ ×  ▶ ÷  OK =`; l = "="; r = LB.clear; }
      else if (S === "settings") { const rows = [`${LB.sound}: ${A.on() ? "ON" : "OFF"}`, `${LB.volume}: ${Math.round(parseFloat(C.store.get("cwas_vol", "0.7")) * 10)}/10`, `${LB.about}`]; main = rows.map((x, i) => (i === this.setting ? `<span class="sel">${esc(x)}</span>` : esc(x))).join("\n") + `\n\nWinebald Lite 2\nIMEI ${IMEI}\n${esc(this.phone.number)}`; l = LB.ok; r = LB.back; }
      else if (S === "calling") { main = `${esc(this.callee)}\n\n${esc(LB.calling)}`; l = ""; r = LB.back; }
      m.innerHTML = main; this.screen.querySelector("[data-l]").textContent = l; this.screen.querySelector("[data-r]").textContent = r;
    }
    typeMT(k) { const now = Date.now(), set = MT[k]; if (!set) return; if (this.mt.k === k && now - this.mt.t < 900) { this.buf = this.buf.slice(0, -1) + set[++this.mt.n % set.length]; } else { this.buf += set[0]; this.mt = { k, n: 0, t: 0 }; } this.mt.t = now; this.mt.k = k; }
    press(k) {
      if (!this.on) { if (k === "end") this.power(); return; }
      A.play("key", /^[\d*#]$/.test(k) ? k : "x"); const S = this.state, digit = /^[0-9]$/.test(k) || k === "*" || k === "#";
      if (k === "end") { if (S === "home") this.power(); else { if (this.phone.active) this.phone.cancel(); this.state = "home"; this.buf = ""; } return this.render(); }
      if (S === "home") { if (digit) { this.state = "dial"; this.buf = k; } else if (k === "sk1" || k === "ok") { this.state = "menu"; this.sel = 0; } else if (k === "sk2") { this.state = "contacts"; this.sel = 0; } }
      else if (S === "menu") {  // arrows move around the 2 x 2 grid; 1 to 4 open an app directly
        const n = this.items().length;
        if (k === "left") this.sel = (this.sel + n - 1) % n; else if (k === "right") this.sel = (this.sel + 1) % n;
        else if (k === "up") this.sel = (this.sel + n - 2) % n; else if (k === "down") this.sel = (this.sel + 2) % n;
        else if (k === "sk2") this.state = "home"; else if (k === "ok" || k === "sk1") this.openItem(this.items()[this.sel][0]);
        else if (/^[1-4]$/.test(k)) this.openItem(this.items()[+k - 1][0]);
      }
      else if (S === "dial") { if (digit) this.buf += k; else if (k === "sk2") this.buf = this.buf.slice(0, -1); if (!this.buf) this.state = "home"; if (k === "call" || k === "sk1" || k === "ok") this.dialNow(this.buf); }
      else if (S === "ussd") { if (k === "sk2" && !this.ussd.ended) { this.phone.cancel(); this.state = "home"; } else if (k === "sk1" || k === "ok" || k === "call") { if (this.ussd.ended) this.state = "home"; else if (this.buf) { const v = this.buf; this.buf = ""; this.phone.send(v); } } else if (digit) this.buf += k; else if (k === "clear") this.buf = this.buf.slice(0, -1); }
      else if (S === "msgs") { if (k === "sk1" || k === "ok") { this.state = "compose"; this.buf = ""; } else if (k === "sk2") this.state = "menu"; }
      else if (S === "compose") { if (digit) this.typeMT(k); else if (k === "sk2" || k === "clear") this.buf = this.buf.slice(0, -1); else if (k === "sk1" || k === "ok") { if (this.buf.trim()) { const t = this.buf; this.buf = ""; this.state = "msgs"; this.phone.sendSms(t); } } }
      else if (S === "contacts") { const cs = this.contacts(); if (k === "up") this.sel = (this.sel + cs.length - 1) % cs.length; else if (k === "down") this.sel = (this.sel + 1) % cs.length; else if (k === "sk2") this.state = "home"; else if (k === "call" || k === "sk1" || k === "ok") { const c = cs[this.sel]; if (c[1] === "7380") { this.state = "compose"; this.buf = ""; } else this.dialNow(c[1], c[0]); } }
      else if (S === "calc") { this.calcKey(k); }
      else if (S === "settings") { if (k === "up") this.setting = (this.setting + 2) % 3; else if (k === "down") this.setting = (this.setting + 1) % 3; else if (k === "sk2") this.state = "menu"; else if (k === "ok" || k === "sk1") { if (this.setting === 0) A.set(!A.on()); } else if ((k === "left" || k === "right") && this.setting === 1) { const v = Math.max(0, Math.min(1, parseFloat(C.store.get("cwas_vol", "0.7")) + (k === "right" ? .1 : -.1))); C.store.set("cwas_vol", v.toFixed(1)); A.play("vol"); } }
      else if (S === "calling") { if (k === "sk2" || k === "end") this.state = "home"; }
      this.render();
    }
    openItem(id) { this.sel = 0; if (id === "phone") { this.state = "dial"; this.buf = ""; } else if (id === "messages") { this.state = "msgs"; this.phone.unread = 0; } else if (id === "contacts") this.state = "contacts"; else if (id === "calc") { this.state = "calc"; this.calc = { cur: "0", acc: null, op: null, fresh: true }; } else if (id === "settings") { this.state = "settings"; this.setting = 0; } else if (id === "cwas") this.dialNow(DIAL); }
    dialNow(num, name) {
      if (/^\*[\d*]+#$/.test(num)) { if (num === DIAL) { this.phone.dial(); } else { this.state = "ussd"; this.ussd = { text: "Connection problem or invalid MMI code.", ended: true }; A.play("error"); } return; }
      this.state = "calling"; this.callee = name || num; A.play("ring"); const id = setTimeout(() => { if (this.state === "calling") { this.callee += "\n" + LB.noanswer; this.render(); setTimeout(() => { this.state = "home"; this.render(); }, 1500); } }, 5000); this.render(); return id;
    }
    calcKey(k) {
      const c = this.calc, opk = { up: "+", down: "-", left: "*", right: "/" }[k];
      if (/^\d$/.test(k)) { c.cur = c.fresh || c.cur === "0" ? k : c.cur + k; c.fresh = false; } else if (k === "#") { if (!c.cur.includes(".")) c.cur += "."; c.fresh = false; }
      else if (k === "sk2" || k === "clear") { this.calc = { cur: "0", acc: null, op: null, fresh: true }; if (k === "sk2") this.state = "menu"; }
      else if (opk) { if (c.op && !c.fresh) this.calcEval(); c.acc = c.cur; c.op = opk; c.fresh = true; }
      else if (k === "ok" || k === "sk1") { this.calcEval(); c.acc = null; c.op = null; c.fresh = true; }
    }
    calcEval() { const c = this.calc; if (c.acc === null || !c.op) return; const a = parseFloat(c.acc), b = parseFloat(c.cur), r = { "+": a + b, "-": a - b, "*": a * b, "/": b === 0 ? NaN : a / b }[c.op]; c.cur = Number.isFinite(r) ? String(+r.toFixed(8)) : "Error"; }
  }

  /* ── device shells ── */
  const DEVICES = { lite: { name: "Winebald Lite 2", w: 260, h: 580 }, nova: { name: "Winebald Nova 6", w: 296, h: 612 }, max: { name: "Winebald Max 9 Pro", w: 326, h: 684 } };
  let phone = null, ui = null, model = C.store.get("cwas_device", "nova"), tourToken = 0;
  const backFace = key => {
    if (key === "lite") return `<div class="body"></div><div class="lens" style="left:50%;top:34px;width:34px;height:34px;margin-left:-17px"></div><div class="flash-led" style="left:50%;top:44px;width:10px;height:10px;margin-left:34px"></div><div style="position:absolute;left:50%;top:120px;transform:translateX(-50%);display:grid;gap:4px">${"<i style='display:block;width:70px;height:3px;border-radius:3px;background:rgba(0,0,0,.4)'></i>".repeat(5)}</div><div class="wb-mark" style="top:300px">WINEBALD</div>`;
    if (key === "nova") return `<div class="body"></div><div class="plate" style="left:20px;top:20px;width:112px;height:112px;border-radius:30px"></div><div class="lens" style="left:36px;top:34px;width:38px;height:38px"></div><div class="lens" style="left:76px;top:78px;width:38px;height:38px"></div><div class="flash-led" style="left:96px;top:40px;width:14px;height:14px"></div><div class="wb-mark" style="top:300px">WINEBALD</div>`;
    return `<div class="body"></div><div class="plate" style="left:14px;right:14px;top:64px;height:92px;border-radius:46px"></div><div class="lens" style="left:36px;top:80px;width:44px;height:44px"></div><div class="lens" style="left:96px;top:80px;width:44px;height:44px"></div><div class="lens" style="left:156px;top:80px;width:44px;height:44px"></div><div class="flash-led" style="right:44px;top:92px;width:22px;height:22px"></div><div class="wb-mark" style="top:330px">WINEBALD</div>`;
  };
  const frontFace = key => key === "lite"
    ? `<div class="body"></div><div class="lite-speaker"></div><div class="lite-screen"></div>
       <div class="softkeys"><button class="sk" data-k="sk1" type="button"></button><button class="sk" data-k="sk2" type="button"></button></div>
       <div class="dpad"><button class="u" data-k="up" aria-label="Up" type="button">▲</button><button class="d" data-k="down" aria-label="Down" type="button">▼</button><button class="lf" data-k="left" aria-label="Left" type="button">◀</button><button class="rt" data-k="right" aria-label="Right" type="button">▶</button><button class="ok" data-k="ok" aria-label="OK" type="button"></button></div>
       <button class="k call" data-k="call" style="position:absolute;left:24px;top:322px;width:64px" aria-label="Call" type="button">✆</button><button class="k end" data-k="end" style="position:absolute;right:24px;top:322px;width:64px" aria-label="End" type="button">✕</button>
       <div class="keys">${"123456789*0#".split("").map(k => `<button class="k" data-k="${k}" type="button">${k}<small>${LETTERS[k] || ""}</small></button>`).join("")}</div>
       <button class="hw l" data-vol="1" style="top:120px;height:44px" aria-label="Volume up" type="button"></button><button class="hw l" data-vol="-1" style="top:174px;height:44px" aria-label="Volume down" type="button"></button>`
    : `<div class="body"></div><div class="bezel"></div><div class="screen" data-screen></div>${key === "nova" ? '<div class="notch"></div>' : '<div class="punch"></div>'}
       <button class="hw l" data-vol="1" style="top:150px;height:52px" aria-label="Volume up" type="button"></button><button class="hw l" data-vol="-1" style="top:212px;height:52px" aria-label="Volume down" type="button"></button><button class="hw r" data-power style="top:190px;height:78px" aria-label="Power" type="button"></button>`;
  if (lab) {
  function fit() { const d = DEVICES[model], s = Math.min(1.1, (stage.clientHeight - 50) / d.h, (stage.clientWidth - 30) / d.w); rig.style.setProperty("--s", Math.max(.45, s).toFixed(3)); }
  function mount() {
    if (ui) ui.destroy(); tourToken++; if (phone && phone.number !== phoneIn.value.trim()) { phone.destroy(); phone = null; }
    if (!phone) phone = new Phone(phoneIn.value.trim());
    const d = DEVICES[model]; rig.innerHTML = ""; rig.style.setProperty("--w", d.w + "px"); rig.style.setProperty("--h", d.h + "px");
    const fl = h("div", "flipper dev-" + model), front = h("div", "face front", frontFace(model)), back = h("div", "face back", backFace(model));
    fl.append(front, back); rig.appendChild(fl); fl.style.width = d.w + "px"; fl.style.height = d.h + "px";
    $$("[data-device]").forEach(b => b.setAttribute("aria-pressed", b.dataset.device === model ? "true" : "false"));
    if (model === "lite") { const ls = $(".lite-screen", front); ui = new LiteOS(ls, phone); front.addEventListener("click", e => { const k = e.target.closest("[data-k]"); if (k) { ui.press(k.dataset.k); k.classList.add("hit"); setTimeout(() => k.classList.remove("hit"), 90); } }); ui.model = "lite"; }
    else { const sc = $("[data-screen]", front); ui = new SmartOS(sc, phone, d.name); const pw = $("[data-power]", front); pw.addEventListener("click", () => ui.power()); sc.addEventListener("dblclick", () => { if (ui.locked) ui.unlock(); }); }
    front.addEventListener("click", e => { const v = e.target.closest("[data-vol]"); if (v) { const nv = Math.max(0, Math.min(1, parseFloat(C.store.get("cwas_vol", "0.7")) + .1 * +v.dataset.vol)); C.store.set("cwas_vol", nv.toFixed(1)); A.play("vol"); hud(front, nv); } });
    fl.addEventListener("dblclick", e => { if (e.target.closest(".face.back") || e.target.classList.contains("body")) fl.classList.toggle("flipped"); });
    fit();
  }
  function hud(front, v) {
    let el = $(".vhud", front); if (!el) { el = h("div", "vhud"); el.style.cssText = "position:absolute;z-index:90;left:50%;top:52px;transform:translateX(-50%);padding:6px 14px;border-radius:99px;background:rgba(0,0,0,.72);color:#fff;font:700 12px system-ui;letter-spacing:2px;pointer-events:none;transition:opacity .3s"; (model === "lite" ? $(".lite-screen", front) : $("[data-screen]", front)).appendChild(el); }
    el.textContent = "▮".repeat(Math.round(v * 10)) + "▯".repeat(10 - Math.round(v * 10)); el.style.opacity = "1"; clearTimeout(el._t); el._t = setTimeout(() => { el.style.opacity = "0"; }, 1200);
  }

  /* ── controls ── */
  $$("[data-device]").forEach(b => b.addEventListener("click", () => { model = b.dataset.device; C.store.set("cwas_device", model); A.play("tap"); mount(); }));
  $("[data-flip]").addEventListener("click", () => { const f = $(".flipper", rig); if (f) f.classList.toggle("flipped"); A.play("tap"); });
  $("[data-reset]").addEventListener("click", () => { A.play("poweroff"); if (phone) { phone.destroy(); phone = null; } consoleEl.innerHTML = ""; mount(); });
  phoneIn.addEventListener("change", () => { const v = phoneIn.value.trim(); if (v) { mount(); } });
  $$("[data-pick]").forEach(b => b.addEventListener("click", () => { let v = b.dataset.pick; if (v === "new") v = "+2613400009" + String(Math.floor(Math.random() * 900) + 100); phoneIn.value = v; A.play("tap"); mount(); }));
  addEventListener("resize", fit); document.addEventListener("cwas:theme", () => { /* colours follow the theme through CSS variables */ });
  document.addEventListener("keydown", e => {
    if (!ui || e.target.matches("input,textarea,select") || e.metaKey || e.ctrlKey) return;
    if (model === "lite") { const m = { Enter: "ok", Backspace: "clear", ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right", Escape: "end" }[e.key]; if (m) { e.preventDefault(); ui.press(m); } else if (/^[0-9*#]$/.test(e.key)) ui.press(e.key); }
  });

  /* ── guided runs: the phone types by itself ── */
  /* each run is planned by the server against the live data (web.py sim_tour), so it can finish every time */
  $$("[data-tour]").forEach(b => b.addEventListener("click", async () => {
    let t; try { t = await fetch(`/simulator/api/tour/${b.dataset.tour}`, { credentials: "same-origin", cache: "no-store" }).then(r => (r.ok ? r.json() : Promise.reject(new Error(String(r.status))))); } catch (e) { C.toast(LB.tourFail, "err"); return; }
    phoneIn.value = t.phone; mount(); const tok = tourToken; await sleep(500);
    if (model !== "lite") { ui.wake && (ui.on ? 0 : ui.wake()); ui.unlock(); await sleep(500); ui.open("phone"); await sleep(600); const v = ui.stack[ui.stack.length - 1]; for (const ch of DIAL) { if (tok !== tourToken) return; A.play("key", ch); v._set(v._num() + ch); await sleep(160); } await sleep(400); ui.call(DIAL); }
    else { for (const ch of DIAL) { ui.press(ch); await sleep(140); } ui.press("call"); }
    for (let w = 0; w < 80 && tok === tourToken && !(phone && phone.active); w++) await sleep(100);  // the call connects first
    for (const s of t.steps) { await sleep(1300); if (tok !== tourToken || !phone || !phone.active) return;  /* ended early: the phone shows the engine's reason */ if (model === "lite") { A.play("key", "5"); ui.buf = s; ui.render(); await sleep(600); ui.press("sk1"); } else { ui.typeUssd(s); A.play("tap"); await sleep(600); if (tok !== tourToken) return; phone.send(s); } }
    await sleep(1200); if (tok === tourToken) C.toast(LB.tourDone, "ok");
  }));

  mount();
  if (model !== "lite") setTimeout(() => { if (ui.wake) ui.on = true; }, 0);
  }

  /* ── homepage and /access: the same devices play the engine's own screens ── */
  if (demoRow) {
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches, conn = navigator.connection || {}, still = reduce || conn.saveData || /2g/.test(conn.effectiveType || "");
    const secret = t => /PIN|kaody|code|Confirm PIN|Hamafiso ny PIN/i.test(String(t).split("\n")[0]);
    /* the same interface as Phone above, fed by the script instead of the network */
    class ScriptPhone {
      constructor() { this.number = ""; this.subs = new Set(); this.reset([]); }
      reset(screens) { this.threads = { "7380": [] }; this.unread = 0; this.active = false; this.screens = screens.slice(); }
      destroy() {}
      emit(ev, d) { this.subs.forEach(f => f(ev, d)); }
      add(dir, body, at, addr = "7380") { (this.threads[addr] = this.threads[addr] || []).push({ dir, body, at: at || clock() }); if (dir === "in") this.unread++; }
      next() { const sc = this.screens.shift(); if (!sc) return; this.active = !sc.end; A.play("receive"); this.emit("ussd", { text: sc.text, ended: sc.end, secret: !sc.end && secret(sc.text) }); }
      dial() { this.active = true; A.play("connect"); setTimeout(() => this.next(), 650); }
      send() { if (!this.active) return; A.play("send"); setTimeout(() => this.next(), 450); }
      sendSms(text) { this.add("out", text); this.emit("threads"); A.play("send"); }
      receive(body) { const m = { body, at: clock() }; this.add("in", body); this.emit("sms", m); this.emit("threads"); }
      cancel() { this.active = false; this.emit("ussd", { text: "", ended: true, gone: true }); }
    }
    let devs = [];
    /* The three phones sit side by side while they stay readable. On a small screen they become a swipe row: one
       phone at a time with the next one peeking in, always inside the page. The column width is measured on the
       row's parent, because the swipe row itself reaches to the screen edges. Height is capped so a phone never
       fills more than about three quarters of the screen (landscape phones). */
    const size = () => {
      const specs = devs.length ? devs.map(d => DEVICES[d.key]) : [DEVICES.lite, DEVICES.nova, DEVICES.max];
      const total = specs.reduce((a, d) => a + d.w, 0), widest = Math.max(...specs.map(d => d.w)), tallest = Math.max(...specs.map(d => d.h));
      const col = (demoRow.parentElement || demoRow).clientWidth, gap = parseFloat(getComputedStyle(demoRow).columnGap) || 16;
      let s = (col - gap * (specs.length - 1) - 8) / total;
      const swipe = s < .46;
      if (swipe) s = Math.min(.62, (col * .74) / widest);
      s = Math.max(.4, Math.min(.74, (innerHeight * .74) / tallest, s));
      demoRow.classList.toggle("is-swipe", swipe);
      demoRow.style.setProperty("--s", s.toFixed(3));
    };
    const wait = async (d, ms) => { const my = d.run; let left = ms; while (left > 0) { await sleep(Math.min(100, left)); if (my !== d.run) throw new Error("restart"); if (!d.paused && demoOn && !document.hidden) left -= 100; } };
    const hit = el => { if (!el) return; el.classList.add("hit"); setTimeout(() => el.classList.remove("hit"), 140); };
    const lock = d => { d.ui.stack.slice().forEach(() => d.ui.close()); d.ui.locked = true; d.ui.os.classList.add("locked"); d.ui.$("[data-lock]").classList.remove("gone"); };
    const smart = async (d, steps) => {
      const { ui, phone } = d;
      phone.reset(steps.filter(x => x[0] === "screen" || x[0] === "end").map(x => ({ text: x[1], end: x[0] === "end" })));
      await wait(d, 1600); ui.unlock(); await wait(d, 900); ui.open("phone"); await wait(d, 900);
      const v = ui.stack[ui.stack.length - 1];
      for (const [op, arg] of steps) {
        if (op === "dial") { for (const ch of arg) { hit($(`.pad button[data-key="${ch}"]`, v)); A.play("key", ch); v._set(v._num() + ch); await wait(d, 190); } await wait(d, 450); }
        else if (op === "call") { hit($(".callbtn", v)); ui.call(v._num()); await wait(d, 2200); }
        else if (op === "key" || op === "pin") {
          for (const ch of arg) { const i = $(".ussd-card input", ui.screen); if (i) i.value += ch; A.play("key", ch); await wait(d, 190); }
          await wait(d, 400); const i = $(".ussd-card input", ui.screen); hit($(".ussd-card [data-ok]", ui.screen)); if (i) i.value = ""; phone.send(); await wait(d, 2000);
        } else if (op === "end") { await wait(d, 3000); const ok = $(".ussd-card [data-ok]", ui.screen); if (ok) { hit(ok); ok.click(); } await wait(d, 700); }
      }
    };
    const sms = async (d, steps) => {
      const { ui, phone } = d; phone.reset([]);
      await wait(d, 1600); ui.unlock(); await wait(d, 900); ui.open("messages"); await wait(d, 1000);
      for (const [op, arg] of steps) {
        if (op === "type") {
          for (const ch of arg) { const i = $(".compose input", ui.screen); if (i) i.value += ch; A.play("key", /\d/.test(ch) ? ch : "x"); await wait(d, 150); }
          await wait(d, 350); const f = $(".compose", ui.screen); hit($(".compose button", ui.screen)); if (f) f.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); await wait(d, 1100);
        } else if (op === "in") { phone.receive(arg); await wait(d, 2600); }
        else if (op === "wait") await wait(d, arg);
      }
      await wait(d, 1400);
    };
    const lite = async (d, steps) => {
      const { ui, phone, front } = d;
      phone.reset(steps.filter(x => x[0] === "screen" || x[0] === "end").map(x => ({ text: x[1], end: x[0] === "end" })));
      const press = k => { hit($(`[data-k="${k}"]`, front)); ui.press(k); };
      await wait(d, 1200);
      for (const [op, arg] of steps) {
        if (op === "dial") { for (const ch of arg) { press(ch); await wait(d, 190); } await wait(d, 400); }
        else if (op === "call") { press("call"); await wait(d, 2200); }
        else if (op === "key" || op === "pin") { for (const ch of arg) { press(ch); await wait(d, 220); } await wait(d, 350); press("sk1"); await wait(d, 2000); }
        else if (op === "screen" || op === "end") { /* the phone shows it when it arrives */ }
      }
      await wait(d, 3000); press("end");
    };
    const play = async (d, delay) => {
      const my = ++d.run, steps = init.script[d.key] || [];
      try {
        await wait(d, delay);
        while (my === d.run) {
          if (d.key === "lite") await lite(d, steps); else if (d.key === "nova") await smart(d, steps); else await sms(d, steps);
          await wait(d, 2200); if (d.key !== "lite") { d.phone.cancel(); lock(d); } await wait(d, 600);
        }
      } catch (e) { /* restarted: a language switch or a new build of the row */ }
    };
    /* reduced motion or Save-Data: one representative screen of each phone, and no timers */
    const settle = d => {
      const steps = init.script[d.key] || [], screens = steps.filter(x => x[0] === "screen");
      if (d.key === "lite") { d.phone.reset([{ text: (screens[1] || screens[0] || ["", ""])[1], end: false }]); d.phone.dial(); return; }
      d.ui.unlock();
      if (d.key === "nova") { d.ui.open("phone"); d.phone.reset([{ text: (screens[1] || screens[0] || ["", ""])[1], end: false }]); d.phone.next(); return; }
      d.ui.open("messages"); steps.forEach(([op, arg]) => { if (op === "type") d.phone.sendSms(arg); else if (op === "in") d.phone.add("in", arg); }); d.phone.emit("threads");
    };
    const build = () => {
      devs.forEach(d => { d.run++; d.ui.destroy(); });
      devs = $$("[data-dev]", demoRow).map(fig => {
        const key = fig.dataset.dev, spec = DEVICES[key], box = $(".pd-rig", fig); box.innerHTML = "";
        box.style.setProperty("--w", spec.w + "px"); box.style.setProperty("--h", spec.h + "px");
        const fl = h("div", "flipper dev-" + key), front = h("div", "face front", frontFace(key));
        fl.style.width = spec.w + "px"; fl.style.height = spec.h + "px"; fl.appendChild(front); box.appendChild(fl);
        const phone = new ScriptPhone(), ui = key === "lite" ? new LiteOS($(".lite-screen", front), phone) : new SmartOS($("[data-screen]", front), phone, spec.name);
        const d = { key, fig, front, phone, ui, run: 0, paused: false };
        if (!fig.dataset.bound) {
          fig.dataset.bound = "1";
          const toggle = () => { const x = devs.find(y => y.fig === fig); if (!x) return; x.paused = !x.paused; fig.classList.toggle("is-paused", x.paused); C.audio.play("tap"); };
          fig.addEventListener("click", toggle); fig.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
        }
        return d;
      });
      size();
      if (still) devs.forEach(settle); else devs.forEach((d, i) => play(d, i * 1500));
    };
    if ("IntersectionObserver" in window) new IntersectionObserver(es => es.forEach(en => { demoOn = en.isIntersecting; }), { threshold: 0.2 }).observe(demoRow);
    addEventListener("resize", size);
    document.addEventListener("cwas:lang", () => { init = readInit(); LB = init.labels; build(); });
    build();
  }
})();
