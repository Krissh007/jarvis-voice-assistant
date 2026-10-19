// The JARVIS orb, drawn after the user's reference image: a particle shell, a tunnel of HUD rings in the
// centre, three tilted orbit bands with lightning crackling along them, a horizontal voice waveform
// through the middle, and sparks, with a bloom glow. One 2D canvas, drawn additively on a transparent
// background so the glow blends into the desktop behind the window.
//
// const orb = createOrb(canvas); orb.draw(t, act, lv, dt) every frame, where t is seconds, act runs from
// 0 (idle) to 1 (speaking), lv is the voice level 0..1 and dt the seconds since the last frame.
// orb.setTint("blue" | "red" | "amber") changes colour smoothly (muted, working on a task).
"use strict";

const ORB_PALETTES = {
  blue: { cyan: [90, 205, 255], ice: [190, 240, 255], deep: [25, 95, 255] },
  red: { cyan: [255, 95, 105], ice: [255, 205, 210], deep: [190, 25, 45] },
  amber: { cyan: [255, 185, 70], ice: [255, 235, 190], deep: [210, 110, 20] },
  muted: { cyan: [138, 155, 176], ice: [210, 220, 232], deep: [60, 76, 98] },
  error: { cyan: [255, 77, 106], ice: [255, 198, 208], deep: [168, 16, 44] },
};

