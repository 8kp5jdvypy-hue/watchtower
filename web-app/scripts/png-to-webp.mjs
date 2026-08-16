// PNG -> lossy WebP via Chromium's own canvas encoder -- no native
// encoder (cwebp/ffmpeg/sharp) is installed on this machine, and
// Playwright's bundled Chromium is already here. Downscales to the
// target width in-canvas for a clean 2x -> 1x resample.
// Usage: node scripts/png-to-webp.mjs <in.png> <out.webp> <width> [quality]
import { chromium } from '@playwright/test'
import { readFileSync, writeFileSync } from 'node:fs'

const [inPath, outPath, widthArg, qualityArg] = process.argv.slice(2)
const width = Number(widthArg)
const quality = qualityArg ? Number(qualityArg) : 0.82

const browser = await chromium.launch()
const page = await browser.newPage()
const dataUrl = `data:image/png;base64,${readFileSync(inPath).toString('base64')}`
const webp = await page.evaluate(async ({ dataUrl, width, quality }) => {
  const img = new Image()
  await new Promise((resolve, reject) => { img.onload = resolve; img.onerror = reject; img.src = dataUrl })
  const scale = width / img.width
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = Math.round(img.height * scale)
  const ctx = canvas.getContext('2d')
  ctx.imageSmoothingQuality = 'high'
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
  return canvas.toDataURL('image/webp', quality)
}, { dataUrl, width, quality })
await browser.close()
writeFileSync(outPath, Buffer.from(webp.split(',')[1], 'base64'))
console.log('wrote', outPath)
