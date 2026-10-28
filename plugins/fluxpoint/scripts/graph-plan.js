// Local dry runner for compile-graph.py --plan. No model or Workflow API exists.
const fs = require('fs')
const input = JSON.parse(fs.readFileSync(0, 'utf8'))
const calls = []
const baseline = input.args._base
function schemaValue(schema) {
  if (schema.enum) return schema.enum[0]
  if (schema.type === 'array') return []
  if (schema.type === 'integer' || schema.type === 'number') return 0
  if (schema.type === 'boolean') return false
  if (schema.type === 'object' || schema.properties) return Object.fromEntries(Object.entries(schema.properties || {}).filter(([k]) => (schema.required || []).includes(k)).map(([k,v]) => [k, schemaValue(v)]))
  return ''
}
async function agent(prompt, opts) {
  calls.push({ prompt, opts })
  if (opts.label.startsWith('tree-check:')) {
    const reading = { root: baseline.root, head: baseline.sha, porcelain: baseline.porcelain }
    const m = /git -C ("(?:[^"\\]|\\.)*") ls-remote origin ("(?:[^"\\]|\\.)*")/.exec(prompt)
    if (m) {
      reading.repository = JSON.parse(m[1]); reading.branch = JSON.parse(m[2]).replace(/^refs\/heads\//, '')
      const known = input.readings.find(r => r.repository === reading.repository && r.branch === reading.branch)
      reading.tip = known ? known.tip : '0'.repeat(40)
    }
    return reading
  }
  const id = Object.keys(input.results).sort((a,b) => b.length-a.length).find(id => opts.label === id || opts.label.startsWith(id + ':'))
  let result = id === undefined ? undefined : input.results[id]
  if (Array.isArray(result) && /:\d+$/.test(opts.label)) result = result[Number(opts.label.split(':').pop())]
  return result === undefined ? schemaValue(opts.schema || {}) : result
}
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor
const source = input.source.replace(/^export const meta =/m, 'const meta =')
const fn = new AsyncFunction('args','agent','budget','parallel','phase','log', source)
fn(input.args, agent, { total: 0, remaining: () => Number.MAX_SAFE_INTEGER, spent: () => null }, tasks => Promise.all(tasks.map(f => f())), () => {}, () => {})
  .then(summary => console.log(JSON.stringify({ summary, calls })))
  .catch(error => { console.error(error.stack || String(error)); process.exitCode = 1 })
