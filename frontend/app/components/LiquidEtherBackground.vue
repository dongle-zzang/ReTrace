<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import * as THREE from 'three'

// Adapted from Vue Bits Liquid Ether (https://vue-bits.dev/backgrounds/liquid-ether).
// Stable-fluids simulation; pauses while off-screen or when the tab is hidden.
const props = withDefaults(defineProps<{
  colors?: string[]
  mouseForce?: number
  cursorSize?: number
  isViscous?: boolean
  viscous?: number
  iterationsViscous?: number
  iterationsPoisson?: number
  dt?: number
  BFECC?: boolean
  resolution?: number
  isBounce?: boolean
  autoDemo?: boolean
  autoSpeed?: number
  autoIntensity?: number
  takeoverDuration?: number
  autoResumeDelay?: number
  autoRampDuration?: number
}>(), {
  colors: () => ['#5227FF', '#FF9FFC', '#B19EEF'],
  mouseForce: 20,
  cursorSize: 100,
  isViscous: false,
  viscous: 30,
  iterationsViscous: 32,
  iterationsPoisson: 32,
  dt: 0.014,
  BFECC: true,
  resolution: 0.5,
  isBounce: false,
  autoDemo: true,
  autoSpeed: 0.5,
  autoIntensity: 2.2,
  takeoverDuration: 0.25,
  autoResumeDelay: 1000,
  autoRampDuration: 0.6,
})

const root = ref<HTMLElement | null>(null)
const failed = ref(false)

const faceVert = `
attribute vec3 position;
uniform vec2 px;
uniform vec2 boundarySpace;
varying vec2 uv;
precision highp float;
void main(){
  vec3 pos = position;
  vec2 scale = 1.0 - boundarySpace * 2.0;
  pos.xy = pos.xy * scale;
  uv = vec2(0.5) + (pos.xy) * 0.5;
  gl_Position = vec4(pos, 1.0);
}`

const lineVert = `
attribute vec3 position;
uniform vec2 px;
precision highp float;
varying vec2 uv;
void main(){
  vec3 pos = position;
  uv = 0.5 + pos.xy * 0.5;
  vec2 n = sign(pos.xy);
  pos.xy = abs(pos.xy) - px * 1.0;
  pos.xy *= n;
  gl_Position = vec4(pos, 1.0);
}`

const mouseVert = `
precision highp float;
attribute vec3 position;
attribute vec2 uv;
uniform vec2 center;
uniform vec2 scale;
uniform vec2 px;
varying vec2 vUv;
void main(){
  vec2 pos = position.xy * scale * 2.0 * px + center;
  vUv = uv;
  gl_Position = vec4(pos, 0.0, 1.0);
}`

const advectionFrag = `
precision highp float;
uniform sampler2D velocity;
uniform float dt;
uniform bool isBFECC;
uniform vec2 fboSize;
uniform vec2 px;
varying vec2 uv;
void main(){
  vec2 ratio = max(fboSize.x, fboSize.y) / fboSize;
  if(isBFECC == false){
    vec2 vel = texture2D(velocity, uv).xy;
    vec2 uv2 = uv - vel * dt * ratio;
    vec2 newVel = texture2D(velocity, uv2).xy;
    gl_FragColor = vec4(newVel, 0.0, 0.0);
  } else {
    vec2 spot_new = uv;
    vec2 vel_old = texture2D(velocity, uv).xy;
    vec2 spot_old = spot_new - vel_old * dt * ratio;
    vec2 vel_new1 = texture2D(velocity, spot_old).xy;
    vec2 spot_new2 = spot_old + vel_new1 * dt * ratio;
    vec2 error = spot_new2 - spot_new;
    vec2 spot_new3 = spot_new - error / 2.0;
    vec2 vel_2 = texture2D(velocity, spot_new3).xy;
    vec2 spot_old2 = spot_new3 - vel_2 * dt * ratio;
    vec2 newVel2 = texture2D(velocity, spot_old2).xy;
    gl_FragColor = vec4(newVel2, 0.0, 0.0);
  }
}`