function createOrb(canvas) {
  const out = canvas.getContext("2d");
  const off = document.createElement("canvas");
  off.width = canvas.width; off.height = canvas.height;
  const x = off.getContext("2d");                 // the scene; out gets it with a bloom glow
  const S = canvas.width, m = S / 2, R = S * 0.27;
  const k = Math.max(0.3, S / 900);               // line widths and glows were tuned at 900 px; scale them
  const TAU = Math.PI * 2;

  const shell = [];                               // points on a sphere, a little scattered like dust
  for (let i = 0, N = Math.round(600 + 1800 * Math.min(1, S / 700)); i < N; i++) {
    const y = 1 - (i / (N - 1)) * 2, rad = Math.sqrt(1 - y * y), th = i * 2.399963;
    const k = 1 + (Math.random() - 0.5) * 0.06;
    shell.push([Math.cos(th) * rad * k, y * k, Math.sin(th) * rad * k, Math.random()]);
  }
  const bands = [                                 // radius, tilt (about x), roll (about z), wobble, flow
    { r: 1.18, tilt: 1.05, roll: -0.42, wob: 0.21, flow: 0.9 },
    { r: 1.08, tilt: 1.22, roll: 0.55, wob: -0.17, flow: -0.7 },
    { r: 1.26, tilt: 0.92, roll: 0.08, wob: 0.13, flow: 0.55 },
  ];
  const bolts = [], sparks = [];

  // colours ease towards the target palette
  const pal = JSON.parse(JSON.stringify(ORB_PALETTES.blue));
  let target = ORB_PALETTES.blue;
  const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${Math.max(0, Math.min(1, a)).toFixed(3)})`;
  const cyan = (a) => rgba(pal.cyan, a), ice = (a) => rgba(pal.ice, a), deep = (a) => rgba(pal.deep, a);
  function easeColours(dt) {
    const k = Math.min(1, dt * 4);
    for (const name of ["cyan", "ice", "deep"]) for (let i = 0; i < 3; i++) pal[name][i] += (target[name][i] - pal[name][i]) * k;
  }

  function bandPoint(b, th, t, rOff) {
    const tilt = b.tilt + Math.sin(t * b.wob) * 0.12, roll = b.roll + Math.cos(t * b.wob * 0.8) * 0.1;
    const rr = R * (b.r + rOff);
    let X = Math.cos(th) * rr, Y = Math.sin(th) * rr, Z = 0;
    [Y, Z] = [Y * Math.cos(tilt), Y * Math.sin(tilt)];
    [X, Y] = [X * Math.cos(roll) - Y * Math.sin(roll), X * Math.sin(roll) + Y * Math.cos(roll)];
    const f = 1 / (1 - Z / (R * 6));
    return [m + X * f, m + Y * f, Z / rr];
  }

  function drawShell(t, act, I) {
    const ry = t * (0.06 + 0.12 * act), rx = 0.3;
    const cy = Math.cos(ry), sy = Math.sin(ry), cx = Math.cos(rx), sx = Math.sin(rx);
    const buckets = [[], [], [], []];
    for (const [a, b, c, tw] of shell) {
      let X = a * cy - c * sy, Z = a * sy + c * cy, Y = b;
      [Y, Z] = [Y * cx - Z * sx, Y * sx + Z * cx];
      const edge = 1 - Math.abs(Z);
      const lvl = Math.min(3, Math.floor((0.25 + 0.75 * edge) * (0.6 + 0.4 * Math.sin(t * 2 + tw * 20)) * 4));
      buckets[lvl].push(m + X * R * 1.02, m + Y * R * 1.02);
    }
    buckets.forEach((pts, i) => {
      x.fillStyle = cyan((0.18 + i * 0.26) * I);
      const d = Math.max(1, 2.4 * k);
      for (let j = 0; j < pts.length; j += 2) x.fillRect(pts[j] - d / 2, pts[j + 1] - d / 2, d, d);
    });
  }

  function drawTunnel(t, act, lv, I) {
    const rings = 16;
    for (let ring = 0; ring < rings; ring++) {
      const f = ring / (rings - 1);
      const r = R * (0.07 + 0.83 * Math.pow(f, 1.15)) * (1 + 0.015 * Math.sin(t * 1.3 + ring));
      const b = Math.min(0.85, I * (0.3 + 0.55 * (1 - f))) * (0.85 + 0.3 * lv * act * Math.sin(t * 9 + ring * 1.7));
      const dir = ring % 2 ? 1 : -1, spin = t * dir * (0.15 + 0.5 * act) * (1.4 - f);
      x.strokeStyle = (f < 0.5 ? ice : cyan)(Math.min(1, b * 1.25));
      if (ring % 3 === 0) {
        x.lineWidth = 2 * k; x.beginPath(); x.arc(m, m, r, 0, TAU); x.stroke();
      } else if (ring % 3 === 1) {
        const n = 18 + ring * 3; x.lineWidth = (3.5 + 4 * (1 - f)) * k;
        for (let i = 0; i < n; i++) {
          if ((i * 7 + ring) % 5 === 0) continue;
          const a = spin + (i / n) * TAU; x.beginPath(); x.arc(m, m, r, a, a + (TAU / n) * 0.62); x.stroke();
        }
      } else {
        const n = 60 + ring * 6; x.lineWidth = Math.max(0.6, 1.6 * k);
        for (let i = 0; i < n; i++) {
          const a = spin + (i / n) * TAU, l = (i % 6 === 0 ? 7 : 3.5) * k;
          x.beginPath(); x.moveTo(m + Math.cos(a) * r, m + Math.sin(a) * r);
          x.lineTo(m + Math.cos(a) * (r + l), m + Math.sin(a) * (r + l)); x.stroke();
        }
      }
    }
    const cr = R * (0.075 + 0.025 * lv * act + 0.01 * act);
    const g = x.createRadialGradient(m, m, 0, m, m, cr * 3);
    g.addColorStop(0, ice(Math.min(0.85, 0.55 * I + 0.15))); g.addColorStop(0.25, cyan(Math.min(0.6, 0.45 * I)));
    g.addColorStop(1, deep(0));
    x.fillStyle = g; x.beginPath(); x.arc(m, m, cr * 3, 0, TAU); x.fill();
  }

  // The voice: a horizontal line through the middle that ripples into a wave as anyone speaks
  // (no vertical bars; the user asked for those to go).
  function drawWave(t, act, lv, I) {
    const half = R * 1.95 * 1.15, step = Math.max(1, 2 * k);
    const amp = R * (0.015 + 0.05 * act + 0.32 * lv * act);
    const lg = x.createLinearGradient(m - half, 0, m + half, 0);
    lg.addColorStop(0, ice(0)); lg.addColorStop(0.5, ice(Math.min(1, 0.9 * I))); lg.addColorStop(1, ice(0));
    const traces = [[1, 2.6, 0], [-0.6, 1.4, 1.7], [0.35, 1, 3.1]];   // a main trace and two fainter echoes
    traces.forEach(([k, w, phase], i) => {
      x.strokeStyle = i ? cyan(Math.min(1, 0.55 * I)) : lg; x.lineWidth = Math.max(0.6, w * k);
      x.beginPath();
      for (let px = -half; px <= half; px += step) {
        const u = px / half, env = Math.exp(-u * u * 3.2);
        const s = Math.sin(px * 0.045 + t * 9 + phase) * 0.55 + Math.sin(px * 0.11 - t * 6 + phase) * 0.3
          + Math.sin(px * 0.023 + t * 3) * 0.25;
        const y = m + s * amp * env * k;
        px === -half ? x.moveTo(m + px, y) : x.lineTo(m + px, y);
      }
      x.stroke();
    });
  }

  function drawBands(t, act, I, back) {
    const segs = 140;
    bands.forEach((b, bi) => {
      for (const [o, w, al] of [[-0.05, 1.6, 0.5], [-0.022, 3.6, 1], [0, 6, 0.55], [0.022, 2.8, 0.9], [0.05, 1.6, 0.45]]) {
        x.lineWidth = Math.max(0.5, w * k);
        let prev = bandPoint(b, 0, t, o);
        for (let s = 1; s <= segs; s++) {
          const th = (s / segs) * TAU, p = bandPoint(b, th, t, o);
          if (((p[2] + prev[2]) / 2 < 0) === back) {
            const flow = 0.6 + 0.4 * Math.sin(th * 3 - t * b.flow * (2 + 4 * act) + bi);
            x.strokeStyle = cyan(Math.min(1, al * I * 1.2 * flow * (back ? 0.35 : 1)));
            x.beginPath(); x.moveTo(prev[0], prev[1]); x.lineTo(p[0], p[1]); x.stroke();
          }
          prev = p;
        }
      }
      if (!back) {                                  // a bright comet travelling round each band
        const c0 = t * b.flow * (0.8 + 1.6 * act) + bi * 2;
        x.lineCap = "round"; x.shadowColor = cyan(1); x.shadowBlur = 14 * k;
        for (let j = 0; j < 14; j++) {
          const p1 = bandPoint(b, c0 - j * 0.03, t, 0), p2 = bandPoint(b, c0 - (j + 1) * 0.03, t, 0);
          if (p1[2] < -0.2) continue;
          x.strokeStyle = ice(Math.min(1, (1 - j / 14) * (I + 0.3))); x.lineWidth = (6 * (1 - j / 14) + 1) * k;
          x.beginPath(); x.moveTo(p1[0], p1[1]); x.lineTo(p2[0], p2[1]); x.stroke();
        }
        x.shadowBlur = 0;
      }
    });
  }

  function lightning(t, act, lv, I, dt) {
    const rate = 0.6 + 7 * act * (0.2 + lv);        // bolts per second
    if (Math.random() < rate * dt) {
      const b = bands[Math.floor(Math.random() * bands.length)];
      bolts.push({ b, th0: Math.random() * TAU, len: 0.35 + Math.random() * (0.4 + 0.8 * lv * act), n: 16, born: t,
        life: 0.1 + Math.random() * 0.15, seed: Math.random() * 1000 });
    }
    x.lineJoin = "round"; x.lineCap = "round";
    for (let i = bolts.length - 1; i >= 0; i--) {
      const bo = bolts[i], age = (t - bo.born) / bo.life;
      if (age > 1 || age < 0) { bolts.splice(i, 1); continue; }
      const pts = [];
      for (let q = 0; q <= bo.n; q++) {
        const p = bandPoint(bo.b, bo.th0 + (q / bo.n) * bo.len, t, 0);
        const j = (q === 0 || q === bo.n) ? 0 : Math.sin(bo.seed + q * 12.9898 + Math.floor(t * 30)) * 0.5 * R * 0.09;
        const dx = p[0] - m, dy = p[1] - m, dl = Math.hypot(dx, dy) || 1;
        pts.push([p[0] + (dx / dl) * j, p[1] + (dy / dl) * j]);
      }
      const al = (1 - age) * Math.min(1, I + 0.2);
      for (const [w, col, blur] of [[5, deep(0.5 * al), 16], [2, cyan(0.9 * al), 8], [0.9, ice(al), 0]]) {
        x.strokeStyle = col; x.lineWidth = Math.max(0.5, w * k); x.shadowColor = deep(1); x.shadowBlur = blur * k;
        x.beginPath(); pts.forEach(([px, py], q) => (q ? x.lineTo(px, py) : x.moveTo(px, py))); x.stroke();
      }
    }
    x.shadowBlur = 0;
  }

  function drawSparks(t, act, lv, I, dt) {
    if (Math.random() < (18 + 150 * act * (0.3 + lv)) * dt) {
      const a = Math.random() * TAU, r = R * (1 + Math.random() * 0.3);
      sparks.push({ x: m + Math.cos(a) * r, y: m + Math.sin(a) * r, vx: Math.cos(a) * (18 + Math.random() * 60) * k,
        vy: Math.sin(a) * (18 + Math.random() * 60) * k, born: t, life: 0.8 + Math.random() * 1.4,
        s: Math.max(1, (1 + Math.random() * 2.2) * k) });
    }
    for (let i = sparks.length - 1; i >= 0; i--) {
      const p = sparks[i], age = (t - p.born) / p.life;
      if (age > 1 || age < 0) { sparks.splice(i, 1); continue; }
      p.x += p.vx * dt; p.y += p.vy * dt;
      x.fillStyle = ice((1 - age) * 0.8 * I + 0.05); x.fillRect(p.x, p.y, p.s, p.s);
    }
  }

  return {
    setTint(name) { target = ORB_PALETTES[name] || ORB_PALETTES.blue; },
    draw(t, act, lv, dt) {
      dt = Math.min(Math.max(dt || 1 / 60, 0), 0.1);
      easeColours(dt);
      const I = 0.45 + 0.4 * act + 0.45 * lv * act;   // low when idle, builds with activity
      x.clearRect(0, 0, S, S);
      x.globalCompositeOperation = "lighter";
      const bg = x.createRadialGradient(m, m, 0, m, m, R * 1.7);
      bg.addColorStop(0, deep(0.32 * I)); bg.addColorStop(0.6, deep(0.12 * I)); bg.addColorStop(1, deep(0));
      x.fillStyle = bg; x.beginPath(); x.arc(m, m, R * 1.7, 0, TAU); x.fill();
      drawBands(t, act, I, true);
      drawShell(t, act, I);
      drawTunnel(t, act, lv, I);
      drawWave(t, act, lv, I);
      drawBands(t, act, I, false);
      lightning(t, act, lv, I, dt);
      drawSparks(t, act, lv, I, dt);
      x.globalCompositeOperation = "source-over";
      out.clearRect(0, 0, S, S);
      out.globalCompositeOperation = "lighter";
      out.filter = `blur(${(S * 0.022).toFixed(1)}px)`; out.globalAlpha = 0.7; out.drawImage(off, 0, 0);
      out.filter = `blur(${(S * 0.006).toFixed(1)}px)`; out.globalAlpha = 0.8; out.drawImage(off, 0, 0);
      out.filter = "none"; out.globalAlpha = 1; out.drawImage(off, 0, 0);
      out.globalCompositeOperation = "source-over";
    },
  };
}
