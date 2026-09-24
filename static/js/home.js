/* Homepage extras: the gallery tunnel of Madagascar photographs (the demo phones live in sim.js). It pauses out of view; reduced motion or Save-Data get a still version. */
(() => {
  "use strict";
  const audio = name => { const a = window.CWAS && window.CWAS.audio; if (a) a.play(name); };
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const conn = navigator.connection || {};
  const save = conn.saveData || /2g/.test(conn.effectiveType || "");
  const sleep = ms => new Promise(r => setTimeout(r, ms));

  /* ── gallery tunnel ── */
  const el = document.querySelector("[data-gallery-tunnel]");
  if (el) {
    const section = el.closest(".gt-section");
    let images = [], words = [];
    try { images = JSON.parse((save && el.dataset.imagesSmall) || el.dataset.images || "[]"); words = JSON.parse(el.dataset.words || "[]"); } catch (e) { /* no data */ }
    if (reduce) {
      section.classList.add("is-static");
      const fb = section.querySelector(".gt-fallback");
      images.slice(0, 6).forEach(src => { const i = new Image(); i.src = src; i.alt = ""; i.decoding = "async"; fb.appendChild(i); });
      return;
    }
    if (matchMedia("(max-width: 767px), (pointer: coarse)").matches) {
      /* phones and touch tablets: the same tunnel drawn on one canvas, photographs and words flying out of the vanishing
         point along the four walls. One drawing surface instead of eighty large 3D layers, which phone browsers (Safari
         above all) drop or flatten, so it shows everywhere and stays light; a resize only resizes the canvas */
      const cv = document.createElement("canvas"); cv.className = "gt-canvas"; el.appendChild(cv);
      const g = cv.getContext("2d"), pics = images.map(src => { const i = new Image(); i.decoding = "async"; i.src = src; return i; });
      const ROWS = 6, KIND = ["--gt-a", "--gt-b", "--gt-c"];
      let W = 0, H = 0, S = 0, D = 0, LEN = 0, P = 0, tw = 0, th = 0, font = "system-ui, sans-serif", col = {}, tiles = [];
      let off = 0, boost = 0, swx = 0, swy = 0, tx = 0, ty = 0, run = false, raf = 0, last = 0, prevY = scrollY, vel = 0;
      const colours = () => { const cs = getComputedStyle(el); col = { ink: cs.getPropertyValue("--gt-ink").trim() || "#111111" }; KIND.forEach(k => { col[k] = cs.getPropertyValue(k).trim() || "#EEEEEE"; }); font = getComputedStyle(document.body).fontFamily || font; };
      const size = () => {
        const r = el.getBoundingClientRect(), dpr = Math.min(devicePixelRatio || 1, 2), big = Math.max(r.width, r.height);
        W = r.width; H = r.height; cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); g.setTransform(dpr, 0, 0, dpr, 0, 0);
        S = big * 0.34; D = big * 0.5; LEN = ROWS * D; P = big * 1.1; tw = Math.min(W * 0.5, 260); th = tw * 0.72;
      };
      let k0 = 0;
      ["left", "right", "top", "bottom"].forEach((wall, wi) => { for (let r = 0; r < ROWS; r++) for (let c = 0; c < 2; c++) {
        const img = (r + c + wi) % 3 !== 0 && pics.length ? pics[k0 % pics.length] : null;
        tiles.push({ wall, r, c, img, word: img ? "" : (words.length ? words[k0 % words.length] : ""), kind: KIND[k0 % 3] }); k0++;
      } });
      const draw = () => {
        g.clearRect(0, 0, W, H);
        const cx = W / 2 + swx * 18, cy = H / 2 + swy * 14, zmax = P * 0.55, list = [];
        tiles.forEach(t => {
          const z = ((-t.r * D - (t.c ? D / 2 : 0) + off) % LEN + LEN) % LEN - LEN + zmax;
          const a = Math.min(1, Math.max(0, (z + LEN - zmax) / (D * 1.6))) * Math.min(1, Math.max(0, (zmax - z) / (P * 0.3)));
          if (a > 0.01) list.push([z, a, t]);
        });
        list.sort((p, q) => p[0] - q[0]).forEach(([z, a, t]) => {
          const k = P / (P - z), across = (t.c - 0.5) * S, side = t.wall === "left" || t.wall === "right";
          const x0 = side ? (t.wall === "left" ? -S : S) : across, y0 = side ? across : (t.wall === "top" ? -S : S);
          const w = tw * k, h = th * k, x = cx + x0 * k - w / 2, y = cy + y0 * k - h / 2;
          g.globalAlpha = a; g.save(); g.beginPath(); if (g.roundRect) g.roundRect(x, y, w, h, 14 * k); else g.rect(x, y, w, h); g.clip();
          if (t.img && t.img.complete && t.img.naturalWidth) {  // object-fit: cover
            const iw = t.img.naturalWidth, ih = t.img.naturalHeight, ratio = w / h;
            let sw = iw, sh = ih; if (iw / ih > ratio) sw = ih * ratio; else sh = iw / ratio;
            g.drawImage(t.img, (iw - sw) / 2, (ih - sh) / 2, sw, sh, x, y, w, h);
          } else {
            g.fillStyle = col[t.kind]; g.fillRect(x, y, w, h);
            if (t.word) { g.fillStyle = col.ink; g.font = `600 ${Math.max(9, 15 * k)}px ${font}`; g.textAlign = "center"; g.textBaseline = "middle"; g.fillText(t.word, x + w / 2, y + h / 2); }
          }
          g.restore();
        });
        g.globalAlpha = 1;
      };
      const tick = now => {  // it drifts forward; scrolling pushes it, a finger sways it, a tap boosts it
        if (!run) return;
        const dt = Math.min(48, now - (last || now)); last = now;
        const y = scrollY; vel = vel * 0.85 + (y - prevY) * 0.15; prevY = y; boost *= 0.94;
        off += (0.08 + boost + Math.abs(vel) * 0.05) * dt; swx += (tx - swx) * 0.05; swy += (ty - swy) * 0.05;
        draw(); raf = requestAnimationFrame(tick);
      };
      section.addEventListener("pointermove", e => { tx = e.clientX / innerWidth - 0.5; ty = e.clientY / innerHeight - 0.5; }, { passive: true });
      section.addEventListener("click", () => { boost = 1.4; });
      colours(); size();
      new IntersectionObserver(es => es.forEach(en => {
        if (en.isIntersecting) { if (!run) { run = true; last = 0; prevY = scrollY; raf = requestAnimationFrame(tick); } }
        else { run = false; cancelAnimationFrame(raf); }
      }), { rootMargin: "300px 0px" }).observe(section);
      let rt = 0; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(size, 150); }, { passive: true });
      new MutationObserver(colours).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
      return;
    }
    const world = document.createElement("div"); world.className = "gt-world"; el.appendChild(world);
    const L = 5200, ROWS = 10, D = L / ROWS, KIND = ["gt-a", "gt-b", "gt-c"];
    const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
    let tiles = [], S = 0, built = false;
    const build = () => {  // four walls of photographs and word tiles
      world.textContent = ""; tiles = [];
      S = Math.max(innerWidth, innerHeight) * 0.42;
      let k = 0;
      ["left", "right", "top", "bottom"].forEach((wall, wi) => {
        for (let r = 0; r < ROWS; r++) for (let c = 0; c < 2; c++) {
          const t = document.createElement("div"); t.className = "gt-tile";
          if ((r + c + wi) % 3 !== 0 && images.length) {
            const im = document.createElement("img"); im.alt = ""; im.decoding = "async"; im.src = images[k % images.length]; t.appendChild(im);
          } else { t.classList.add(KIND[k % 3]); t.textContent = words.length ? words[k % words.length] : ""; }
          const side = wall === "left" || wall === "right", w = side ? D * 0.84 : S * 0.92, h = side ? S * 0.92 : D * 0.84;
          Object.assign(t.style, { width: w + "px", height: h + "px", marginLeft: -w / 2 + "px", marginTop: -h / 2 + "px" });
          world.appendChild(t);
          tiles.push({ el: t, wall, across: (c - 0.5) * S, z0: -r * D - (c ? D / 2 : 0) });
          k++;
        }
      });
    };
    const place = () => tiles.forEach(t => {
      const z = ((t.z0 + offset) % L + L) % L - L + 700;
      const o = clamp((z + L - 700) / 900, 0, 1) * clamp((620 - z) / 420, 0, 1);
      t.el.style.transform = t.wall === "left" ? `translate3d(${-S}px,${t.across}px,${z}px) rotateY(90deg)`
        : t.wall === "right" ? `translate3d(${S}px,${t.across}px,${z}px) rotateY(-90deg)`
        : t.wall === "top" ? `translate3d(${t.across}px,${-S}px,${z}px) rotateX(-90deg)`
        : `translate3d(${t.across}px,${S}px,${z}px) rotateX(90deg)`;
      t.el.style.opacity = o.toFixed(3);
    });
    let offset = 0, boost = 0, sx = 0, sy = 0, tx = 0, ty = 0, running = false, raf = 0, lastT = 0, lastScroll = scrollY, vel = 0;
    const frame = now => {  // it drifts forward; scrolling pushes it, the pointer sways it, a click boosts it
      if (!running) return;
      const dt = Math.min(48, now - (lastT || now)); lastT = now;
      const y = scrollY; vel = vel * 0.85 + (y - lastScroll) * 0.15; lastScroll = y;
      boost *= 0.94;
      offset += (0.08 + boost + Math.abs(vel) * 0.05) * dt;
      sx += (tx - sx) * 0.05; sy += (ty - sy) * 0.05;
      world.style.transform = `rotateY(${sx * -10}deg) rotateX(${sy * 8}deg)`;
      place();
      raf = requestAnimationFrame(frame);
    };
    section.addEventListener("pointermove", e => { tx = e.clientX / innerWidth - 0.5; ty = e.clientY / innerHeight - 0.5; }, { passive: true });
    section.addEventListener("click", () => { boost = 1.4; });
    new IntersectionObserver(es => es.forEach(en => {
      if (en.isIntersecting) {
        if (!built) { build(); built = true; }
        if (!running) { running = true; lastT = 0; lastScroll = scrollY; raf = requestAnimationFrame(frame); }
      } else { running = false; cancelAnimationFrame(raf); }
    }), { rootMargin: "300px 0px" }).observe(section);
    let rt = 0;
    addEventListener("resize", () => { if (!built) return; clearTimeout(rt); rt = setTimeout(() => { build(); place(); }, 200); }, { passive: true });
  }
})();
