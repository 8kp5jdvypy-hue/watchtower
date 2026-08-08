import * as THREE from 'three'

// Procedurally draws a hovering-kestrel silhouette to an offscreen canvas and
// returns it as a THREE.CanvasTexture. No external image/model asset —
// self-contained, and deliberately a silhouette (rim-lit against atmosphere)
// rather than an attempt at photoreal texture, which this project has no
// licensed source imagery for.
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
  ctx.scale(1, 1)

  // soft atmospheric glow behind the bird
  const glow = ctx.createRadialGradient(0, 0, 10, 0, 0, size * 0.42)
  glow.addColorStop(0, 'rgba(180, 220, 255, 0.22)')
  glow.addColorStop(1, 'rgba(180, 220, 255, 0)')
  ctx.fillStyle = glow
  ctx.beginPath()
  ctx.arc(0, 0, size * 0.42, 0, Math.PI * 2)
  ctx.fill()

  ctx.fillStyle = 'rgba(6, 9, 13, 1)'

  // body (compact, angled slightly forward/down, characteristic of a hover)
  ctx.beginPath()
  ctx.moveTo(-18, -70)
  ctx.bezierCurveTo(-30, -40, -34, 10, -14, 60)
  ctx.bezierCurveTo(-4, 82, 10, 84, 18, 62)
  ctx.bezierCurveTo(34, 14, 30, -38, 14, -72)
  ctx.bezierCurveTo(4, -86, -8, -86, -18, -70)
  ctx.closePath()
  ctx.fill()

  // head + hooked beak, bowed down (watching the ground)
  ctx.beginPath()
  ctx.moveTo(-16, -66)
  ctx.bezierCurveTo(-30, -78, -30, -100, -12, -108)
  ctx.bezierCurveTo(6, -114, 22, -104, 22, -88)
  ctx.bezierCurveTo(22, -76, 10, -66, -4, -64)
  ctx.closePath()
  ctx.fill()
  ctx.beginPath()
  ctx.moveTo(18, -96)
  ctx.bezierCurveTo(30, -98, 40, -92, 38, -84)
  ctx.bezierCurveTo(36, -78, 24, -78, 16, -84)
  ctx.closePath()
  ctx.fill()

  // wings: swept up and back, the classic hover silhouette
  const wing = (mirror) => {
    ctx.save()
    ctx.scale(mirror, 1)
    ctx.beginPath()
    ctx.moveTo(14, -34)
    ctx.bezierCurveTo(90, -70, 230, -96, 330, -60)
    ctx.bezierCurveTo(300, -34, 220, -18, 150, -6)
    ctx.bezierCurveTo(230, 10, 260, 30, 260, 48)
    ctx.bezierCurveTo(190, 40, 110, 20, 60, -4)
    ctx.bezierCurveTo(90, 10, 100, 28, 90, 40)
    ctx.bezierCurveTo(55, 20, 24, -4, 12, -20)
    ctx.closePath()
    ctx.fill()
    ctx.restore()
  }
  wing(1)
  wing(-1)

  // fanned tail, angled down (braking, characteristic of a hover)
  ctx.beginPath()
  ctx.moveTo(-10, 56)
  ctx.bezierCurveTo(-40, 120, -34, 190, -14, 230)
  ctx.bezierCurveTo(-4, 200, 2, 160, 2, 130)
  ctx.bezierCurveTo(2, 160, 8, 198, 18, 226)
  ctx.bezierCurveTo(36, 186, 38, 118, 8, 58)
  ctx.closePath()
  ctx.fill()

  ctx.restore()

  const tex = new THREE.CanvasTexture(canvas)
  tex.colorSpace = THREE.SRGBColorSpace
  tex.needsUpdate = true
  return tex
}