const colorFrag = `
precision highp float;
uniform sampler2D velocity;
uniform sampler2D palette;
uniform vec4 bgColor;
varying vec2 uv;
void main(){
  vec2 vel = texture2D(velocity, uv).xy;
  float lenv = clamp(length(vel), 0.0, 1.0);
  vec3 c = texture2D(palette, vec2(lenv, 0.5)).rgb;
  vec3 outRGB = mix(bgColor.rgb, c, lenv);
  float outA = mix(bgColor.a, 1.0, lenv);
  gl_FragColor = vec4(outRGB, outA);
}`

const divergenceFrag = `
precision highp float;
uniform sampler2D velocity;
uniform float dt;
uniform vec2 px;
varying vec2 uv;
void main(){
  float x0 = texture2D(velocity, uv - vec2(px.x, 0.0)).x;
  float x1 = texture2D(velocity, uv + vec2(px.x, 0.0)).x;
  float y0 = texture2D(velocity, uv - vec2(0.0, px.y)).y;
  float y1 = texture2D(velocity, uv + vec2(0.0, px.y)).y;
  float divergence = (x1 - x0 + y1 - y0) / 2.0;
  gl_FragColor = vec4(divergence / dt);
}`

const externalForceFrag = `
precision highp float;
uniform vec2 force;
uniform vec2 center;
uniform vec2 scale;
uniform vec2 px;
varying vec2 vUv;
void main(){
  vec2 circle = (vUv - 0.5) * 2.0;
  float d = 1.0 - min(length(circle), 1.0);
  d *= d;
  gl_FragColor = vec4(force * d, 0.0, 1.0);
}`

const poissonFrag = `
precision highp float;
uniform sampler2D pressure;
uniform sampler2D divergence;
uniform vec2 px;
varying vec2 uv;
void main(){
  float p0 = texture2D(pressure, uv + vec2(px.x * 2.0, 0.0)).r;
  float p1 = texture2D(pressure, uv - vec2(px.x * 2.0, 0.0)).r;
  float p2 = texture2D(pressure, uv + vec2(0.0, px.y * 2.0)).r;
  float p3 = texture2D(pressure, uv - vec2(0.0, px.y * 2.0)).r;
  float div = texture2D(divergence, uv).r;
  float newP = (p0 + p1 + p2 + p3) / 4.0 - div;
  gl_FragColor = vec4(newP);
}`

const pressureFrag = `
precision highp float;
uniform sampler2D pressure;
uniform sampler2D velocity;
uniform vec2 px;
uniform float dt;
varying vec2 uv;
void main(){
  float p0 = texture2D(pressure, uv + vec2(px.x, 0.0)).r;
  float p1 = texture2D(pressure, uv - vec2(px.x, 0.0)).r;
  float p2 = texture2D(pressure, uv + vec2(0.0, px.y)).r;
  float p3 = texture2D(pressure, uv - vec2(0.0, px.y)).r;
  vec2 v = texture2D(velocity, uv).xy;
  vec2 gradP = vec2(p0 - p1, p2 - p3) * 0.5;
  v = v - gradP * dt;
  gl_FragColor = vec4(v, 0.0, 1.0);
}`

const viscousFrag = `
precision highp float;
uniform sampler2D velocity;
uniform sampler2D velocity_new;
uniform float v;
uniform vec2 px;
uniform float dt;
varying vec2 uv;
void main(){
  vec2 old = texture2D(velocity, uv).xy;
  vec2 new0 = texture2D(velocity_new, uv + vec2(px.x * 2.0, 0.0)).xy;
  vec2 new1 = texture2D(velocity_new, uv - vec2(px.x * 2.0, 0.0)).xy;
  vec2 new2 = texture2D(velocity_new, uv + vec2(0.0, px.y * 2.0)).xy;
  vec2 new3 = texture2D(velocity_new, uv - vec2(0.0, px.y * 2.0)).xy;
  vec2 newv = 4.0 * old + v * dt * (new0 + new1 + new2 + new3);
  newv /= 4.0 * (1.0 + v * dt);
  gl_FragColor = vec4(newv, 0.0, 0.0);
}`

