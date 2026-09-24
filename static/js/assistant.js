/* Assistant: saved chats, file attachments, voice input and read-aloud. */
(() => {
  "use strict";
  const root = document.querySelector("[data-chat]"); if (!root) return;
  const $ = (s, r = root) => r.querySelector(s), $$ = (s, r = root) => Array.from(r.querySelectorAll(s));
  const init = JSON.parse(document.getElementById("chat-init").textContent), L = init.labels;
  const C = window.CWAS, log = $("[data-log]"), form = $("[data-form]"), ta = $("textarea"), fileIn = $("[data-file]"), pendingEl = $("[data-pending]");
  const threadsEl = $("[data-threads]"), titleEl = $("[data-title]");
  let threadId = init.active || null, pending = [], busy = false;
  const ico = n => `<svg class="icon" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt !== undefined) e.textContent = txt; return e; };

  /* read replies aloud */
  const ttsBox = $("[data-tts]", document), tts = "speechSynthesis" in window;
  const lang = { mg: "fr-FR", fr: "fr-FR", en: "en-US" }[root.dataset.lang] || "en-US";
  ttsBox.checked = tts && C.store.get("cwas_tts", "0") === "1"; ttsBox.disabled = !tts;
  ttsBox.addEventListener("change", () => { C.store.set("cwas_tts", ttsBox.checked ? "1" : "0"); if (!ttsBox.checked && tts) speechSynthesis.cancel(); C.audio.play("tap"); });
  const speak = text => { if (!tts) return; speechSynthesis.cancel(); const u = new SpeechSynthesisUtterance(text.replace(/[-•*_#]/g, " ")); u.lang = lang; const v = speechSynthesis.getVoices().find(x => x.lang.toLowerCase().startsWith(lang.slice(0, 2))); if (v) u.voice = v; speechSynthesis.speak(u); };

  /* voice input */
  const voiceBox = $("[data-voice]", document), mic = $("[data-mic]"), SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null;
  voiceBox.checked = !!SR && C.store.get("cwas_voice", "0") === "1"; voiceBox.disabled = !SR;
  if (!SR) voiceBox.closest("label").title = L.noMic;
  const syncMic = () => { mic.hidden = !voiceBox.checked; };
  syncMic();
  voiceBox.addEventListener("change", () => { C.store.set("cwas_voice", voiceBox.checked ? "1" : "0"); syncMic(); if (!voiceBox.checked && rec) rec.stop(); C.audio.play("tap"); });
  mic.addEventListener("click", () => {
    if (!SR) return C.toast(L.noMic, "err");
    if (rec) { rec.stop(); return; }
    rec = new SR(); rec.lang = lang; rec.interimResults = true; rec.continuous = false;
    const base = ta.value ? ta.value + " " : "";
    rec.onstart = () => { mic.classList.add("mic-on"); C.toast(L.listening, "note", { ms: 2000 }); C.audio.play("connect"); };
    rec.onresult = e => { ta.value = base + Array.from(e.results).map(r => r[0].transcript).join(""); grow(); };
    rec.onend = () => { mic.classList.remove("mic-on"); rec = null; if (ta.value.trim()) C.audio.play("receive"); };
    rec.onerror = () => { mic.classList.remove("mic-on"); rec = null; };
    rec.start();
  });

  /* rendering */
  const addMsg = m => {
    const me = m.role === "user", box = el("div", "msg " + (me ? "me" : "ai"));
    if (m.body) box.appendChild(el("div", "bubble", m.body));
    if (m.files && m.files.length) {
      const a = el("div", "att");
      m.files.forEach(f => {
        const link = document.createElement("a"); link.href = f.url; link.target = "_blank"; link.rel = "noopener";
        if (f.image) { const im = document.createElement("img"); im.src = f.url; im.alt = f.name; link.appendChild(im); }
        else link.insertAdjacentHTML("beforeend", ico("file"));
        link.appendChild(el("span", "", `${f.name} (${f.size})`)); a.appendChild(link);
      });
      box.appendChild(a);
    }
    const meta = el("div", "meta"); meta.appendChild(el("span", "", `${me ? L.you : L.cwas} · ${m.at}`));
    const copy = el("button", "mini"); copy.type = "button"; copy.innerHTML = ico("copy"); copy.title = L.copy; copy.setAttribute("aria-label", L.copy);
    copy.addEventListener("click", () => { navigator.clipboard && navigator.clipboard.writeText(m.body || ""); C.toast(L.copied, "ok", { ms: 1500 }); C.audio.play("tap"); });
    meta.appendChild(copy);
    if (!me && tts) { const sp = el("button", "mini"); sp.type = "button"; sp.innerHTML = ico("speaker"); sp.title = L.read; sp.setAttribute("aria-label", L.read); sp.addEventListener("click", () => speak(m.body)); meta.appendChild(sp); }
    box.appendChild(meta); log.appendChild(box); log.scrollTop = log.scrollHeight; return box;
  };
  const setTitle = t => { titleEl.textContent = t || L.newChat; };
  const exportLink = () => { const a = $("[data-export]"); a.href = threadId ? `/app/assistant/${threadId}/export.txt` : "#"; a.style.visibility = threadId ? "visible" : "hidden"; };
  const upsertThread = t => {
    let row = $(`[data-thread="${t.id}"]`, threadsEl);
    if (!row) {
      row = el("div", "chat-item"); row.setAttribute("role", "button"); row.tabIndex = 0; row.dataset.thread = t.id;
      row.innerHTML = ico("message") + "<span></span>"; const x = el("button", "x"); x.type = "button"; x.dataset.del = t.id; x.setAttribute("aria-label", L.del); x.innerHTML = ico("x"); row.appendChild(x);
      threadsEl.prepend(row);
    }
    $("span", row).textContent = t.title; $$(".chat-item", threadsEl).forEach(r => r.setAttribute("aria-current", r === row ? "true" : "false"));
    $("[data-del-all]").classList.remove("hidden");
  };
  const clearLog = () => { log.innerHTML = ""; $("[data-chips]").hidden = false; };
  const load = id => fetch(`/api/assistant/threads/${id}`, { credentials: "same-origin" }).then(r => r.json()).then(d => {
    threadId = d.id; clearLog(); d.messages.forEach(addMsg); setTitle(d.title); exportLink();
    $$(".chat-item", threadsEl).forEach(r => r.setAttribute("aria-current", r.dataset.thread == d.id ? "true" : "false")); if (d.messages.length) $("[data-chips]").hidden = true;
  });
  const fresh = () => { threadId = null; clearLog(); setTitle(); exportLink(); $$(".chat-item", threadsEl).forEach(r => r.setAttribute("aria-current", "false")); };

  /* attachments */
  const drawPending = () => {
    pendingEl.innerHTML = "";
    pending.forEach((f, i) => { const d = el("div", "", `${f.name} (${Math.max(1, Math.round(f.size / 1024))} KB)`); const x = el("button", "toast-x"); x.type = "button"; x.innerHTML = ico("x"); x.setAttribute("aria-label", "Remove"); x.addEventListener("click", () => { pending.splice(i, 1); drawPending(); }); d.appendChild(x); pendingEl.appendChild(d); });
  };
  $("[data-attach]").addEventListener("click", () => fileIn.click());
  fileIn.addEventListener("change", () => { Array.from(fileIn.files).slice(0, 5 - pending.length).forEach(f => { if (f.size > 10 * 1024 * 1024) C.toast(L.big, "err"); else pending.push(f); }); fileIn.value = ""; drawPending(); C.audio.play("tap"); });
  ["dragover", "drop"].forEach(ev => root.addEventListener(ev, e => { e.preventDefault(); if (ev === "drop") { Array.from(e.dataTransfer.files).slice(0, 5 - pending.length).forEach(f => f.size <= 10 * 1024 * 1024 ? pending.push(f) : C.toast(L.big, "err")); drawPending(); } }));

  /* sending */
  const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(130, ta.scrollHeight) + "px"; };
  ta.addEventListener("input", grow);
  ta.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });
  form.addEventListener("submit", e => {
    e.preventDefault(); if (busy) return;
    const text = ta.value.trim(); if (!text && !pending.length) return;
    busy = true; if (rec) rec.stop();
    const fd = new FormData(); fd.append("message", text); if (threadId) fd.append("thread_id", threadId); pending.forEach(f => fd.append("files", f));
    ta.value = ""; grow(); const sent = pending; pending = []; drawPending(); $("[data-chips]").hidden = true; C.audio.play("send");
    const typing = el("div", "msg ai"); typing.innerHTML = '<div class="bubble typing"><i></i><i></i><i></i></div>'; log.appendChild(typing); log.scrollTop = log.scrollHeight;
    fetch("/api/assistant/message", { method: "POST", credentials: "same-origin", headers: { "X-CSRFToken": C.csrf() }, body: fd }).then(r => r.json().then(d => ({ ok: r.ok, d }))).then(({ ok, d }) => {
      typing.remove();
      if (!ok) { C.toast(d.error === "too_big" ? L.big : L.err, "err"); C.audio.play("error"); pending = sent; drawPending(); return; }
      threadId = d.thread.id; addMsg(d.user); addMsg(d.assistant); setTitle(d.thread.title); upsertThread(d.thread); exportLink();
      C.audio.play("receive"); if (ttsBox.checked) speak(d.assistant.body);
    }).catch(() => { typing.remove(); C.toast(L.err, "err"); C.audio.play("error"); }).finally(() => { busy = false; ta.focus(); });
  });
  $$("[data-suggest]").forEach(b => b.addEventListener("click", () => { ta.value = b.dataset.suggest; form.requestSubmit(); }));

  /* thread list */
  threadsEl.addEventListener("click", e => {
    const del = e.target.closest("[data-del]");
    if (del) { e.stopPropagation(); if (!confirm(L.delOne)) return; fetch(`/api/assistant/threads/${del.dataset.del}/delete`, { method: "POST", credentials: "same-origin", headers: { "X-CSRFToken": C.csrf() } }).then(() => { del.closest(".chat-item").remove(); if (threadId == del.dataset.del) fresh(); if (!$$(".chat-item", threadsEl).length) $("[data-del-all]").classList.add("hidden"); C.audio.play("tap"); }); return; }
    const row = e.target.closest("[data-thread]"); if (row) load(row.dataset.thread);
  });
  threadsEl.addEventListener("keydown", e => { if ((e.key === "Enter" || e.key === " ") && e.target.matches("[data-thread]")) { e.preventDefault(); load(e.target.dataset.thread); } });
  $("[data-new]").addEventListener("click", () => { fresh(); ta.focus(); C.audio.play("tap"); });
  $("[data-del-all]").addEventListener("click", () => { if (!confirm(L.delAll)) return; fetch("/api/assistant/threads/delete-all", { method: "POST", credentials: "same-origin", headers: { "X-CSRFToken": C.csrf() } }).then(() => { threadsEl.innerHTML = ""; $("[data-del-all]").classList.add("hidden"); fresh(); C.audio.play("tap"); }); });
  $("[data-rename]").addEventListener("click", () => { if (!threadId) return; const t = prompt(L.rename, titleEl.textContent); if (t) C.post(`/api/assistant/threads/${threadId}/rename`, { title: t }).then(d => { setTitle(d.title); upsertThread(d); }); });

  init.messages.forEach(addMsg); if (init.messages.length) $("[data-chips]").hidden = true;
  setTitle(init.active ? (init.threads.find(t => t.id === init.active) || {}).title : ""); exportLink();
})();
