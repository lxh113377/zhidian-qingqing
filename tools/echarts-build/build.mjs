import { rolldown } from 'rolldown'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { statSync } from 'node:fs'

const here = dirname(fileURLToPath(import.meta.url))
const out = resolve(here, 'out/echarts.custom.js')

try {
  const bundle = await rolldown({ input: resolve(here, 'entry.js'), platform: 'browser' })
  await bundle.write({ file: out, format: 'iife', name: 'echarts', minify: true })
  await bundle.close()
} catch (e) {
  const errs = e?.errors || []
  if (errs.length) {
    for (const x of errs.slice(0, 5)) console.log('BUILD ERROR:', x.code, x.message, x.id || '')
  } else {
    console.log('BUILD ERROR:', e?.message)
  }
  process.exit(1)
}

console.log(`built: ${out}  ${(statSync(out).size / 1024).toFixed(1)} KB`)
