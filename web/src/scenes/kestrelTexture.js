import * as THREE from 'three'
import { KESTREL } from '../components/PerchMark'

// The Perch kestrel mark, baked to a texture for the hero's WebGL
// billboard -- same path data as PerchMark.jsx (nav/footer/favicon) and
// MarketField.jsx (the mid-page dive), so every instance of the bird on
// the site is the same drawing. Line-art: thin cyan strokes, only a very
// subtle fill, no external image/model asset.
function drawSvgPath(ctx, d) {
  const tokens = d.match(/[MLCQZ]|-?\d*\.?\d+/g)
  let i = 0
  const num = () => parseFloat(tokens[i++])
  while (i < tokens.length) {
    const cmd = tokens[i++]
    if (cmd === 'M') ctx.moveTo(num(), num())
    else if (cmd === 'L') ctx.lineTo(num(), num())
    else if (cmd === 'C') ctx.bezierCurveTo(num(), num(), num(), num(), num(), num())
    else if (cmd === 'Q') ctx.quadraticCurveTo(num(), num(), num(), num())
    else if (cmd === 'Z') ctx.closePath()
  }
}

export function makeKestrelTexture() {
  const size = 1024
  const canvas = document.createElement('canvas')
  canvas.width = size
  canvas.height = size
  const ctx = canvas.getContext('2d')
  ctx.clearRect(0, 0, size, size)

  const cx = size * 0.5
  const cy = size * 0.52

  ctx.save()
  ctx.translate(cx, cy)
  ctx.scale(1.7, 1.7)
  ctx.translate(-153, -136) // recenter the mark's own coordinate space

  // faint atmospheric glow behind the mark
  const glow = ctx.createRadialGradient(0, 0, 10, 0, 0, size * 0.24)
  glow.addColorStop(0, 'rgba(52, 226, 255, 0.1)')
  glow.addColorStop(1, 'rgba(52, 226, 255, 0)')
  ctx.fillStyle = glow
  ctx.beginPath()
  ctx.arc(153, 136, size * 0.24, 0, Math.PI * 2)
  ctx.fill()

  ctx.lineWidth = 3.2
  ctx.lineCap = 'round'
  ctx.lineJoin = 'round'
  ctx.strokeStyle = 'rgba(52, 226, 255, 0.95)'
  ctx.fillStyle = 'rgba(52, 226, 255, 0.06)'

  // perch line
  ctx.beginPath(); drawSvgPath(ctx, KESTREL.perch); ctx.stroke()
  // tail, legs, talons
  for (const d of [...KESTREL.tail, ...KESTREL.legs, ...KESTREL.talons]) {
    ctx.beginPath(); drawSvgPath(ctx, d); ctx.stroke()
  }
  // body (subtle fill + stroke)
  ctx.beginPath(); drawSvgPath(ctx, KESTREL.body); ctx.fill(); ctx.stroke()
  // wing (stroke only)
  ctx.beginPath(); drawSvgPath(ctx, KESTREL.wing); ctx.stroke()
  // wing feather lines
  for (const d of KESTREL.feathers) {
    ctx.beginPath(); drawSvgPath(ctx, d); ctx.stroke()
  }
  // skull + beak
  ctx.beginPath(); ctx.arc(KESTREL.skull.cx, KESTREL.skull.cy, KESTREL.skull.r, 0, Math.PI * 2); ctx.fill(); ctx.stroke()
  ctx.beginPath(); drawSvgPath(ctx, KESTREL.beak); ctx.fill(); ctx.stroke()
  // eye + nostril
  ctx.fillStyle = 'rgba(52, 226, 255, 1)'
  ctx.beginPath(); ctx.arc(KESTREL.eye.cx, KESTREL.eye.cy, KESTREL.eye.r, 0, Math.PI * 2); ctx.fill()
  ctx.beginPath(); ctx.arc(KESTREL.nostril.cx, KESTREL.nostril.cy, KESTREL.nostril.r, 0, Math.PI * 2); ctx.fill()

  ctx.restore()

  const tex = new THREE.CanvasTexture(canvas)
  tex.colorSpace = THREE.SRGBColorSpace
  tex.needsUpdate = true
  return tex
}
