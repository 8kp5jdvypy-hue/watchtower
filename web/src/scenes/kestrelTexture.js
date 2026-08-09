import * as THREE from 'three'

// "The Windhover," redesigned again -- the previous version's wings
// attached right next to the head and both swept the same direction,
// which read as ears on a rabbit rather than wings on a bird. Wings now
// attach at the shoulder, well below a small distinct head, and sweep
// outward and back (not upward) -- unambiguous as a wing-on-a-body
// silhouette. Same silhouette family as PerchMark.jsx (nav/footer logo,
// reverted to its original pre-redesign glyph) and MarketField.jsx (the
// mid-page dive moment, wings tucked instead of spread). Procedurally
// drawn to an offscreen canvas, no external image/model asset.
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
  ctx.scale(1.3, 1.3)

  // soft atmospheric glow behind the bird
  const glow = ctx.createRadialGradient(0, 0, 10, 0, 0, size * 0.3)
  glow.addColorStop(0, 'rgba(180, 220, 255, 0.2)')
  glow.addColorStop(1, 'rgba(180, 220, 255, 0)')
  ctx.fillStyle = glow
  ctx.beginPath()
  ctx.arc(0, 0, size * 0.3, 0, Math.PI * 2)
  ctx.fill()

  // far wing -- smaller, tucked behind, attached at the shoulder
  ctx.fillStyle = 'rgba(6, 9, 13, 0.88)'
  ctx.beginPath()
  ctx.moveTo(20, -25)
  ctx.bezierCurveTo(40, -34, 62, -38, 78, -30)
  ctx.bezierCurveTo(76, -22, 62, -16, 46, -14)
  ctx.bezierCurveTo(54, -6, 52, 4, 44, 8)
  ctx.bezierCurveTo(30, 0, 20, -12, 16, -22)
  ctx.closePath()
  ctx.fill()

  ctx.fillStyle = 'rgba(6, 9, 13, 1)'

  // near wing -- attached at the shoulder (well below the head), swept
  // outward and back, not upward -- the fix for the "bunny ears" read
  ctx.beginPath()
  ctx.moveTo(26, -18)
  ctx.bezierCurveTo(60, -30, 100, -35, 130, -20)
  ctx.bezierCurveTo(128, -8, 110, 2, 85, 4)
  ctx.bezierCurveTo(100, 14, 108, 28, 100, 38)
  ctx.bezierCurveTo(75, 30, 50, 12, 32, -6)
  ctx.bezierCurveTo(30, -10, 28, -14, 26, -18)
  ctx.closePath()
  ctx.fill()

  // body + head + hooked beak, one continuous silhouette
  ctx.beginPath()
  ctx.moveTo(-5, -70)
  ctx.bezierCurveTo(-15, -85, -8, -100, 8, -102)
  ctx.bezierCurveTo(20, -104, 32, -98, 30, -88)
  ctx.bezierCurveTo(40, -92, 48, -88, 44, -80)
  ctx.bezierCurveTo(36, -78, 28, -76, 22, -70)
  ctx.bezierCurveTo(30, -55, 34, -35, 28, -15)
  ctx.bezierCurveTo(24, 15, 16, 45, 4, 68)
  ctx.bezierCurveTo(-2, 80, -10, 82, -16, 74)
  ctx.bezierCurveTo(-22, 60, -22, 40, -20, 15)
  ctx.bezierCurveTo(-20, -20, -18, -50, -5, -70)
  ctx.closePath()
  ctx.fill()

  // tail -- small fork at the back
  ctx.beginPath()
  ctx.moveTo(-6, 62)
  ctx.bezierCurveTo(-14, 82, -16, 98, -8, 110)
  ctx.bezierCurveTo(0, 100, 4, 86, 2, 74)
  ctx.bezierCurveTo(6, 86, 12, 98, 20, 106)
  ctx.bezierCurveTo(26, 92, 22, 76, 10, 60)
  ctx.bezierCurveTo(4, 64, 0, 64, -6, 62)
  ctx.closePath()
  ctx.fill()

  ctx.restore()

  const tex = new THREE.CanvasTexture(canvas)
  tex.colorSpace = THREE.SRGBColorSpace
  tex.needsUpdate = true
  return tex
}
