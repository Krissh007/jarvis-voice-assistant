// The JARVIS orb in 3D (Three.js, WebGL): "Cloud and core", which Krishna designed from the mock-ups. Outside, a
// soft cloud of light points shaped like a sphere that slowly breathes, with no lines or rings; when JARVIS
// speaks, the cloud swirls and bulges with the voice. Inside, an arc reactor like Iron Man's (Krishna picked it
// from ten inner shapes): a bright nucleus, an inner ring, a ring of ten segments that light up in a chase
// while JARVIS is busy, and an outer ring with ticks, gently tilting. The cloud is 1.2 times the reactor's
// size (Krishna: inner 100%, outer 120%). Around the cloud, hugging it, three glowing Möbius ribbons (from the
// orbit bands of the first orb, each with a half twist) carry light and a comet; lightning runs along them
// while JARVIS speaks. The reactor, the cloud and the ribbons each have their own brightness. A bloom
// pass adds the glow. It runs on the graphics card, so it stays smooth.
//
// The canvas is transparent: the last pass sets each pixel's alpha from its brightness, so the glow
// blends into whatever is behind JARVIS's see-through window.
//
// const orb = await createOrb3D(canvas); orb.draw(t, act, lv, dt) each frame: act 0 (idle) .. 1 (speaking),
// lv = voice level 0..1. orb.setTint("blue" | "red" | "amber"). orb.setBrightness(core, cloud, ribbons),
// each 0.2..1. orb.setCoreSize(share): the reactor's size as a share of the cloud's, 0.4..1 (default 1/1.2).
// Returns null if WebGL isn't available (the caller then uses the 2D orb, orb.js).
import * as THREE from "./vendor/three/three.module.js";
import { EffectComposer } from "./vendor/three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "./vendor/three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "./vendor/three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "./vendor/three/addons/postprocessing/OutputPass.js";
import { ShaderPass } from "./vendor/three/addons/postprocessing/ShaderPass.js";

const PALETTES = {
  blue: { cyan: 0x5acdff, ice: 0xbef0ff, deep: 0x195fff },
  red: { cyan: 0xff5f69, ice: 0xffcdd2, deep: 0xbe192d },
  amber: { cyan: 0xffb946, ice: 0xffebbe, deep: 0xd26e14 },
  muted: { cyan: 0x8a9bb0, ice: 0xd2dce8, deep: 0x3c4c62 },   // microphone off
  thinking: { cyan: 0xaa8cff, ice: 0xe4daff, deep: 0x603cdc },   // a reply or a tool is on its way
  error: { cyan: 0xff4d6a, ice: 0xffc6d0, deep: 0xa8102c },   // something JARVIS needs isn't working
};
const CLOUD_POINTS = 5000;
const CLOUD_RADIUS = 0.95;
const CORE_SHARE = 1 / 1.2;   // the core's size as a share of the cloud's (Krishna: inner 100%, outer 120%)
const NUCLEUS = 1.0;            // the nucleus's glow per unit of the reactor's radius
const SEGMENTS = 10;
const RIBBONS = [             // radius (cloud = 0.95), half width, tilt about x, roll about z, wobble, flow speed
  { r: 1.03, w: 0.045, tilt: 1.05, roll: -0.42, wob: 0.21, flow: 0.9 },
  { r: 0.98, w: 0.035, tilt: 1.22, roll: 0.55, wob: -0.17, flow: -0.7 },
  { r: 1.08, w: 0.05, tilt: 0.92, roll: 0.08, wob: 0.13, flow: 0.55 },
];
const TAU = Math.PI * 2;
const BLOOM = 0.38;
const additive = { transparent: true, depthWrite: false, depthTest: false, blending: THREE.AdditiveBlending };

// ---------------------------------------------------------------- shaders

