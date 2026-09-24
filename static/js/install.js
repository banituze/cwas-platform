/* CWAS install card (templates/partials/install.html): a dismissable liquid glass card that offers to install the app.
   Where the browser can install (Chrome, Edge, Android) it shows an Install button that opens the browser's own prompt; on
   iPhone and iPad it shows the two taps that add CWAS to the home screen. The icon is the app icon of the theme on screen and
   follows theme changes. Dismissed, the card stays away for two weeks; it never shows inside the installed app. */
(() => {
  const card = document.querySelector("[data-install]"); if (!card) return;
  if (matchMedia("(display-mode: standalone)").matches || navigator.standalone === true) return;
  const KEY = "cwas-install-dismissed", QUIET = 14 * 864e5, q = s => card.querySelector(s);
  let dismissed = 0; try { dismissed = Number(localStorage.getItem(KEY)) || 0; } catch (e) { dismissed = 0; }
  if (Date.now() - dismissed < QUIET) return;
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const icon = q("[data-install-icon]");
  const syncIcon = () => { const th = document.documentElement.dataset.theme || "saina"; icon.src = icon.getAttribute("src").replace(/favicon-[a-z]+\.svg/, "favicon-" + th + ".svg"); };
  let prompt = null, timer = 0;
  const show = delay => {
    clearTimeout(timer);
    timer = setTimeout(() => { if (!card.hidden) return; syncIcon(); card.hidden = false; requestAnimationFrame(() => requestAnimationFrame(() => card.classList.add("is-in"))); }, delay);
  };
  const hide = () => {
    clearTimeout(timer); card.classList.remove("is-in"); setTimeout(() => { card.hidden = true; }, 450);
    try { localStorage.setItem(KEY, String(Date.now())); } catch (e) { /* storage blocked: it simply comes back next visit */ }
  };
  addEventListener("beforeinstallprompt", e => { e.preventDefault(); prompt = e; show(2500); });
  addEventListener("appinstalled", hide);
  document.addEventListener("cwas:theme", syncIcon);
  q("[data-install-close]").addEventListener("click", hide);
  q("[data-install-later]").addEventListener("click", hide);
  card.addEventListener("keydown", e => { if (e.key === "Escape") hide(); });
  q("[data-install-go]").addEventListener("click", async () => {
    if (!prompt) return;
    const p = prompt; prompt = null; p.prompt();
    try { await p.userChoice; } catch (e) { /* the browser closed its prompt */ }
    hide();
  });
  if (ios) { q("[data-install-text]").hidden = true; q("[data-install-ios]").hidden = false; q("[data-install-actions]").hidden = true; show(4000); }
})();
