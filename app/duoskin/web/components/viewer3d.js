// @ts-check
// The 3D viewer (APP_SPEC 9.4, 11, 12): orbit, zoom, turntable, one or two models side by side (Gate 3 shows both
// characters). three.js is vendored under /vendor/three (tools/vendor_three.py) and loaded only when a viewer opens.
//
// CSP notes (security.py): script-src 'self' + the import-map hash, so no eval and no CDN; connect-src falls back to
// default-src 'self', which blocks fetch("blob:..."). GLTFLoader reads embedded textures with ImageBitmapLoader (that
// fetch), so for the one synchronous parse() call the loader constructor sees no createImageBitmap and uses TextureLoader
// (an <img>, allowed by img-src blob:). Nothing in the vendored files is edited.
import { h } from "../dom.js";
import { authHeaders } from "../api.js";

/** @type {Promise<any> | null} */
let threeModules = null;

function loadThree() {
  threeModules ??= Promise.all([
    import("three"),
    import("three/addons/controls/OrbitControls.js"),
    import("three/addons/loaders/GLTFLoader.js"),
    import("three/addons/environments/RoomEnvironment.js"),
  ]).then(([THREE, oc, gl, re]) => ({ THREE, OrbitControls: oc.OrbitControls, GLTFLoader: gl.GLTFLoader, RoomEnvironment: re.RoomEnvironment }));
  return threeModules;
}

/** True when this browser can make a WebGL context. */
export function webglAvailable() {
  try {
    const c = document.createElement("canvas");
    return Boolean(c.getContext("webgl2") || c.getContext("webgl"));
  } catch { return false; }
}

/** @param {() => any} fn */
function withoutImageBitmap(fn) {
  const g = /** @type {any} */ (globalThis);
  const saved = g.createImageBitmap;
  try { g.createImageBitmap = undefined; return fn(); } finally { g.createImageBitmap = saved; }
}

/** @param {ArrayBuffer} buf @returns {"glb" | "fbx" | "unknown"} */
export function sniffModel(buf) {
  const head = new Uint8Array(buf.slice(0, 32));
  const text = String.fromCharCode(...head);
  if (text.startsWith("glTF")) return "glb";
  if (text.startsWith("Kaydara FBX Binary")) return "fbx";
  if (text.startsWith("{")) return "glb";   // a .gltf JSON file
  return "unknown";
}

/**
 * Build a viewer inside `host`. Returns immediately; the model loads when you call `load`.
 * @param {HTMLElement} host
 * @param {{height?: number, turntable?: boolean, onStatus?: (text: string, tone: "info" | "bad") => void}} [opts]
 */
