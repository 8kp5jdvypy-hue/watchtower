import * as THREE from 'three'

// "The Windhover" -- redesigned (see PerchMark.jsx and MarketField.jsx for
// the same silhouette family used consistently across the hero, the
// mid-page dive moment, and the nav/footer logo). A kestrel mid-hover:
// body pitched nose-down, one dominant sickle-shaped near wing swept up
// and back, a smaller foreshortened far wing hinting depth, tail fanned
// as an air brake. One continuous body+head+beak path (the previous
// version's separately-drawn, self-overlapping head and mirrored wings
// were what made it read as broken: a front-view wing spread on a
// profile-view head). Procedurally drawn to an offscreen canvas, no
// external image/model asset.
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
  ctx.scale(1.4, 1.4)

  // soft atmospheric glow behind the bird
  const glow = ctx.createRadialGradient(0, 0, 10, 0, 0, size * 0.3)
  glow.addColorStop(0, 'rgba(180, 220, 255, 0.2)')
  glow.addColorStop(1, 'rgba(180, 220, 255, 0)')
  ctx.fillStyle = glow
  ctx.beginPath()
  ctx.arc(0, 0, size * 0.3, 0, Math.PI * 2)
  ctx.fill()

  // far wing -- smaller, foreshortened, tucked behind the body
  ctx.fillStyle = 'rgba(6, 9, 13, 0.88)'
  ctx.beginPath()
  ctx.moveTo(-22, -44)
  ctx.bezierCurveTo(-30, -64, -28, -86, -14, -100)
  ctx.bezierCurveTo(-2, -90, 0, -72, -8, -54)
  ctx.bezierCurveTo(-12, -48, -18, -44, -22, -44)
  ctx.closePath()
  ctx.fill()

  ctx.fillStyle = 'rgba(6, 9, 13, 1)'

  // body + head + hooked beak -- one continuous silhouette, no seams
  ctx.beginPath()
  ctx.moveTo(-44, -12)
  ctx.bezierCurveTo(-50, -34, -44, -56, -26, -68)
  ctx.bezierCurveTo(-30, -82, -24, -98, -8, -104)
  ctx.bezierCurveTo(6, -109, 20, -98, 16, -78)
  ctx.bezierCurveTo(8, -62, -6, -56, -14, -34)
  ctx.bezierCurveTo(-8, -16, -2, 2, -2, 20)
  ctx.bezierCurveTo(-2, 36, -10, 50, -24, 54)
  ctx.bezierCurveTo(-36, 57, -46, 50, -50, 38)
  ctx.bezierCurveTo(-58, 48, -70, 56, -84, 58)
  ctx.bezierCurveTo(-74, 42, -62, 20, -52, 2)
  ctx.bezierCurveTo(-50, -4, -47, -8, -44, -12)
  ctx.closePath()
  ctx.fill()

  // near wing -- one clean sickle, sharp single tip, swept up and back
  ctx.beginPath()
  ctx.moveTo(2, -60)
  ctx.bezierCurveTo(30, -70, 60, -78, 90, -80)
  ctx.quadraticCurveTo(106, -76, 98, -64)
  ctx.bezierCurveTo(68, -50, 36, -44, 8, -48)
  ctx.bezierCurveTo(18, -32, 22, -16, 16, -2)
  ctx.bezierCurveTo(-4, -16, -14, -40, 2, -60)
  ctx.closePath()
  ctx.fill()

  // tail -- forked, angled down as an air brake
  ctx.beginPath()
  ctx.moveTo(-50, 38)
  ctx.bezierCurveTo(-62, 68, -66, 94, -56, 112)
  ctx.bezierCurveTo(-46, 98, -42, 78, -44, 60)
  ctx.bezierCurveTo(-40, 78, -32, 96, -22, 106)
  ctx.bezierCurveTo(-14, 88, -18, 64, -34, 42)
  ctx.bezierCurveTo(-40, 46, -46, 44, -50, 38)
  ctx.closePath()
  ctx.fill()

  ctx.restore()

  const tex = new THREE.CanvasTexture(canvas)
  tex.colorSpace = THREE.SRGBColorSpace
  tex.needsUpdate = true
  return tex
}