const NOISE = `
vec3 mod289(vec3 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 mod289(vec4 x){return x-floor(x*(1.0/289.0))*289.0;}
vec4 permute(vec4 x){return mod289(((x*34.0)+1.0)*x);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}
float snoise(vec3 v){   // 3D simplex noise (Ashima Arts, MIT)
  const vec2 C=vec2(1.0/6.0,1.0/3.0); const vec4 D=vec4(0.0,0.5,1.0,2.0);
  vec3 i=floor(v+dot(v,C.yyy)); vec3 x0=v-i+dot(i,C.xxx);
  vec3 g=step(x0.yzx,x0.xyz); vec3 l=1.0-g; vec3 i1=min(g.xyz,l.zxy); vec3 i2=max(g.xyz,l.zxy);
  vec3 x1=x0-i1+C.xxx; vec3 x2=x0-i2+C.yyy; vec3 x3=x0-D.yyy;
  i=mod289(i);
  vec4 p=permute(permute(permute(i.z+vec4(0.0,i1.z,i2.z,1.0))+i.y+vec4(0.0,i1.y,i2.y,1.0))+i.x+vec4(0.0,i1.x,i2.x,1.0));
  float n_=0.142857142857; vec3 ns=n_*D.wyz-D.xzx;
  vec4 j=p-49.0*floor(p*ns.z*ns.z); vec4 x_=floor(j*ns.z); vec4 y_=floor(j-7.0*x_);
  vec4 x=x_*ns.x+ns.yyyy; vec4 y=y_*ns.x+ns.yyyy; vec4 h=1.0-abs(x)-abs(y);
  vec4 b0=vec4(x.xy,y.xy); vec4 b1=vec4(x.zw,y.zw);
  vec4 s0=floor(b0)*2.0+1.0; vec4 s1=floor(b1)*2.0+1.0; vec4 sh=-step(h,vec4(0.0));
  vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy; vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;
  vec3 p0=vec3(a0.xy,h.x); vec3 p1=vec3(a0.zw,h.y); vec3 p2=vec3(a1.xy,h.z); vec3 p3=vec3(a1.zw,h.w);
  vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
  p0*=norm.x; p1*=norm.y; p2*=norm.z; p3*=norm.w;
  vec4 m=max(0.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.0); m=m*m;
  return 42.0*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
}`;

// the cloud: each point is pushed in and out by slowly moving noise (more while speaking), brighter at the rim
const CLOUD_VS = NOISE + `
attribute float aSeed;
uniform float uTime, uIntensity, uAmp, uSize;
varying float vB, vD;
void main() {
  vec3 p = position;
  float n = snoise(p * 1.4 + vec3(uTime * 0.15));
  float n2 = snoise(p * 3.0 - vec3(uTime * 0.3));
  vec4 mv = modelViewMatrix * vec4(p * (1.0 + n * uAmp + n2 * uAmp * 0.4), 1.0);
  vec3 nv = normalize(mat3(modelViewMatrix) * p);
  vB = (0.2 + 0.8 * (1.0 - abs(nv.z))) * (0.6 + 0.4 * sin(uTime * 1.5 + aSeed * 30.0)) * uIntensity;
  vD = n;
  gl_PointSize = uSize * (5.0 / -mv.z);
  gl_Position = projectionMatrix * mv;
}`;
const CLOUD_FS = `
uniform vec3 uCyan, uIce;
varying float vB, vD;
void main() {
  float a = smoothstep(0.5, 0.0, length(gl_PointCoord - 0.5));
  gl_FragColor = vec4(mix(uCyan, uIce, clamp(vD * 0.6 + 0.5, 0.0, 1.0)) * vB * a, 1.0);
}`;

// a Möbius ribbon: light flowing along it and a comet, bright along its edges (or, for its glow, soft across
// its width); the far half is dimmer
const RIBBON_VS = `
varying vec2 vUv;
varying float vDepth;
void main() {
  vUv = uv;
  vDepth = (modelMatrix * vec4(position, 1.0)).z;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}`;
const RIBBON_FS = `
uniform vec3 uColor, uIce;
uniform float uTime, uFlow, uIntensity, uAlpha, uComet, uGlow;
varying vec2 vUv;
varying float vDepth;
void main() {
  float across = abs(vUv.y * 2.0 - 1.0);                              // 0 in the middle, 1 at the edges
  float shape = mix(0.35 + 0.65 * smoothstep(0.55, 1.0, across), exp(-across * across * 3.0), uGlow);
  shape *= 1.0 - smoothstep(0.88, 1.0, across);                       // soft edges
  float flow = 0.6 + 0.4 * sin(vUv.x * 6.2831853 * 3.0 - uTime * uFlow);
  float near = vDepth >= 0.0 ? 1.0 : 0.35;
  float behind = fract((uComet - vUv.x) * sign(uFlow + 1e-4));       // distance behind the comet's head
  float comet = exp(-behind * 26.0) * step(0.0, vDepth + 0.2);
  vec3 col = uColor * flow * near * uIntensity + uIce * comet * (uIntensity + 0.2);
  gl_FragColor = vec4(col * shape * uAlpha, 1.0);
}`;

