// The JARVIS orb in 3D (Three.js, WebGL), after the user's reference image: a particle shell, a tunnel of HUD
// rings facing you, three tilted orbit bands with light running along them and a comet on each,
// lightning crackling along the bands, a voice wave through the middle and sparks, all lit by a bloom
// glow. It runs on the graphics card, so it stays smooth.
//
// The canvas is transparent: the last pass sets each pixel's alpha from its brightness, so the glow
// blends into whatever is behind JARVIS's see-through window.
//
// const orb = await createOrb3D(canvas); orb.draw(t, act, lv, dt) each frame: act 0 (idle) .. 1 (speaking),
// lv = voice level 0..1. orb.setTint("blue" | "red" | "amber"). Returns null if WebGL isn't available
// (the caller then uses the 2D orb, orb.js).
import * as THREE from "./vendor/three/three.module.js";
import { EffectComposer } from "./vendor/three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "./vendor/three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "./vendor/three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "./vendor/three/addons/postprocessing/OutputPass.js";
import { ShaderPass } from "./vendor/three/addons/postprocessing/ShaderPass.js";

const TAU = Math.PI * 2;
const PALETTES = {
  blue: { cyan: 0x5acdff, ice: 0xbef0ff, deep: 0x195fff },
  red: { cyan: 0xff5f69, ice: 0xffcdd2, deep: 0xbe192d },
  amber: { cyan: 0xffb946, ice: 0xffebbe, deep: 0xd26e14 },
};
const BANDS = [   // radius (sphere = 1), tilt about x, roll about z, wobble speed, flow speed
  { r: 1.18, tilt: 1.05, roll: -0.42, wob: 0.21, flow: 0.9 },
  { r: 1.08, tilt: 1.22, roll: 0.55, wob: -0.17, flow: -0.7 },
  { r: 1.26, tilt: 0.92, roll: 0.08, wob: 0.13, flow: 0.55 },
];
const additive = { transparent: true, depthWrite: false, depthTest: false, blending: THREE.AdditiveBlending };

// ---------------------------------------------------------------- shaders

const SHELL_VS = `
attribute float aSeed;
uniform float uTime, uSize, uIntensity;
varying float vB;
void main() {
  vec4 mv = modelViewMatrix * vec4(position, 1.0);
  vec3 n = normalize(mat3(modelViewMatrix) * position);
  float edge = 1.0 - abs(n.z);                       // brighter at the rim, like the reference
  float twinkle = 0.6 + 0.4 * sin(uTime * 2.0 + aSeed * 20.0);
  vB = (0.25 + 0.75 * edge) * twinkle * uIntensity;
  gl_PointSize = uSize * (6.0 / -mv.z);
  gl_Position = projectionMatrix * mv;
}`;
const SHELL_FS = `
uniform vec3 uColor;
varying float vB;
void main() {
  float a = smoothstep(0.5, 0.0, length(gl_PointCoord - 0.5));
  gl_FragColor = vec4(uColor * vB * a, 1.0);
}`;

const BAND_VS = `
varying vec2 vUv;
varying float vDepth;
void main() {
  vUv = uv;
  vDepth = (modelMatrix * vec4(position, 1.0)).z;      // > 0: the near half
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}`;
const BAND_FS = `
uniform vec3 uColor, uIce;
uniform float uTime, uFlow, uIntensity, uAlpha, uComet;
varying vec2 vUv;
varying float vDepth;
void main() {
  float th = vUv.x * 6.2831853;
  float flow = 0.6 + 0.4 * sin(th * 3.0 - uTime * uFlow);          // light running along the band
  float near = vDepth >= 0.0 ? 1.0 : 0.35;                          // the far half is dimmer
  float behind = fract((uComet - vUv.x) * sign(uFlow + 1e-4));      // distance behind the comet's head
  float comet = exp(-behind * 30.0) * step(0.0, vDepth + 0.2);
  vec3 col = uColor * flow * uIntensity * uAlpha * near + uIce * comet * (uIntensity + 0.3) * uAlpha;
  gl_FragColor = vec4(col, 1.0);
}`;

