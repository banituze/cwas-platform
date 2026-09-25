/* CWAS phone numbers: the country picker beside a phone field (templates/partials/phone.html). Madagascar comes first; every
   other country libphonenumber knows is listed by flag, name and calling code. The number's length is checked for the chosen
   country as it is typed; the server checks the number fully (services.parse_phone). Flags are cells of one sprite
   (static/img/flags.webp) that the page has already loaded, so the list draws complete the moment it opens. Typing or pasting
   an international number (+230 ...) switches the picker to that country. */
(() => {
  const data = JSON.parse((document.querySelector("script[data-cc-data]") || {}).textContent || "[]"), by = {};
  data.forEach(r => { by[r[0]] = r; });
  // calling code -> country; for a code several countries share, the main one (a row's sixth value) wins: +1 is the US
  const byCode = {}; data.forEach(r => { const k = String(r[1]); if (!byCode[k] || r[5]) byCode[k] = r; });
  const lang = document.documentElement.lang || "en";
  let names = null; try { names = new Intl.DisplayNames([lang === "mg" ? "fr" : lang, "en"], { type: "region" }); } catch (e) { names = null; }
  const nameOf = r => { try { return (names && names.of(r)) || r; } catch (e) { return r; } };
  document.querySelectorAll("[data-phone]").forEach(box => {
    const q = s => box.querySelector(s);
    const btn = q("[data-cc-btn]"), panel = q("[data-cc-panel]"), list = q("[data-cc-list]"), search = q("[data-cc-search]");
    const hid = q("[data-cc]"), input = q(".phone-row .field"), flag = q("[data-cc-flag]"), code = q("[data-cc-code]");
    const either = box.hasAttribute("data-either"), bad = box.dataset.bad || "";
    // a row's fifth value is its flag's cell in the sprite: 16 cells to a row, each 22 x 16 CSS pixels
    const cell = r => `${-(r[4] % 16) * 22}px ${-Math.floor(r[4] / 16) * 16}px`;
    const phoneLike = v => /^[\d\s+().-]*$/.test(v);
    const check = () => {
      const v = input.value.trim(), r = by[hid.value];
      if (either) box.classList.toggle("is-email", !!v && !phoneLike(v));
      if (!v || !r || !phoneLike(v)) { input.setCustomValidity(""); return; }
      let d = v.replace(/\D/g, "");
      if (v.startsWith("+")) { const cc = String(r[1]); if (!d.startsWith(cc)) { input.setCustomValidity(""); return; } d = d.slice(cc.length); }
      else if (d.length > 1 && d[0] === "0") d = d.slice(1);  // the national trunk prefix
      input.setCustomValidity(r[3].length && !r[3].includes(d.length) ? bad.replace("{country}", nameOf(r[0])) : "");
    };
    const close = () => { panel.hidden = true; btn.setAttribute("aria-expanded", "false"); };
    const set = r => {
      hid.value = r[0]; code.textContent = "+" + r[1]; flag.style.backgroundPosition = cell(r);
      input.placeholder = either ? (input.dataset.ph || "").replace("{example}", r[2] || "") : r[2] || "";
    };
    const pick = r => { set(r); close(); check(); input.focus(); };
    // an international number typed or pasted (+230 5...) picks its country; calling codes never begin another code, so
    // the first match is the one. The country on show stays when it already has that code (Canada for +1, say)
    const detect = () => {
      const m = input.value.match(/^\s*\+\s*(\d{1,3})/); if (!m) return;
      for (let n = 1; n <= m[1].length; n++) {
        const k = m[1].slice(0, n), r = byCode[k];
        if (r) { if (!by[hid.value] || String(by[hid.value][1]) !== k) set(r); return; }
      }
    };
    const render = f => {
      list.textContent = "";
      data.filter(r => !f || nameOf(r[0]).toLowerCase().includes(f) || ("+" + r[1]).includes(f) || r[0].toLowerCase() === f).forEach(r => {
        const li = document.createElement("li"), b = document.createElement("button"), im = document.createElement("i"), s = document.createElement("span"), c = document.createElement("b");
        b.type = "button"; b.dataset.r = r[0]; b.setAttribute("role", "option"); b.setAttribute("aria-selected", r[0] === hid.value ? "true" : "false");
        im.className = "cc-flag"; im.style.backgroundPosition = cell(r); im.setAttribute("aria-hidden", "true");
        s.textContent = nameOf(r[0]); c.textContent = "+" + r[1]; b.append(im, s, c); li.append(b); list.append(li);
      });
    };
    const open = () => {
      render(""); panel.hidden = false; btn.setAttribute("aria-expanded", "true"); search.value = ""; search.focus();
      const sel = list.querySelector("[aria-selected=true]"); if (sel) sel.scrollIntoView({ block: "nearest" });
    };
    btn.addEventListener("click", () => (panel.hidden ? open() : close()));
    search.addEventListener("input", () => render(search.value.trim().toLowerCase()));
    search.addEventListener("keydown", e => {
      const first = list.querySelector("button");
      if (e.key === "ArrowDown" && first) { e.preventDefault(); first.focus(); }
      if (e.key === "Enter") { e.preventDefault(); if (first) pick(by[first.dataset.r]); }
    });
    list.addEventListener("click", e => { const b = e.target.closest("[data-r]"); if (b) pick(by[b.dataset.r]); });
    list.addEventListener("keydown", e => {
      const b = e.target.closest("[data-r]"); if (!b) return; const li = b.parentElement;
      if (e.key === "ArrowDown" && li.nextElementSibling) { e.preventDefault(); li.nextElementSibling.firstChild.focus(); }
      if (e.key === "ArrowUp") { e.preventDefault(); (li.previousElementSibling ? li.previousElementSibling.firstChild : search).focus(); }
    });
    document.addEventListener("click", e => { if (!box.contains(e.target)) close(); });
    box.addEventListener("keydown", e => { if (e.key === "Escape" && !panel.hidden) { e.stopPropagation(); close(); btn.focus(); } });
    input.addEventListener("input", () => { detect(); check(); });
    check();
  });
})();