// last pass: alpha from brightness, so the transparent canvas is valid premultiplied light
const ALPHA_SHADER = {
  uniforms: { tDiffuse: { value: null } },
  vertexShader: "varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }",
  fragmentShader: `uniform sampler2D tDiffuse; varying vec2 vUv;
    void main() {
      vec4 c = texture2D(tDiffuse, vUv);
      c.rgb *= smoothstep(1.0, 0.8, length(vUv - 0.5) * 2.0);        // fade out in a circle: no square edge
      c.rgb = max(c.rgb - 0.03, 0.0) * 1.2;                         // drop the faintest haze
      float a = clamp(max(max(c.r, c.g), c.b) * 1.25, 0.0, 1.0);    // and more solid where it shines
      gl_FragColor = vec4(min(c.rgb, vec3(a)), a);
    }`,
};

// ---------------------------------------------------------------- helpers

function mobius(r, w, segs = 240) {   // a ribbon around a circle of radius r with a half twist, in the xy plane
  const pos = [], uv = [], idx = [];
  for (let i = 0; i <= segs; i++) {
    const u = (i / segs) * TAU, c = Math.cos(u), s = Math.sin(u), ch = Math.cos(u / 2), sh = Math.sin(u / 2);
    for (const v of [-1, 1]) {
      pos.push((r + v * w * ch) * c, (r + v * w * ch) * s, v * w * sh);
      uv.push(i / segs, (v + 1) / 2);
    }
    if (i < segs) { const a = i * 2; idx.push(a, a + 1, a + 2, a + 1, a + 3, a + 2); }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  g.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
  g.setIndex(idx);
  return g;
}

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

const clampB = (b, fallback) => (Number.isFinite(b) ? Math.min(1, Math.max(0.2, b)) : fallback);

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
  camera.position.set(0, 0, 4.45);                 // the cloud (radius 0.95) fills 68% of the canvas (ORB_SCALE in ui/window.py)

  const pal = { cyan: new THREE.Color(), ice: new THREE.Color(), deep: new THREE.Color() };
  const target = { cyan: new THREE.Color(), ice: new THREE.Color(), deep: new THREE.Color() };
  const setPalette = (into, name) => { const p = PALETTES[name] || PALETTES.blue; for (const k in p) into[k].setHex(p[k]); };
  setPalette(pal, "blue"); setPalette(target, "blue");
  const bright = { core: 0.55, cloud: 0.45, ribbons: 0.5 };
  let coreShare = CORE_SHARE;

  // the cloud: points spread evenly over a sphere, a little uneven so it looks soft
  const pos = new Float32Array(CLOUD_POINTS * 3), seeds = new Float32Array(CLOUD_POINTS);
  for (let i = 0; i < CLOUD_POINTS; i++) {
    const y = 1 - (i / (CLOUD_POINTS - 1)) * 2, rad = Math.sqrt(1 - y * y), th = i * 2.399963;
    const k = CLOUD_RADIUS * (1 + (Math.random() - 0.5) * 0.08);
    pos.set([Math.cos(th) * rad * k, y * k, Math.sin(th) * rad * k], i * 3);
    seeds[i] = Math.random();
  }
  const cloudGeo = new THREE.BufferGeometry();
  cloudGeo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  cloudGeo.setAttribute("aSeed", new THREE.BufferAttribute(seeds, 1));
  const cloudMat = new THREE.ShaderMaterial({ vertexShader: CLOUD_VS, fragmentShader: CLOUD_FS, ...additive,
    uniforms: { uTime: { value: 0 }, uIntensity: { value: 1 }, uAmp: { value: 0.08 }, uSize: { value: 2 },
      uCyan: { value: pal.cyan }, uIce: { value: pal.ice } } });
  const cloud = new THREE.Points(cloudGeo, cloudMat);
  cloud.rotation.x = 0.3;
  scene.add(cloud);

  // the arc reactor, built at the cloud's radius and scaled down to its share: ten segments, an inner ring, an
  // outer ring with ticks, and the nucleus in the middle
  const R = CLOUD_RADIUS;
  const reactor = new THREE.Group();
  scene.add(reactor);
  const flat = (geo, colour) => new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color: colour.clone(), ...additive, side: THREE.DoubleSide }));
  const segments = [];
  for (let i = 0; i < SEGMENTS; i++) {
    const a = (i / SEGMENTS) * TAU;
    segments.push(flat(arcs([[R * 0.42, R * 0.64, a + 0.05, a + TAU / SEGMENTS - 0.05]]), pal.cyan));
  }
  const innerRing = flat(arcs([[R * 0.3, R * 0.33, 0, TAU]]), pal.ice);
  const ticks = [];
  for (let i = 0; i < 60; i++) { const a = (i / 60) * TAU; ticks.push([R * 0.72, i % 5 === 0 ? R * 0.8 : R * 0.76, a, a + 0.015]); }
  const outerRing = flat(arcs([[R * 0.69, R * 0.705, 0, TAU], ...ticks]), pal.cyan);
  reactor.add(...segments, innerRing, outerRing);
  const nucleus = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTexture([[0, "rgba(255,255,255,1)"], [0.25, "rgba(160,230,255,0.8)"], [0.6, "rgba(40,140,255,0.2)"], [1, "rgba(0,0,0,0)"]]), ...additive }));
  scene.add(nucleus);

  // the Möbius ribbons around the cloud: a ribbon with bright edges and a wide soft glow each
  const ribbons = RIBBONS.map((b, bi) => {
    const group = new THREE.Group();
    group.rotation.order = "ZYX";
    for (const [w, alpha, glow] of [[b.w, 1.0, 0], [b.w * 3, 0.14, 1]]) {
      group.add(new THREE.Mesh(mobius(b.r, w), new THREE.ShaderMaterial({
        vertexShader: RIBBON_VS, fragmentShader: RIBBON_FS, ...additive, side: THREE.DoubleSide,
        uniforms: { uColor: { value: pal.cyan }, uIce: { value: pal.ice }, uTime: { value: 0 }, uFlow: { value: b.flow },
          uIntensity: { value: 1 }, uAlpha: { value: alpha }, uComet: { value: bi * 0.3 }, uGlow: { value: glow } } })));
    }
    scene.add(group);
    return { ...b, group };
  });

  // lightning along the ribbons, while JARVIS speaks
  const bolts = Array.from({ length: 5 }, () => {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(17 * 3), 3));
    const line = new THREE.Line(geo, new THREE.LineBasicMaterial({ color: pal.ice.clone(), ...additive, opacity: 0 }));
    line.visible = false;
    scene.add(line);
    return { line, born: 0, life: 0, ribbon: null, th0: 0, len: 0, seed: 0 };
  });
  const v = new THREE.Vector3();

  // bloom, then colour output, then alpha from brightness
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), BLOOM, 0.0, 0.3);
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
    cloudMat.uniforms.uSize.value = Math.max(1.2, 2.2 * (px / 300));
  }

  return {
    setTint(name) { setPalette(target, name); },
    is3d: true,
    setBrightness(core, cloudB, ribbonsB) {
      bright.core = clampB(core, bright.core); bright.cloud = clampB(cloudB, bright.cloud); bright.ribbons = clampB(ribbonsB, bright.ribbons);
    },
    setCoreSize(share) { if (Number.isFinite(share)) coreShare = Math.min(1, Math.max(0.4, share)); },
    draw(t, act, lv, dt) {
      dt = Math.min(Math.max(dt || 1 / 60, 0), 0.1);
      fit();
      const ease = Math.min(1, dt * 4);
      for (const k of ["cyan", "ice", "deep"]) pal[k].lerp(target[k], ease);
      const build = 0.55 + 0.35 * act + 0.35 * lv * act;   // low when idle, builds with activity
      const I = bright.core * build, C = bright.cloud * build, B = bright.ribbons * build;

      const u = cloudMat.uniforms;
      u.uTime.value = t * (1 + 1.5 * act); u.uIntensity.value = 1.35 * C; u.uAmp.value = 0.06 + 0.06 * act + 0.3 * lv * act;
      cloud.rotation.y = t * (0.06 + 0.2 * act);

      reactor.rotation.set(0.35 * Math.sin(t * 0.3), 0.45 * Math.sin(t * 0.23), 0);   // a gentle tilt: it reads as 3D
      reactor.scale.setScalar(coreShare * (1 + 0.04 * lv * act));
      outerRing.rotation.z = -t * (0.1 + 0.3 * act);
      innerRing.rotation.z = t * 0.2;
      const head = (t * (1.5 + 6 * act)) % SEGMENTS;   // the chase runs while JARVIS is listening, speaking or working
      segments.forEach((m, i) => {
        const behind = ((head - i) % SEGMENTS + SEGMENTS) % SEGMENTS, chase = act > 0.3 ? Math.exp(-behind * 0.8) : 0;
        m.material.opacity = Math.min(1, I * (0.55 + 0.9 * chase + 0.3 * lv * act));
        m.material.color.copy(pal.cyan).lerp(pal.ice, chase);
      });
      innerRing.material.opacity = Math.min(1, I * 1.2);
      innerRing.material.color.copy(pal.ice);
      outerRing.material.opacity = I * 0.8;
      outerRing.material.color.copy(pal.cyan);
      nucleus.material.opacity = Math.min(1, (0.5 + 0.4 * act) * I * 1.4);
      nucleus.material.color.copy(pal.ice);
      nucleus.scale.setScalar(NUCLEUS * CLOUD_RADIUS * coreShare * (1 + 0.45 * lv * act + 0.07 * Math.sin(t * 2)));

      ribbons.forEach((b, bi) => {
        b.group.rotation.x = b.tilt + Math.sin(t * b.wob) * 0.12;
        b.group.rotation.z = b.roll + Math.cos(t * b.wob * 0.8) * 0.1;
        b.group.rotation.y = t * 0.05 * (bi % 2 ? -1 : 1);
        b.group.updateMatrixWorld();
        const comet = ((((t * b.flow * (0.8 + 1.6 * act)) / TAU + bi * 0.3) % 1) + 1) % 1;
        for (const mesh of b.group.children) {
          const u = mesh.material.uniforms;
          u.uTime.value = t * (2 + 4 * act); u.uIntensity.value = B * 1.3; u.uComet.value = comet;
        }
      });
      if (Math.random() < (0.3 + 6 * act * lv) * dt) {
        const free = bolts.find((b) => !b.line.visible);
        if (free) {
          Object.assign(free, { ribbon: ribbons[Math.floor(Math.random() * ribbons.length)], th0: Math.random() * TAU,
            len: 0.3 + Math.random() * (0.4 + 0.6 * lv * act), born: t, life: 0.1 + Math.random() * 0.15, seed: Math.random() * 1000 });
          free.line.visible = true;
        }
      }
      for (const bo of bolts) {
        if (!bo.line.visible) continue;
        const age = (t - bo.born) / bo.life;
        if (age > 1 || age < 0) { bo.line.visible = false; continue; }
        const arr = bo.line.geometry.attributes.position.array;
        for (let q = 0; q <= 16; q++) {
          const th = bo.th0 + (q / 16) * bo.len;
          v.set(Math.cos(th) * bo.ribbon.r, Math.sin(th) * bo.ribbon.r, 0).applyMatrix4(bo.ribbon.group.matrixWorld);
          const j = q === 0 || q === 16 ? 0 : Math.sin(bo.seed + q * 12.9898 + Math.floor(t * 30)) * 0.04, l = Math.hypot(v.x, v.y) || 1;
          arr.set([v.x + (v.x / l) * j, v.y + (v.y / l) * j, v.z], q * 3);
        }
        bo.line.geometry.attributes.position.needsUpdate = true;
        bo.line.material.color.copy(pal.ice);
        bo.line.material.opacity = (1 - age) * Math.min(1, B * 1.6);
      }

      bloom.strength = BLOOM * (0.6 + 0.6 * Math.max(bright.core, bright.cloud, bright.ribbons));
      composer.render(dt);
    },
  };
}
