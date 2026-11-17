// Run after `vite build` and `vite build --ssr`: injects each page's
// prerendered markup into dist/<page>.html so crawlers see real copy.
import { readFileSync, writeFileSync, rmSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const ssrDir = path.join(root, 'dist-ssr')
const mod = await import(pathToFileURL(path.join(ssrDir, 'entry-server.js')).href)
const EMPTY = '<div id="root"></div>'

for (const [file, render] of [['index.html', mod.renderHome], ['record.html', mod.renderRecord]]) {
  const p = path.join(root, 'dist', file)
  const html = readFileSync(p, 'utf8')
  if (!html.includes(EMPTY)) throw new Error(`${file}: empty root not found`)
  const out = html.replace(EMPTY, `<div id="root">${render()}</div>`)
  writeFileSync(p, out)
  console.log(`prerendered ${file} (${out.length} bytes)`)
}
rmSync(ssrDir, { recursive: true, force: true })