// last pass: alpha from brightness, so the transparent canvas is valid premultiplied light
const ALPHA_SHADER = {
  uniforms: { tDiffuse: { value: null } },
  vertexShader: "varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }",
  fragmentShader: `uniform sampler2D tDiffuse; varying vec2 vUv;
    void main() {
      vec4 c = texture2D(tDiffuse, vUv);
      c.rgb *= smoothstep(1.0, 0.8, length(vUv - 0.5) * 2.0);        // fade out in a circle: no square edge
      c.rgb = max(c.rgb - 0.05, 0.0) * 1.45;                        // drop the faintest haze; brighter on the desktop
      float a = clamp(max(max(c.r, c.g), c.b) * 1.25, 0.0, 1.0);    // and more solid where it shines
      gl_FragColor = vec4(min(c.rgb, vec3(a)), a);
    }`,
};

// ---------------------------------------------------------------- geometry helpers

function arcs(list) {        // flat ring pieces in the xy plane: [r0, r1, a0, a1]
  const pos = [], idx = [];
  for (const [r0, r1, a0, a1] of list) {
    const segs = Math.max(2, Math.ceil(((a1 - a0) / TAU) * 96)), base = pos.length / 3;
    for (let i = 0; i <= segs; i++) {
      const a = a0 + ((a1 - a0) * i) / segs, c = Math.cos(a), s = Math.sin(a);
      pos.push(c * r0, s * r0, 0, c * r1, s * r1, 0);
      if (i < segs) { const v = base + i * 2; idx.push(v, v + 1, v + 2, v + 1, v + 3, v + 2); }
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  g.setIndex(idx);
  return g;
}

function glowTexture(stops) {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const x = c.getContext("2d"), g = x.createRadialGradient(64, 64, 0, 64, 64, 64);
  for (const [at, colour] of stops) g.addColorStop(at, colour);
  x.fillStyle = g; x.fillRect(0, 0, 128, 128);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

// ---------------------------------------------------------------- the orb

export async function createOrb3D(canvas) {
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, premultipliedAlpha: true });
  } catch (err) {
    console.warn("WebGL isn't available; using the 2D orb", err);
    return null;
  }
  renderer.setClearColor(0x000000, 0);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 50);
  camera.position.set(0, 0, 5.87);                 // the sphere (radius 1) fills 27% of the canvas width

  const pal = { cyan: new THREE.Color(), ice: new THREE.Color(), deep: new THREE.Color() };
  const target = { cyan: new THREE.Color(), ice: new THREE.Color(), deep: new THREE.Color() };
  const setPalette = (into, name) => { const p = PALETTES[name] || PALETTES.blue; for (const k in p) into[k].setHex(p[k]); };
  setPalette(pal, "blue"); setPalette(target, "blue");

  // halo and core
  const halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTexture([[0, "rgba(40,110,255,0.9)"], [0.5, "rgba(25,80,255,0.25)"], [1, "rgba(0,0,0,0)"]]), ...additive }));
  halo.scale.setScalar(2.7);
  const core = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTexture([[0, "rgba(255,255,255,1)"], [0.25, "rgba(160,230,255,0.85)"], [0.6, "rgba(40,140,255,0.25)"], [1, "rgba(0,0,0,0)"]]), ...additive }));
  halo.visible = false;                            // the bloom is the glow; a halo sprite fogged the desktop
  scene.add(halo, core);

  // particle shell
  const N = 2800, shellPos = new Float32Array(N * 3), seeds = new Float32Array(N);
  for (let i = 0; i < N; i++) {
    const y = 1 - (i / (N - 1)) * 2, rad = Math.sqrt(1 - y * y), th = i * 2.399963, k = 1.02 * (1 + (Math.random() - 0.5) * 0.06);
    shellPos.set([Math.cos(th) * rad * k, y * k, Math.sin(th) * rad * k], i * 3);
    seeds[i] = Math.random();
  }
  const shellGeo = new THREE.BufferGeometry();
  shellGeo.setAttribute("position", new THREE.BufferAttribute(shellPos, 3));
  shellGeo.setAttribute("aSeed", new THREE.BufferAttribute(seeds, 1));
  const shellMat = new THREE.ShaderMaterial({ vertexShader: SHELL_VS, fragmentShader: SHELL_FS, ...additive,
    uniforms: { uTime: { value: 0 }, uSize: { value: 2 }, uIntensity: { value: 1 }, uColor: { value: pal.cyan } } });
  const shell = new THREE.Points(shellGeo, shellMat);
  shell.rotation.x = 0.3;
  scene.add(shell);

  // tunnel of HUD rings, facing the camera
  const rings = [];
  for (let k = 0; k < 16; k++) {
    const f = k / 15, r = 0.07 + 0.83 * Math.pow(f, 1.15), list = [];
    if (k % 3 === 0) list.push([r - 0.004, r + 0.004, 0, TAU]);
    else if (k % 3 === 1) {
      const n = 18 + k * 3, w = 0.007 + 0.009 * (1 - f);
      for (let i = 0; i < n; i++) if ((i * 7 + k) % 5 !== 0) { const a = (i / n) * TAU; list.push([r - w, r + w, a, a + (TAU / n) * 0.62]); }
    } else {
      const n = 60 + k * 6;
      for (let i = 0; i < n; i++) { const a = (i / n) * TAU, l = i % 6 === 0 ? 0.03 : 0.015; list.push([r, r + l, a, a + 0.0035 / r]); }
    }
    const mesh = new THREE.Mesh(arcs(list), new THREE.MeshBasicMaterial({ color: f < 0.5 ? pal.ice : pal.cyan, ...additive, side: THREE.DoubleSide }));
    mesh.userData = { f, dir: k % 2 ? 1 : -1, k };
    rings.push(mesh);
    scene.add(mesh);
  }

  // orbit bands: a few thin tori each, with flowing light and a comet
  const bandMats = [];
  const bands = BANDS.map((b, bi) => {
    const group = new THREE.Group();
    group.rotation.order = "ZYX";
    for (const [off, tube, alpha] of [[-0.035, 0.005, 0.5], [0, 0.011, 1.0], [0.035, 0.005, 0.45], [0, 0.035, 0.12]]) {
      const mat = new THREE.ShaderMaterial({ vertexShader: BAND_VS, fragmentShader: BAND_FS, ...additive,
        uniforms: { uColor: { value: pal.cyan }, uIce: { value: pal.ice }, uTime: { value: 0 }, uFlow: { value: b.flow },
          uIntensity: { value: 1 }, uAlpha: { value: alpha }, uComet: { value: bi * 0.3 } } });
      bandMats.push(mat);
      group.add(new THREE.Mesh(new THREE.TorusGeometry(b.r + off, tube, 6, 220), mat));
    }
    scene.add(group);
    return { ...b, group };
  });

  // lightning along the bands
  const bolts = Array.from({ length: 6 }, () => {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(17 * 3), 3));
    const line = new THREE.Line(geo, new THREE.LineBasicMaterial({ color: pal.ice.clone(), ...additive, opacity: 0 }));
    line.visible = false;
    scene.add(line);
    return { line, born: 0, life: 0, band: null, th0: 0, len: 0, seed: 0 };
  });

  // the voice wave: three horizontal traces, fading towards the ends
  const W = 220, waves = [[1, 1], [-0.6, 0.55], [0.35, 0.45]].map(([k, alpha], i) => {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(W * 3), 3));
    geo.setAttribute("color", new THREE.BufferAttribute(new Float32Array(W * 3), 3));
    const line = new THREE.Line(geo, new THREE.LineBasicMaterial({ vertexColors: true, ...additive }));
    line.position.z = 0.6;
    scene.add(line);
    return { line, k, alpha, phase: i * 1.6 };
  });

  // sparks
  const S = 140, sparkPos = new Float32Array(S * 3), spark = [];
  const sparkGeo = new THREE.BufferGeometry();
  sparkGeo.setAttribute("position", new THREE.BufferAttribute(sparkPos, 3));
  const sparkMat = new THREE.PointsMaterial({ color: pal.ice, size: 3, sizeAttenuation: false, map: glowTexture([[0, "#fff"], [1, "rgba(0,0,0,0)"]]), ...additive });
  scene.add(new THREE.Points(sparkGeo, sparkMat));
  for (let i = 0; i < S; i++) spark.push({ x: 0, y: 0, z: 99, vx: 0, vy: 0, born: -9, life: 1 });

  // bloom, then colour output, then alpha from brightness
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), 0.6, 0.0, 0.3);
  // only the tighter blur layers: the widest ones spread faint light over the whole canvas (a foggy disc)
  bloom.compositeMaterial.uniforms.bloomFactors.value = [1.0, 0.7, 0.35, 0.0, 0.0];
  composer.addPass(bloom);
  composer.addPass(new OutputPass());
  composer.addPass(new ShaderPass(ALPHA_SHADER));

  let pixels = 0;
  function fit() {
    const css = canvas.clientWidth || canvas.width, ratio = window.devicePixelRatio || 1, px = Math.round(css * ratio);
    if (px === pixels) return;
    pixels = px;
    renderer.setPixelRatio(ratio);
    renderer.setSize(css, css, false);
    composer.setPixelRatio(ratio);
    composer.setSize(css, css);
    shellMat.uniforms.uSize.value = Math.max(1.5, 2.6 * (px / 900) * 2.2);
    sparkMat.size = Math.max(1.5, 3 * (px / 900) * 2);
  }

  const v = new THREE.Vector3();
  function bandPoint(b, th, out) {           // a point on a band, in world space
    v.set(Math.cos(th) * b.r, Math.sin(th) * b.r, 0);
    return out.copy(v).applyMatrix4(b.group.matrixWorld);
  }
  const tmp = new THREE.Vector3();

  return {
    setTint(name) { setPalette(target, name); },
    draw(t, act, lv, dt) {
      dt = Math.min(Math.max(dt || 1 / 60, 0), 0.1);
      fit();
      const ease = Math.min(1, dt * 4);
      for (const k of ["cyan", "ice", "deep"]) pal[k].lerp(target[k], ease);
      const I = 0.45 + 0.4 * act + 0.45 * lv * act;   // low when idle, builds with activity

      halo.material.opacity = 0.22 * I; halo.material.color.copy(pal.deep);
      const cr = 0.075 + 0.025 * lv * act + 0.01 * act;
      core.scale.setScalar(cr * 6); core.material.opacity = Math.min(0.9, 0.55 * I + 0.15); core.material.color.copy(pal.ice);

      shell.rotation.y = t * (0.06 + 0.12 * act);
      shellMat.uniforms.uTime.value = t; shellMat.uniforms.uIntensity.value = 0.6 * I;

      for (const ring of rings) {
        const { f, dir, k } = ring.userData;
        ring.rotation.z = t * dir * (0.15 + 0.5 * act) * (1.4 - f);
        ring.scale.setScalar(1 + 0.015 * Math.sin(t * 1.3 + k));
        ring.material.opacity = Math.min(0.85, I * (0.3 + 0.55 * (1 - f))) * (0.85 + 0.3 * lv * act * Math.sin(t * 9 + k * 1.7));
      }

      for (const b of bands) {
        b.group.rotation.x = b.tilt + Math.sin(t * b.wob) * 0.12;
        b.group.rotation.z = b.roll + Math.cos(t * b.wob * 0.8) * 0.1;
        b.group.updateMatrixWorld();
      }
      bands.forEach((b, bi) => {
        const comet = (t * b.flow * (0.8 + 1.6 * act)) / TAU + bi * 0.3;
        for (const mesh of b.group.children) {
          const u = mesh.material.uniforms;
          u.uTime.value = t * (2 + 4 * act); u.uIntensity.value = I; u.uComet.value = ((comet % 1) + 1) % 1;
        }
      });

      // lightning: more, and longer, while someone speaks
      const rate = 0.6 + 7 * act * (0.2 + lv);
      if (Math.random() < rate * dt) {
        const free = bolts.find((b) => !b.line.visible);
        if (free) Object.assign(free, { band: bands[Math.floor(Math.random() * 3)], th0: Math.random() * TAU,
          len: 0.35 + Math.random() * (0.4 + 0.8 * lv * act), born: t, life: 0.1 + Math.random() * 0.15, seed: Math.random() * 1000 });
        if (free) free.line.visible = true;
      }
      for (const bo of bolts) {
        if (!bo.line.visible) continue;
        const age = (t - bo.born) / bo.life;
        if (age > 1 || age < 0) { bo.line.visible = false; continue; }
        const arr = bo.line.geometry.attributes.position.array;
        for (let q = 0; q <= 16; q++) {
          bandPoint(bo.band, bo.th0 + (q / 16) * bo.len, tmp);
          const j = q === 0 || q === 16 ? 0 : Math.sin(bo.seed + q * 12.9898 + Math.floor(t * 30)) * 0.045;
          const l = Math.hypot(tmp.x, tmp.y) || 1;
          arr.set([tmp.x + (tmp.x / l) * j, tmp.y + (tmp.y / l) * j, tmp.z], q * 3);
        }
        bo.line.geometry.attributes.position.needsUpdate = true;
        bo.line.material.color.copy(pal.ice);
        bo.line.material.opacity = (1 - age) * Math.min(1, I + 0.2);
      }

      // the voice wave
      const half = 2.25, amp = 0.015 + 0.05 * act + 0.32 * lv * act;
      for (const w of waves) {
        const p = w.line.geometry.attributes.position.array, c = w.line.geometry.attributes.color.array;
        for (let i = 0; i < W; i++) {
          const x = -half + (2 * half * i) / (W - 1), u = x / half, env = Math.exp(-u * u * 3.2), px = x * 243;
          const s = Math.sin(px * 0.045 + t * 9 + w.phase) * 0.55 + Math.sin(px * 0.11 - t * 6 + w.phase) * 0.3 + Math.sin(px * 0.023 + t * 3) * 0.25;
          p[i * 3] = x; p[i * 3 + 1] = s * amp * env * w.k; p[i * 3 + 2] = 0;
          const b = Math.min(1, (0.15 + env) * I * w.alpha);
          c[i * 3] = pal.ice.r * b; c[i * 3 + 1] = pal.ice.g * b; c[i * 3 + 2] = pal.ice.b * b;
        }
        w.line.geometry.attributes.position.needsUpdate = true;
        w.line.geometry.attributes.color.needsUpdate = true;
      }

      // sparks drifting outward
      if (Math.random() < (18 + 150 * act * (0.3 + lv)) * dt) {
        const s = spark.find((p) => t - p.born > p.life);
        if (s) {
          const a = Math.random() * TAU, r = 1 + Math.random() * 0.3, sp = 0.08 + Math.random() * 0.3;
          Object.assign(s, { x: Math.cos(a) * r, y: Math.sin(a) * r, z: 0.3, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp, born: t, life: 0.8 + Math.random() * 1.4 });
        }
      }
      spark.forEach((p, i) => {
        const alive = t - p.born <= p.life;
        if (alive) { p.x += p.vx * dt; p.y += p.vy * dt; }
        sparkPos.set(alive ? [p.x, p.y, p.z] : [0, 0, 99], i * 3);
      });
      sparkGeo.attributes.position.needsUpdate = true;
      sparkMat.opacity = Math.min(1, 0.8 * I);

      bloom.strength = 0.45 + 0.35 * I;
      composer.render(dt);
    },
  };
}