export function createViewer(host, opts = {}) {
  const status = h("p", { class: "viewer-status", role: "status" }, "Loading the 3D viewer…");
  const canvasBox = h("div", { class: "viewer-canvas", tabindex: "0", role: "img", "aria-label": "3D preview. Drag to turn it, scroll to zoom, arrow keys to turn." });
  const stats = h("p", { class: "viewer-stats muted" });
  /** @type {any} */ let three = null;
  /** @type {any} */ let renderer = null;
  /** @type {any} */ let scene = null;
  /** @type {any} */ let camera = null;
  /** @type {any} */ let controls = null;
  /** @type {any[]} */ let objects = [];
  let turntable = opts.turntable ?? true;
  let disposed = false;
  let visible = true;
  /** @type {ResizeObserver | null} */ let ro = null;
  /** @type {IntersectionObserver | null} */ let io = null;
  let ready = false;
  /** @type {(() => void)[]} */ const pending = [];

  /** @param {string} text @param {"info" | "bad"} [tone] */
  const say = (text, tone = "info") => { status.textContent = text; status.className = `viewer-status ${tone}`; opts.onStatus?.(text, tone); };

  async function init() {
    if (!webglAvailable()) {
      canvasBox.replaceChildren(h("p", { class: "viewer-fallback" }, "The 3D preview needs graphics support that this browser window does not have. The flat pictures are still shown next to it."));
      say("3D preview is not available in this browser.", "bad");
      return false;
    }
    try { three = await loadThree(); } catch (err) {
      console.error(err);
      say("The 3D viewer could not be loaded.", "bad");
      return false;
    }
    if (disposed) return false;
    const { THREE, OrbitControls, RoomEnvironment } = three;
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    canvasBox.replaceChildren(renderer.domElement);
    scene = new THREE.Scene();
    const pmrem = new THREE.PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    pmrem.dispose();
    scene.add(new THREE.HemisphereLight(0xffffff, 0x8888aa, 0.6));
    const key = new THREE.DirectionalLight(0xffffff, 1.2);
    key.position.set(3, 5, 4);
    scene.add(key);
    camera = new THREE.PerspectiveCamera(35, 1, 0.01, 200);
    camera.position.set(0, 1.4, 6);
    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.autoRotate = turntable;
    controls.autoRotateSpeed = 1.6;
    controls.addEventListener("start", () => { controls.autoRotate = false; });
    ro = new ResizeObserver(resize);
    ro.observe(host);
    io = new IntersectionObserver((entries) => { visible = entries.some((e) => e.isIntersecting); });
    io.observe(host);
    resize();
    renderer.setAnimationLoop(() => {
      if (!visible || document.hidden) return;
      controls.update();
      renderer.render(scene, camera);
    });
    canvasBox.addEventListener("keydown", (e) => {
      const k = e.key;
      if (k === "ArrowLeft" || k === "ArrowRight") { e.preventDefault(); controls.autoRotate = false; scene.rotation.y += k === "ArrowLeft" ? -0.15 : 0.15; }
      else if (k === "+" || k === "=") camera.position.multiplyScalar(0.9);
      else if (k === "-") camera.position.multiplyScalar(1.1);
    });
    ready = true;
    say("");
    for (const fn of pending.splice(0)) fn();
    return true;
  }

  function resize() {
    if (!renderer || !camera) return;
    const w = Math.max(160, host.clientWidth);
    const hh = opts.height ?? 360;
    renderer.setSize(w, hh, false);
    renderer.domElement.style.width = "100%";
    renderer.domElement.style.height = `${hh}px`;
    camera.aspect = w / hh;
    camera.updateProjectionMatrix();
  }

  function frame() {
    if (!three || !objects.length) return;
    const { THREE } = three;
    const box = new THREE.Box3();
    for (const o of objects) box.expandByObject(o);
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());
    const radius = Math.max(size.x, size.y, size.z) * 0.5 || 1;
    const dist = radius / Math.tan((camera.fov * Math.PI) / 360) * 1.35;
    controls.target.copy(center);
    camera.position.set(center.x + dist * 0.45, center.y + radius * 0.25, center.z + dist);
    camera.near = Math.max(0.01, dist / 100);
    camera.far = dist * 20;
    camera.updateProjectionMatrix();
    controls.update();
  }

  /** Put the loaded models in a row, centred, each scaled to the same height. */
  function arrange() {
    const { THREE } = three;
    const TARGET = 4;
    let x = 0;
    const gap = 0.6;
    /** @type {{obj: any, w: number}[]} */
    const placed = [];
    for (const obj of objects) {
      obj.position.set(0, 0, 0);
      obj.scale.set(1, 1, 1);
      const b = new THREE.Box3().setFromObject(obj);
      const s = b.getSize(new THREE.Vector3());
      const k = TARGET / (s.y || Math.max(s.x, s.z) || 1);
      obj.scale.setScalar(k);
      const b2 = new THREE.Box3().setFromObject(obj);
      const c = b2.getCenter(new THREE.Vector3());
      obj.position.set(-c.x, -b2.min.y, -c.z);
      placed.push({ obj, w: b2.getSize(new THREE.Vector3()).x });
    }
    const total = placed.reduce((a, p) => a + p.w, 0) + gap * Math.max(0, placed.length - 1);
    x = -total / 2;
    for (const p of placed) { p.obj.position.x += x + p.w / 2; x += p.w + gap; }
    frame();
  }

  /** @param {any} obj */
  function countTriangles(obj) {
    let tris = 0;
    obj.traverse((/** @type {any} */ n) => {
      if (n.isMesh && n.geometry) {
        const g = n.geometry;
        tris += g.index ? g.index.count / 3 : (g.attributes.position?.count ?? 0) / 3;
      }
    });
    return Math.round(tris);
  }

  /** @param {ArrayBuffer} buf @param {"glb" | "fbx"} kind */
  async function parse(buf, kind) {
    if (kind === "fbx") {
      const { FBXLoader } = await import("three/addons/loaders/FBXLoader.js");
      return withoutImageBitmap(() => new FBXLoader().parse(buf, ""));
    }
    return new Promise((resolve, reject) => {
      withoutImageBitmap(() => new three.GLTFLoader().parse(buf, "", (/** @type {any} */ gltf) => resolve(gltf.scene), reject));
    });
  }

  /** @param {ArrayBuffer} buf */
  async function addBuffer(buf) {
    const kind = sniffModel(buf);
    if (kind === "unknown") throw new Error("not a 3D file");
    const obj = await parse(buf, kind);
    if (disposed) return;
    scene.add(obj);
    objects.push(obj);
    return obj;
  }

  /** @param {() => Promise<void>} work */
  function whenReady(work) {
    return new Promise((resolve, reject) => {
      const run = () => work().then(resolve, reject);
      if (ready) run(); else if (disposed) resolve(undefined); else pending.push(run);
    });
  }

  const api = {
    host,
    /** Replace the models with these (one or two). @param {{url?: string, file?: File}[]} sources */
    load(sources) {
      return whenReady(async () => {
        say("Opening the 3D model…");
        clearObjects();
        try {
          let tris = 0;
          for (const s of sources) {
            let buf;
            if (s.file) buf = await s.file.arrayBuffer();
            else {
              const res = await fetch(/** @type {string} */ (s.url), { headers: authHeaders(), cache: "force-cache" });
              if (!res.ok) throw new Error("download failed");
              buf = await res.arrayBuffer();
            }
            const obj = await addBuffer(buf);
            if (obj) tris += countTriangles(obj);
          }
          if (disposed) return;
          arrange();
          controls.autoRotate = turntable;
          stats.textContent = `${tris.toLocaleString()} triangles`;
          say("");
        } catch (err) {
          console.error(err);
          say("This 3D file could not be opened. Is it a GLB or FBX file?", "bad");
          throw err;
        }
      });
    },
    resetView() { if (ready) { frame(); controls.autoRotate = turntable; } },
    /** @param {boolean} on */
    setTurntable(on) { turntable = on; if (controls) controls.autoRotate = on; },
    get turntable() { return turntable; },
    /** PNG of what is on screen (for tests and the "save a picture" button). */
    snapshot() { return renderer ? renderer.domElement.toDataURL("image/png") : null; },
    get ready() { return ready; },
    dispose() {
      disposed = true;
      ro?.disconnect();
      io?.disconnect();
      clearObjects();
      if (renderer) {
        renderer.setAnimationLoop(null);
        renderer.dispose();
        try { renderer.forceContextLoss(); } catch { /* already lost */ }
      }
      controls?.dispose();
      renderer = null;
    },
  };

  function clearObjects() {
    for (const o of objects) {
      scene?.remove(o);
      o.traverse((/** @type {any} */ n) => {
        n.geometry?.dispose?.();
        const mats = Array.isArray(n.material) ? n.material : n.material ? [n.material] : [];
        for (const m of mats) {
          for (const v of Object.values(m)) if (v && /** @type {any} */ (v).isTexture) /** @type {any} */ (v).dispose();
          m.dispose?.();
        }
      });
    }
    objects = [];
  }

  host.classList.add("viewer");
  host.replaceChildren(canvasBox, status, stats);
  void init();
  return api;
}

/**
 * A viewer with its toolbar: turntable on/off, reset view, and a text alternative.
 * @param {{models: {url?: string, file?: File}[], height?: number, caption?: string, onError?: (m: string) => void}} o
 */
export function viewerPanel(o) {
  const host = h("div", { class: "viewer-host" });
  const viewer = createViewer(host, { height: o.height, onStatus: (t, tone) => { if (tone === "bad") o.onError?.(t); } });
  const turn = h("button", { type: "button", class: "btn small", "aria-pressed": "true", onclick: () => {
    viewer.setTurntable(!viewer.turntable);
    turn.setAttribute("aria-pressed", String(viewer.turntable));
    turn.textContent = viewer.turntable ? "Stop turning" : "Turn slowly";
  } }, "Stop turning");
  const reset = h("button", { type: "button", class: "btn small", onclick: () => viewer.resetView() }, "Reset view");
  const el = h("div", { class: "viewer-panel" }, host,
    h("div", { class: "viewer-tools" }, turn, reset, o.caption ? h("span", { class: "muted" }, o.caption) : h("span", { class: "muted" }, "Drag to turn, scroll to zoom.")));
  if (o.models.length) viewer.load(o.models).catch(() => {});
  return { el, viewer };
}
