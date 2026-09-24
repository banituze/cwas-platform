/* Homepage hero: live Madagascar water. One photograph of the pond is drawn through a WebGL ripple surface: drops fall
   on the water and the pointer drags ripples across it. Reduced motion, Save-Data or no WebGL keep the plain photograph. */
(() => {
  "use strict";
  const root = document.querySelector("[data-hero-media]");
  if (!root) return;
  const section = root.closest("[data-hero],[data-live-water]") || root.parentElement;  /* the homepage hero or the sign-up panel */
  const canvas = root.querySelector("[data-hero-liquid]");
  const conn = navigator.connection || {};
  const lite = matchMedia("(prefers-reduced-motion: reduce)").matches || conn.saveData || /2g/.test(conn.effectiveType || "");
  let inView = true;

  /* ── live water: a height field of ripples displaces the photograph ── */
  const liquid = (() => {
    if (lite || !canvas) return null;
    let gl = null;
    try { gl = canvas.getContext("webgl", { alpha: false, antialias: false, premultipliedAlpha: false }); } catch (e) { gl = null; }
    if (!gl) return null;
    const VS = "attribute vec2 p;varying vec2 v;void main(){v=p*.5+.5;v.y=1.-v.y;gl_Position=vec4(p,0.,1.);}";
    const FS = "precision mediump float;varying vec2 v;uniform sampler2D img;uniform sampler2D hf;uniform vec2 tx;uniform vec2 cover;uniform float k;" +
      "void main(){float l=texture2D(hf,v-vec2(tx.x,0.)).r;float r=texture2D(hf,v+vec2(tx.x,0.)).r;" +
      "float u=texture2D(hf,v-vec2(0.,tx.y)).r;float d=texture2D(hf,v+vec2(0.,tx.y)).r;vec2 g=vec2(r-l,d-u);" +
      "vec3 c=texture2D(img,clamp((v+g*k-.5)*cover+.5,.001,.999)).rgb;float s=dot(g,vec2(-.72,.69));" +
      "c+=clamp(s*3.,0.,1.)*.26;c-=clamp(-s*3.,0.,1.)*.1;gl_FragColor=vec4(c,1.);}";
    const shader = (type, src) => {
      const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error("shader");
      return s;
    };
    const prog = gl.createProgram();
    try {
      gl.attachShader(prog, shader(gl.VERTEX_SHADER, VS)); gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, FS)); gl.linkProgram(prog);
      if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error("link");
    } catch (e) { return null; }
    gl.useProgram(prog);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const pLoc = gl.getAttribLocation(prog, "p");
    gl.enableVertexAttribArray(pLoc); gl.vertexAttribPointer(pLoc, 2, gl.FLOAT, false, 0, 0);
    const U = name => gl.getUniformLocation(prog, name);
    const uTx = U("tx"), uCover = U("cover");
    const texture = unit => {
      const t = gl.createTexture(); gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, t);
      [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]]
        .forEach(([a, b]) => gl.texParameteri(gl.TEXTURE_2D, a, b));
      return t;
    };
    const imgTex = texture(0), hfTex = texture(1);
    gl.uniform1i(U("img"), 0); gl.uniform1i(U("hf"), 1); gl.uniform1f(U("k"), 0.045);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    let SW = 160, SH = 90, cur = null, prev = null, bytes = null, iw = 16, ih = 9;
    let ready = false, running = false, calm = false, raf = 0, lastDrop = 0, last = null;
    const alloc = () => {
      const a = canvas.width / Math.max(1, canvas.height);
      SW = a >= 1 ? 160 : Math.max(60, Math.round(160 * a));
      SH = a >= 1 ? Math.max(60, Math.round(160 / a)) : 160;
      cur = new Float32Array(SW * SH); prev = new Float32Array(SW * SH); bytes = new Uint8Array(SW * SH).fill(128);
      gl.uniform2f(uTx, 1 / SW, 1 / SH);
    };
    const cover = () => {  // object-fit: cover, like the <img> underneath
      const ca = canvas.width / canvas.height, ia = iw / ih;
      gl.uniform2f(uCover, ca > ia ? 1 : ca / ia, ca > ia ? ia / ca : 1);
    };
    const resize = () => {
      const r = canvas.getBoundingClientRect(), dpr = Math.min(1.5, window.devicePixelRatio || 1);
      canvas.width = Math.max(2, Math.round(r.width * dpr)); canvas.height = Math.max(2, Math.round(r.height * dpr));
      gl.viewport(0, 0, canvas.width, canvas.height); alloc(); cover();
    };
    const draw = () => {
      gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, hfTex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, SW, SH, 0, gl.LUMINANCE, gl.UNSIGNED_BYTE, bytes);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    };
    const disturb = (x, y, rad, amt) => {
      const cx = Math.round(x * (SW - 1)), cy = Math.round(y * (SH - 1));
      for (let dy = -rad; dy <= rad; dy++) for (let dx = -rad; dx <= rad; dx++) {
        const px = cx + dx, py = cy + dy, d = Math.hypot(dx, dy);
        if (px > 0 && py > 0 && px < SW - 1 && py < SH - 1 && d <= rad) cur[py * SW + px] += amt * (1 - d / (rad + 1));
      }
    };
    const step = damp => {  // classic two-buffer water: the new height is the neighbours' mean minus the previous height
      for (let y = 1; y < SH - 1; y++) for (let x = 1; x < SW - 1; x++) {
        const i = y * SW + x;
        prev[i] = ((cur[i - 1] + cur[i + 1] + cur[i - SW] + cur[i + SW]) * 0.5 - prev[i]) * damp;
      }
      const t = cur; cur = prev; prev = t;
      for (let i = 0; i < cur.length; i++) { const b = 128 + cur[i] * 60; bytes[i] = b < 0 ? 0 : b > 255 ? 255 : b; }
    };
    const frame = now => {
      if (!running) return;
      if (!calm && now - lastDrop > 1600 + Math.random() * 2200) { disturb(0.3 + Math.random() * 0.4, 0.64 + Math.random() * 0.26, 2, 1.4); lastDrop = now; }
      step(calm ? 0.9 : 0.985); draw();
      raf = requestAnimationFrame(frame);
    };
    const at = e => { const r = canvas.getBoundingClientRect(); return { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height }; };
    section.addEventListener("pointermove", e => {
      if (!running || calm) return;
      const p = at(e);
      if (p.x < 0 || p.x > 1 || p.y < 0 || p.y > 1) { last = null; return; }
      if (last) {
        const n = Math.min(20, Math.ceil(Math.hypot((p.x - last.x) * SW, (p.y - last.y) * SH) / 1.5));
        for (let s = 1; s <= n; s++) disturb(last.x + (p.x - last.x) * s / n, last.y + (p.y - last.y) * s / n, 2, 0.8);
      }
      last = p;
    }, { passive: true });
    section.addEventListener("pointerleave", () => { last = null; });
    section.addEventListener("pointerdown", e => { if (running && !calm) { const p = at(e); disturb(p.x, p.y, 3, 2.4); } }, { passive: true });
    let rt = 0;
    addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (!ready) return; resize(); draw(); }, 160); }, { passive: true });
    return {
      setImage(img) {
        if (!img.naturalWidth) return;
        iw = img.naturalWidth; ih = img.naturalHeight; resize();
        gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, imgTex);
        try { gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, img); } catch (e) { return; }
        ready = true; draw();
      },
      start() { if (running || !ready) return; running = true; calm = false; canvas.classList.add("is-on"); raf = requestAnimationFrame(frame); },
      stop() { running = false; cancelAnimationFrame(raf); },
      hide() { canvas.classList.remove("is-on"); },
      settle() { calm = true; return new Promise(ok => setTimeout(ok, 380)); },
      flatten() { if (!cur) return; cur.fill(0); prev.fill(0); bytes.fill(128); draw(); }
    };
  })();
  const baseImg = root.querySelector("[data-still]");
  const sync = () => {
    if (!liquid || !baseImg || !baseImg.complete || !baseImg.naturalWidth) return;
    liquid.setImage(baseImg);
    if (inView) liquid.start();
  };
  if (baseImg) { baseImg.addEventListener("load", sync); sync(); }
  if (liquid && "IntersectionObserver" in window) {
    new IntersectionObserver(es => es.forEach(en => { inView = en.isIntersecting; if (inView) liquid.start(); else liquid.stop(); }), { threshold: 0.02 }).observe(section);
  }
  document.addEventListener("visibilitychange", () => { if (!liquid) return; if (document.hidden) liquid.stop(); else if (inView) liquid.start(); });
})();
