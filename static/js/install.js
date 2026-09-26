/* CWAS install card (templates/partials/install.html): a small liquid glass card that offers to install the app. It is part
   of the first paint of every page, so it is on screen the moment the page is; boot.js takes it away before anything is
   drawn inside the installed app, and marks iPhone and iPad (html.is-ios), which get the two taps that add CWAS to the home
   screen. Where the browser can install (Chrome, Edge, Android) the Install button joins the card as soon as the browser
   offers its prompt, and opens that prompt. Fixed to the bottom of the screen, the card never moves the page. Once closed it
   stays off the other pages (boot.js reads the choice); the homepage and every theme change show it again and
   forget the close. Its icon is the theme's own logo tile, drawn
   by CSS, so it changes colour the instant the theme does. */
(() => {
  const card = document.querySelector("[data-install]"), root = document.documentElement;
  if (!card) return;
  if (matchMedia("(display-mode: standalone)").matches || navigator.standalone === true) { root.classList.add("no-install"); return; }   // never inside the installed app
  const go = card.querySelector("[data-install-go]");
  const manual = card.querySelector("[data-install-manual]");
  const ios = card.querySelector("[data-install-ios]");
  let prompt = window.__bip || null, installed = false, timer = 0;
  const KEY = "cwas-install-closed";
  // closing slides the card away and takes it out of the page; when the visitor closed it, the choice is remembered
  const hide = remember => {
    card.classList.add("is-out"); clearTimeout(timer); timer = setTimeout(() => root.classList.add("no-install"), 320);
    if (remember) try { localStorage.setItem(KEY, "1"); } catch (e) { /* storage blocked: only this page forgets it */ }
  };
  const show = () => {
    if (installed) return;
    clearTimeout(timer); card.classList.remove("is-out"); root.classList.remove("no-install");
    try { localStorage.removeItem(KEY); } catch (e) { /* storage blocked */ }
  };
  addEventListener("beforeinstallprompt", e => { e.preventDefault(); prompt = e; });
  addEventListener("appinstalled", () => { installed = true; hide(); });
  document.addEventListener("cwas:theme", show);   // every theme change brings the card back
  card.querySelector("[data-install-close]").addEventListener("click", () => hide(true));
  card.addEventListener("keydown", e => { if (e.key === "Escape") hide(true); });
  go.addEventListener("click", async () => {
    if (!prompt) {
      card.querySelector("[data-install-text]").hidden = true; if (root.classList.contains("is-ios") && ios) ios.hidden = false;
      else manual.hidden = false; return;
    }
    const p = prompt; prompt = null; p.prompt();
    try { await p.userChoice; } catch (e) { /* the browser closed its prompt */ }
    hide();
  });
})();