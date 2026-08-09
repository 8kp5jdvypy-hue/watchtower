import * as THREE from 'three'
import { FALCON_PATHS } from '../components/PerchMark'

// The Perch falcon mark, same polygon data as PerchMark.jsx (the nav/
// footer/favicon icon) and MarketField.jsx (the mid-page dive), baked to a
// texture for the hero's WebGL billboard. Deliberately a plain dark
// silhouette here, not a fully bloomed glow -- the brand direction is a
// refined mark with a restrained cyan accent, not a neon centerpiece. No
// external image/model asset; drawn procedurally to an offscreen canvas.
//
// This is the one place that can't share PerchMarkGlyph -- that's SVG
// JSX, this is imperative Canvas 2D feeding a Three.js GPU texture, a
// different rendering path by necessity (the hero mesh needs a texture,
// not a DOM node). When the final mark replaces FALCON_PATHS, the
// drawPolygon() calls below need their own update -- or, better, a
// follow-up refactor to draw a loaded SVG/image instead of hand-drawn
// path commands. See BRAND.md §6.
function drawPolygon(ctx, pointStr) {
  const pts = pointStr.split(' ').map((p) => p.split(',').map(Number))
  ctx.beginPath()
  ctx.moveTo(pts[0][0], pts[0][1])
  for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1])
  ctx.closePath()
  ctx.fill()
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
  ctx.scale(1.9, 1.9)

  // faint atmospheric glow -- subtle, not the mark's primary light source
  const glow = ctx.createRadialGradient(0, 0, 10, 0, 0, size * 0.24)
  glow.addColorStop(0, 'rgba(180, 220, 255, 0.14)')
  glow.addColorStop(1, 'rgba(180, 220, 255, 0)')
  ctx.fillStyle = glow
  ctx.beginPath()
  ctx.arc(0, 0, size * 0.24, 0, Math.PI * 2)
  ctx.fill()

  ctx.fillStyle = 'rgba(6, 9, 13, 0.88)'
  drawPolygon(ctx, FALCON_PATHS.farWing)

  ctx.fillStyle = 'rgba(6, 9, 13, 1)'
  drawPolygon(ctx, FALCON_PATHS.nearWing)
  drawPolygon(ctx, FALCON_PATHS.body)
  drawPolygon(ctx, FALCON_PATHS.tail)

  ctx.restore()

  const tex = new THREE.CanvasTexture(canvas)
  tex.colorSpace = THREE.SRGBColorSpace
  tex.needsUpdate = true
  return tex
}