function makePaletteTexture(stops: string[]) {
  const colors = stops.length ? (stops.length === 1 ? [stops[0]!, stops[0]!] : stops) : ['#ffffff', '#ffffff']
  const data = new Uint8Array(colors.length * 4)
  colors.forEach((hex, index) => {
    const color = new THREE.Color(hex)
    data[index * 4] = Math.round(color.r * 255)
    data[index * 4 + 1] = Math.round(color.g * 255)
    data[index * 4 + 2] = Math.round(color.b * 255)
    data[index * 4 + 3] = 255
  })
  const texture = new THREE.DataTexture(data, colors.length, 1, THREE.RGBAFormat)
  texture.magFilter = THREE.LinearFilter
  texture.minFilter = THREE.LinearFilter
  texture.wrapS = THREE.ClampToEdgeWrapping
  texture.wrapT = THREE.ClampToEdgeWrapping
  texture.generateMipmaps = false
  texture.needsUpdate = true
  return texture
}

let teardown: (() => void) | null = null

function start(container: HTMLElement) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
  renderer.autoClear = false
  renderer.setClearColor(new THREE.Color(0x000000), 0)
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
  const canvas = renderer.domElement
  canvas.style.width = '100%'
  canvas.style.height = '100%'
  canvas.style.display = 'block'
  container.prepend(canvas)

  const size = { width: 1, height: 1 }
  function measure() {
    const rect = container.getBoundingClientRect()
    size.width = Math.max(1, Math.floor(rect.width))
    size.height = Math.max(1, Math.floor(rect.height))
    renderer.setSize(size.width, size.height, false)
  }
  measure()

  const camera = new THREE.Camera()
  const quad = new THREE.PlaneGeometry(2, 2)
  const disposables: { dispose(): void }[] = [quad]
  const palette = makePaletteTexture(props.colors)
  disposables.push(palette)

  const fboSize = new THREE.Vector2()
  const cellScale = new THREE.Vector2()
  const boundarySpace = new THREE.Vector2()

  function computeFboSize() {
    const resolution = Math.max(0.1, Math.min(1, props.resolution))
    fboSize.set(Math.max(1, Math.round(resolution * size.width)), Math.max(1, Math.round(resolution * size.height)))
    cellScale.set(1 / fboSize.x, 1 / fboSize.y)
  }
  computeFboSize()

  const fboOptions = {
    type: THREE.HalfFloatType,
    depthBuffer: false,
    stencilBuffer: false,
    minFilter: THREE.LinearFilter,
    magFilter: THREE.LinearFilter,
    wrapS: THREE.ClampToEdgeWrapping,
    wrapT: THREE.ClampToEdgeWrapping,
  } as const
  const fboNames = ['vel0', 'vel1', 'viscous0', 'viscous1', 'div', 'pressure0', 'pressure1'] as const
  const fbos = Object.fromEntries(
    fboNames.map((name) => [name, new THREE.WebGLRenderTarget(fboSize.x, fboSize.y, fboOptions)]),
  ) as Record<(typeof fboNames)[number], THREE.WebGLRenderTarget>

  function makePass(vertexShader: string, fragmentShader: string, uniforms: Record<string, THREE.IUniform>, geometry: THREE.BufferGeometry = quad, blending?: THREE.Blending) {
    const scene = new THREE.Scene()
    const material = new THREE.RawShaderMaterial({ vertexShader, fragmentShader, uniforms, ...(blending === undefined ? {} : { blending }) })
    disposables.push(material)
    const mesh = new THREE.Mesh(geometry, material)
    scene.add(mesh)
    return { scene, mesh, uniforms }
  }

  function renderPass(scene: THREE.Scene, target: THREE.WebGLRenderTarget | null) {
    renderer.setRenderTarget(target)
    renderer.render(scene, camera)
    renderer.setRenderTarget(null)
  }

  // Advection with an optional bounce boundary line.
  const advection = makePass(faceVert, advectionFrag, {
    boundarySpace: { value: cellScale },
    px: { value: cellScale },
    fboSize: { value: fboSize },
    velocity: { value: fbos.vel0.texture },
    dt: { value: props.dt },
    isBFECC: { value: true },
  })
  const boundaryGeometry = new THREE.BufferGeometry()
  boundaryGeometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array([
    -1, -1, 0, -1, 1, 0, -1, 1, 0, 1, 1, 0, 1, 1, 0, 1, -1, 0, 1, -1, 0, -1, -1, 0,
  ]), 3))
  disposables.push(boundaryGeometry)
  const boundaryMaterial = new THREE.RawShaderMaterial({
    vertexShader: lineVert,
    fragmentShader: advectionFrag,
    uniforms: advection.uniforms,
  })
  disposables.push(boundaryMaterial)
  const boundaryLine = new THREE.LineSegments(boundaryGeometry, boundaryMaterial)
  advection.scene.add(boundaryLine)

  const mouseGeometry = new THREE.PlaneGeometry(1, 1)
  disposables.push(mouseGeometry)
  const externalForce = makePass(mouseVert, externalForceFrag, {
    px: { value: cellScale },
    force: { value: new THREE.Vector2() },
    center: { value: new THREE.Vector2() },
    scale: { value: new THREE.Vector2(props.cursorSize, props.cursorSize) },
  }, mouseGeometry, THREE.AdditiveBlending)

  const viscous = makePass(faceVert, viscousFrag, {
    boundarySpace: { value: boundarySpace },
    velocity: { value: fbos.vel1.texture },
    velocity_new: { value: fbos.viscous0.texture },
    v: { value: props.viscous },
    px: { value: cellScale },
    dt: { value: props.dt },
  })

  const divergence = makePass(faceVert, divergenceFrag, {
    boundarySpace: { value: boundarySpace },
    velocity: { value: fbos.viscous0.texture },
    px: { value: cellScale },
    dt: { value: props.dt },
  })

  const poisson = makePass(faceVert, poissonFrag, {
    boundarySpace: { value: boundarySpace },
    pressure: { value: fbos.pressure0.texture },
    divergence: { value: fbos.div.texture },
    px: { value: cellScale },
  })

  const pressure = makePass(faceVert, pressureFrag, {
    boundarySpace: { value: boundarySpace },
    pressure: { value: fbos.pressure0.texture },
    velocity: { value: fbos.viscous0.texture },
    px: { value: cellScale },
    dt: { value: props.dt },
  })

  const output = makePass(faceVert, colorFrag, {
    velocity: { value: fbos.vel0.texture },
    boundarySpace: { value: new THREE.Vector2() },
    palette: { value: palette },
    bgColor: { value: new THREE.Vector4(0, 0, 0, 0) },
  })

  // Pointer state in normalized device coordinates.
  const mouse = {
    coords: new THREE.Vector2(),
    coordsOld: new THREE.Vector2(),
    diff: new THREE.Vector2(),
    isHoverInside: false,
    hasUserControl: false,
    isAutoActive: false,
    takeoverActive: false,
    takeoverStart: 0,
    takeoverFrom: new THREE.Vector2(),
    takeoverTo: new THREE.Vector2(),
  }
  let lastUserInteraction = performance.now()

  function toNdc(clientX: number, clientY: number) {
    const rect = container.getBoundingClientRect()
    if (!rect.width || !rect.height) return null
    const nx = (clientX - rect.left) / rect.width
    const ny = (clientY - rect.top) / rect.height
    if (nx < 0 || nx > 1 || ny < 0 || ny > 1) return null
    return { x: nx * 2 - 1, y: -(ny * 2 - 1) }
  }

  function onPointerMove(event: PointerEvent) {
    const point = toNdc(event.clientX, event.clientY)
    mouse.isHoverInside = !!point
    if (!point) return
    lastUserInteraction = performance.now()
    if (mouse.isAutoActive && !mouse.hasUserControl && !mouse.takeoverActive) {
      mouse.takeoverFrom.copy(mouse.coords)
      mouse.takeoverTo.set(point.x, point.y)
      mouse.takeoverStart = performance.now()
      mouse.takeoverActive = true
      mouse.hasUserControl = true
      mouse.isAutoActive = false
      return
    }
    mouse.coords.set(point.x, point.y)
    mouse.hasUserControl = true
  }

  function onPointerLeave() {
    mouse.isHoverInside = false
  }

  function updateMouse() {
    if (mouse.takeoverActive) {
      const t = (performance.now() - mouse.takeoverStart) / (props.takeoverDuration * 1000)
      if (t >= 1) {
        mouse.takeoverActive = false
        mouse.coords.copy(mouse.takeoverTo)
        mouse.coordsOld.copy(mouse.coords)
        mouse.diff.set(0, 0)
      } else {
        const k = t * t * (3 - 2 * t)
        mouse.coords.copy(mouse.takeoverFrom).lerp(mouse.takeoverTo, k)
      }
    }
    mouse.diff.subVectors(mouse.coords, mouse.coordsOld)
    mouse.coordsOld.copy(mouse.coords)
    if (mouse.coordsOld.x === 0 && mouse.coordsOld.y === 0) mouse.diff.set(0, 0)
    if (mouse.isAutoActive && !mouse.takeoverActive) mouse.diff.multiplyScalar(props.autoIntensity)
  }

  // Idle auto-drive keeps the fluid moving without user input.
  const auto = {
    active: false,
    current: new THREE.Vector2(),
    target: new THREE.Vector2(),
    direction: new THREE.Vector2(),
    lastTime: performance.now(),
    activationTime: 0,
    margin: 0.2,
  }
  function pickTarget() {
    auto.target.set((Math.random() * 2 - 1) * (1 - auto.margin), (Math.random() * 2 - 1) * (1 - auto.margin))
  }
  pickTarget()

  function stopAuto() {
    auto.active = false
    mouse.isAutoActive = false
  }

  function updateAuto() {
    if (!props.autoDemo) return
    const now = performance.now()
    if (now - lastUserInteraction < props.autoResumeDelay || mouse.isHoverInside) {
      if (auto.active) stopAuto()
      return
    }
    if (!auto.active) {
      auto.active = true
      auto.current.copy(mouse.coords)
      auto.lastTime = now
      auto.activationTime = now
    }
    mouse.isAutoActive = true
    mouse.hasUserControl = false
    let dtSec = (now - auto.lastTime) / 1000
    auto.lastTime = now
    if (dtSec > 0.2) dtSec = 0.016
    const direction = auto.direction.subVectors(auto.target, auto.current)
    const distance = direction.length()
    if (distance < 0.01) {
      pickTarget()
      return
    }
    direction.normalize()
    let ramp = 1
    const rampMs = props.autoRampDuration * 1000
    if (rampMs > 0) {
      const t = Math.min(1, (now - auto.activationTime) / rampMs)
      ramp = t * t * (3 - 2 * t)
    }
    auto.current.addScaledVector(direction, Math.min(props.autoSpeed * dtSec * ramp, distance))
    mouse.coords.set(auto.current.x, auto.current.y)
  }

  function step() {
    boundarySpace.copy(props.isBounce ? new THREE.Vector2() : cellScale)

    advection.uniforms.dt!.value = props.dt
    advection.uniforms.isBFECC!.value = props.BFECC
    advection.uniforms.velocity!.value = fbos.vel0.texture
    boundaryLine.visible = props.isBounce
    renderPass(advection.scene, fbos.vel1)

    const forceUniforms = externalForce.uniforms
    const cursorX = props.cursorSize * cellScale.x
    const cursorY = props.cursorSize * cellScale.y
    ;(forceUniforms.force!.value as THREE.Vector2).set(
      (mouse.diff.x / 2) * props.mouseForce,
      (mouse.diff.y / 2) * props.mouseForce,
    )
    ;(forceUniforms.center!.value as THREE.Vector2).set(
      Math.min(Math.max(mouse.coords.x, -1 + cursorX + cellScale.x * 2), 1 - cursorX - cellScale.x * 2),
      Math.min(Math.max(mouse.coords.y, -1 + cursorY + cellScale.y * 2), 1 - cursorY - cellScale.y * 2),
    )
    ;(forceUniforms.scale!.value as THREE.Vector2).set(props.cursorSize, props.cursorSize)
    renderPass(externalForce.scene, fbos.vel1)

    let velocity = fbos.vel1
    if (props.isViscous) {
      viscous.uniforms.v!.value = props.viscous
      viscous.uniforms.dt!.value = props.dt
      let fboOut = fbos.viscous1
      for (let i = 0; i < props.iterationsViscous; i++) {
        const fboIn = i % 2 === 0 ? fbos.viscous0 : fbos.viscous1
        fboOut = i % 2 === 0 ? fbos.viscous1 : fbos.viscous0
        viscous.uniforms.velocity_new!.value = fboIn.texture
        renderPass(viscous.scene, fboOut)
      }
      velocity = fboOut
    }

    divergence.uniforms.velocity!.value = velocity.texture
    renderPass(divergence.scene, fbos.div)

    let pressureOut = fbos.pressure1
    for (let i = 0; i < props.iterationsPoisson; i++) {
      const pressureIn = i % 2 === 0 ? fbos.pressure0 : fbos.pressure1
      pressureOut = i % 2 === 0 ? fbos.pressure1 : fbos.pressure0
      poisson.uniforms.pressure!.value = pressureIn.texture
      renderPass(poisson.scene, pressureOut)
    }

    pressure.uniforms.velocity!.value = velocity.texture
    pressure.uniforms.pressure!.value = pressureOut.texture
    pressure.uniforms.dt!.value = props.dt
    renderPass(pressure.scene, fbos.vel0)
  }

  let frame = 0
  let running = false
  let visible = true

  function loop() {
    if (!running) return
    updateAuto()
    updateMouse()
    step()
    renderPass(output.scene, null)
    frame = requestAnimationFrame(loop)
  }

  function sync() {
    const shouldRun = visible && !document.hidden
    if (shouldRun && !running) {
      running = true
      auto.lastTime = performance.now()
      frame = requestAnimationFrame(loop)
    } else if (!shouldRun && running) {
      running = false
      cancelAnimationFrame(frame)
      frame = 0
    }
  }

  function resize() {
    measure()
    computeFboSize()
    for (const fbo of Object.values(fbos)) fbo.setSize(fboSize.x, fboSize.y)
  }

  let resizeFrame = 0
  const resizeObserver = new ResizeObserver(() => {
    if (resizeFrame) cancelAnimationFrame(resizeFrame)
    resizeFrame = requestAnimationFrame(() => {
      resizeFrame = 0
      resize()
    })
  })
  resizeObserver.observe(container)

  const intersectionObserver = new IntersectionObserver(([entry]) => {
    visible = (entry?.isIntersecting ?? false) && (entry?.intersectionRatio ?? 0) > 0
    sync()
  }, { threshold: [0, 0.01] })
  intersectionObserver.observe(container)

  window.addEventListener('pointermove', onPointerMove, { passive: true })
  document.addEventListener('pointerleave', onPointerLeave)
  document.addEventListener('visibilitychange', sync)
  sync()

  return () => {
    running = false
    cancelAnimationFrame(frame)
    if (resizeFrame) cancelAnimationFrame(resizeFrame)
    resizeObserver.disconnect()
    intersectionObserver.disconnect()
    window.removeEventListener('pointermove', onPointerMove)
    document.removeEventListener('pointerleave', onPointerLeave)
    document.removeEventListener('visibilitychange', sync)
    for (const fbo of Object.values(fbos)) fbo.dispose()
    for (const item of disposables) item.dispose()
    renderer.dispose()
    renderer.forceContextLoss()
    canvas.remove()
  }
}

onMounted(() => {
  const container = root.value
  if (!container) return
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    failed.value = true
    return
  }
  try {
    teardown = start(container)
  } catch {
    // WebGL unavailable: fall back to the static gradient.
    failed.value = true
  }
})

onBeforeUnmount(() => {
  teardown?.()
  teardown = null
})
</script>

<template>
  <div ref="root" class="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden="true">
    <div
      v-if="failed"
      class="absolute inset-0"
      :style="{
        background: `radial-gradient(60% 80% at 20% 30%, ${colors[0]}33, transparent 70%), radial-gradient(50% 70% at 80% 40%, ${colors[colors.length - 1]}26, transparent 70%)`,
      }"
    />
  </div>
</template>
