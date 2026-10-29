// Inlined only for graphs opting into lanes, input replay or rejudging.
// SHA-256 works on UTF-8 bytes, with no crypto imports or runtime extensions.
function canonical(value) {
  if (value === null || typeof value !== 'object') {
    if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('non-finite replay input')
    const out = JSON.stringify(value)
    if (out === undefined) throw new Error('non-JSON replay input')
    return out
  }
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']'
  return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}'
}
function sha256(text) {
  text = String(text).replace(/[\uD800-\uDFFF]/g, (ch, i, source) => {
    const code = ch.charCodeAt(0), before = source.charCodeAt(i - 1), after = source.charCodeAt(i + 1)
    return code <= 0xDBFF && after >= 0xDC00 && after <= 0xDFFF || code >= 0xDC00 && before >= 0xD800 && before <= 0xDBFF ? ch : '\uFFFD'
  })
  const bytes = encodeURIComponent(text).replace(/%([0-9A-F]{2})/g, (_, value) => String.fromCharCode(parseInt(value, 16))), words = [], len = bytes.length
  for (let i = 0; i < len; i++) words[i >> 2] = (words[i >> 2] || 0) | bytes.charCodeAt(i) << (24 - (i % 4) * 8)
  words[len >> 2] = (words[len >> 2] || 0) | 0x80 << (24 - (len % 4) * 8)
  words[((len + 8) >> 6) * 16 + 15] = len * 8
  words[((len + 8) >> 6) * 16 + 14] = Math.floor(len / 0x20000000)
  const K = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]
  const h = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]
  const r = (v, n) => (v >>> n) | (v << (32 - n))
  for (let block = 0; block < words.length; block += 16) {
    const w = Array(64)
    for (let i = 0; i < 64; i++) {
      if (i < 16) w[i] = words[block + i] | 0
      else { const x=w[i-15], y=w[i-2]; w[i] = (w[i-16] + (r(x,7)^r(x,18)^(x>>>3)) + w[i-7] + (r(y,17)^r(y,19)^(y>>>10))) | 0 }
    }
    let [a,b,c,d,e,f,g,j] = h
    for (let i = 0; i < 64; i++) {
      const t1 = (j + (r(e,6)^r(e,11)^r(e,25)) + ((e&f)^(~e&g)) + K[i] + w[i]) | 0
      const t2 = ((r(a,2)^r(a,13)^r(a,22)) + ((a&b)^(a&c)^(b&c))) | 0
      j=g; g=f; f=e; e=(d+t1)|0; d=c; c=b; b=a; a=(t1+t2)|0
    }
    ;[a,b,c,d,e,f,g,j].forEach((v,i) => { h[i] = (h[i] + v) | 0 })
  }
  return h.map(v => (v >>> 0).toString(16).padStart(8,'0')).join('')
}
const CALL_RECORDS = [], MEMO_RECORDS = [], REJUDGES = []
const LEDGER_REPLAYS = []
const CAPTURE = A._capture || null
if (CAPTURE && (CAPTURE.version !== 1 || canonical(CAPTURE.launch) !== canonical(A._launch))) throw new Error('captured prefix protocol or launch identity mismatch')
function capturedResult(entry) {
  const encoded = CAPTURE && CAPTURE.results && CAPTURE.results[entry.resultRef]
  if (typeof encoded !== 'string' || sha256(encoded) !== entry.resultRef) throw new Error('captured result missing or digest mismatched')
  const value = JSON.parse(encoded)
  if (sha256(canonical(value)) !== entry.resultSha256) throw new Error('captured result semantic digest mismatched')
  return value
}
function replaySource(entry) {
  return { runId: entry.runId, artifactSha256: entry.artifactSha256, agentId: entry.agentId,
    ...(entry.callIndex === undefined ? {} : { callIndex: entry.callIndex }) }
}
const PUSH_CHECKS = []
const NODE_STATE = {}, LIVE_READINGS = {}, LIVE_READING_CALLS = {}, LIVE_READING_FRESH = {}, CALL_STATE = {}
const REJUDGE_REACHED = new Set()
let HALT = null, FORKED = false, RESERVED = 0, PREFIX_OPEN = true, PREFIX_INDEX = 0
function halt(outcome) { if (!HALT) HALT = { outcome }; return null }
function replayAncestorKeys(n) { return n.after.map(id => NODE_STATE[id] && NODE_STATE[id].key || sha256(canonical({ missing: id }))) }
function allAncestorsReplayed(n) { return n.after.every(id => NODE_STATE[id] && NODE_STATE[id].replayed) }
function rejudgeText(id, includeNote = true) {
  const row = (A.rejudge || {})[id]
  return row ? '\nREJUDGE NONCE: ' + row.nonce + '\nREJUDGE LAUNCH NONCE: ' + A._replayLaunchNonce + (!includeNote || row.note === undefined ? '' : '\nOPERATOR REJUDGE NOTE (verbatim):\n' + row.note) : ''
}
function promptArg(path) {
  // Reasons are attribution metadata, never part of a gate's reasoning input.
  // Internal launcher state also never reaches a whole-arguments token.
  let value = Object.fromEntries(Object.entries(A).filter(([k]) => !k.startsWith('_') && k !== 'rejudge'))
  for (const key of path) value = value === null || value === undefined ? undefined : value[key]
  return value
}
function validateRejudges() {
  if (A.rejudge === undefined) return
  if (!A._resumeFromRunId) throw new Error('rejudge requires an explicitly resumed source run')
  if (!A.rejudge || typeof A.rejudge !== 'object' || Array.isArray(A.rejudge)) throw new Error('rejudge must be a gate map')
  for (const [id, row] of Object.entries(A.rejudge)) {
    if (!REPLAY_NODES[id] || !REPLAY_NODES[id].gate) throw new Error('rejudge names a non-gate: ' + id)
    if (!row || typeof row !== 'object' || Array.isArray(row) || Object.keys(row).some(k => !['nonce','reason','note'].includes(k)) || typeof row.nonce !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(row.nonce) || typeof row.reason !== 'string' || !row.reason.trim() || row.note !== undefined && typeof row.note !== 'string') throw new Error('invalid rejudge entry: ' + id)
  }
}
validateRejudges()
const runtimeAgent = agent
function auditedPrompt(prompt, options) {
  const header = 'Fluxpoint-Call-Opts-SHA256: ' + sha256(canonical(options)) + '\n'
  return prompt.startsWith(header) ? prompt : header + prompt
}
function prefixTreePrompt(prompt, options) {
  const previous = (A._prefix || [])[PREFIX_INDEX]
  if (!CAPTURE || FORKED || !PREFIX_OPEN || !previous || !previous.readingNonce) return prompt
  const candidate = prompt.replace('\nLIVE TREE READING NONCE: ' + A._replayLaunchNonce + ' point ', '\nLIVE TREE READING NONCE: ' + previous.readingNonce + ' point ')
  return previous.promptSha256 === sha256(auditedPrompt(candidate, options)) && previous.optsSha256 === sha256(canonical(options)) ? candidate : prompt
}
function parseRemoteHeads(text) {
  if (typeof text !== 'string') throw new Error('remote heads output must be a string')
  const heads = {}
  for (const line of text.split(/\r?\n/).filter(x => x.trim())) {
    const match = /^([0-9a-fA-F]{40}|[0-9a-fA-F]{64})\s+(refs\/heads\/[^\s]+)$/.exec(line)
    if (!match || Object.prototype.hasOwnProperty.call(heads, match[2])) throw new Error('invalid or duplicate remote head')
    heads[match[2]] = match[1].toLowerCase()
  }
  return heads
}
async function pushSnapshot(n, options, side, forceLive = false) {
  const point = side + ' ' + options.label
  const opts = { label: 'push-check:' + point, phase: options.phase, effort: 'low', schema: {
    type: 'object', additionalProperties: false, required: ['repository','heads'],
    properties: { repository: { type: 'string' }, heads: { type: 'string' } }
  } }
  const base = longPreamble() + 'Run exactly: git -C ' + JSON.stringify(n.target.repository) + ' ls-remote --heads origin. Return stdout verbatim as heads and the exact declared repository as repository. Empty stdout is valid only when the command succeeds and the remote has no heads. On any command failure return null. Do not push, fetch, fix, or explain anything.'
  let prompt = base + '\nLIVE REF SNAPSHOT NONCE: ' + A._replayLaunchNonce + ' point ' + point
  const previous = (A._prefix || [])[PREFIX_INDEX]
  if (!forceLive && !FORKED && PREFIX_OPEN && previous && previous.readingNonce) {
    const candidate = base + '\nLIVE REF SNAPSHOT NONCE: ' + previous.readingNonce + ' point ' + point
    if (prefixMatches(auditedPrompt(candidate, opts), opts)) prompt = candidate
  }
  const value = await agent(prompt, opts)
  const call = CALL_STATE[opts.label]
  try {
    if (!value || value.repository !== n.target.repository) throw new Error('remote snapshot missing or repository mismatched')
    return { heads: parseRemoteHeads(value.heads), callIndex: CALL_RECORDS.indexOf(call), mode: call.mode }
  } catch (error) {
    note(n.id, 'PUSH-UNKNOWN', point + ': ' + error.message); halt('PUSH-UNKNOWN'); return null
  }
}
async function pushPrefix(n, options) {
  const previous = (A._prefix || [])[PREFIX_INDEX]
  return n && n.mutates && n.target && !FORKED && PREFIX_OPEN && previous && previous.label === 'push-check:before ' + options.label
    ? pushSnapshot(n, options, 'before') : null
}
async function guardedInvoke(n, prompt, options, invoke, before = null) {
  if (!n || !n.mutates || !n.target) return invoke(prompt, options)
  const nativePrefix = prefixMatches(auditedPrompt(prompt, options), options)
  if (!before || before.mode === 'prefix' && !nativePrefix) before = await pushSnapshot(n, options, 'before', true)
  if (!before) return null
  let result, failure
  try { result = await invoke(prompt, options) } catch (error) { failure = error }
  const call = CALL_STATE[options.label]
  const after = await pushSnapshot(n, options, 'after', !(call && call.mode === 'prefix'))
  if (!after) return null
  const changedRefs = [...new Set([...Object.keys(before.heads), ...Object.keys(after.heads)])].filter(ref => before.heads[ref] !== after.heads[ref]).sort()
  const declared = 'refs/heads/' + n.target.branch
  const unexpected = changedRefs.filter(ref => ref !== declared)
  PUSH_CHECKS.push({ node: n.id, label: options.label, target: n.target, beforeCallIndex: before.callIndex,
    callIndex: CALL_RECORDS.indexOf(call), afterCallIndex: after.callIndex, changedRefs,
    declaredMoved: changedRefs.includes(declared) })
  if (!changedRefs.includes(declared)) note(n.id, 'PUSH-NO-CHANGE', 'declared remote ref did not move: ' + declared)
  if (unexpected.length) {
    note(n.id, 'PUSH-REF-MISMATCH', 'undeclared remote refs changed during builder interval: ' + unexpected.join(', '))
    halt('PUSH-REF-MISMATCH'); return null
  }
  if (failure) throw failure
  return result
}
function prefixMatches(prompt, options) {
  const previous = (A._prefix || [])[PREFIX_INDEX]
  return !!CAPTURE && !FORKED && PREFIX_OPEN && previous && previous.promptSha256 === sha256(prompt) && previous.optsSha256 === sha256(canonical(options))
}
agent = async function(prompt, options) {
  if (HALT) return null
  prompt = auditedPrompt(prompt, options)
  const previous = (A._prefix || [])[PREFIX_INDEX++]
  const prefix = !!CAPTURE && !FORKED && PREFIX_OPEN && previous && previous.promptSha256 === sha256(prompt) && previous.optsSha256 === sha256(canonical(options))
  if (!prefix) PREFIX_OPEN = false
  const rec = { prompt, opts: options, result: null, mode: prefix ? 'prefix' : 'live' }
  if (prefix) rec.source = replaySource(previous)
  CALL_RECORDS.push(rec)
  const result = prefix && CAPTURE ? capturedResult(previous) : await runtimeAgent(prompt, options)
  if (prefix && CAPTURE) rec.execution = 'captured'
  rec.result = result === undefined ? null : result
  if (prefix && previous.resultSha256 !== sha256(canonical(rec.result))) {
    rec.mode = 'live'; delete rec.source; PREFIX_OPEN = false
  }
  CALL_STATE[options.label] = rec
  return result
}
function ledgerReplay(id, originalPrompt, options, result, ledgerKey) {
  const prompt = auditedPrompt(originalPrompt, options), n = REPLAY_NODES[id]
  const previous = (A._prefix || [])[PREFIX_INDEX]
  const matches = !!CAPTURE && prefixMatches(prompt, options) && previous.resultSha256 === sha256(canonical(result))
  const key = campaign + '|' + id + '|sha256:' + sha256(originalPrompt)
  const effect = (A._ledgerCalls || {})[key]
  const verified = matches || !!(CAPTURE && effect && effect.node === id && effect.promptSha256 === sha256(prompt)
    && effect.optsSha256 === sha256(canonical(options)) && effect.resultSha256 === sha256(canonical(result)))
  if (matches) { capturedResult(previous); PREFIX_INDEX++ }
  else PREFIX_OPEN = false
  const source = matches ? replaySource(previous) : verified ? effect.source : null
  result = matches ? capturedResult(previous) : verified && effect.resultRef ? capturedResult(effect) : result
  const call = { prompt, opts: options, result, mode: matches ? 'prefix' : 'ledger', execution: 'ledger', ledgerKey,
    ...(source ? { source } : {}) }
  CALL_RECORDS.push(call); CALL_STATE[options.label] = call
  const payload = { campaign, compiler: REPLAY_COMPILER, launch: A._launch, id, prompt, opts: options, tree: null, ancestors: replayAncestorKeys(n) }
  const memoKey = sha256(canonical(payload))
  NODE_STATE[id] = { key: memoKey, replayed: matches }
  MEMO_RECORDS.push({ node: id, key: memoKey, payload, prompt, result, mode: 'ledger', callIndex: CALL_RECORDS.indexOf(call),
    irreversible: true, replay: null, fresh: n.fresh, contract: n.contract, ledgerKey, ...(source ? { source } : {}) })
  LEDGER_REPLAYS.push({ node: id, key: ledgerKey, callIndex: CALL_RECORDS.indexOf(call), verified, source })
  if (!verified) note('ledger', 'LEGACY-LEDGER', id + ': once-only result retained without captured native-call proof; downstream calls run live')
  return result
}
function readingValue(reading) {
  return reading ? { root: reading.root, head: reading.head, porcelain: reading.porcelain,
    repository: reading.repository, branch: reading.branch, tip: reading.tip } : null
}
function sameReading(a, b) { return !!a && !!b && canonical(readingValue(a)) === canonical(readingValue(b)) }
function annotateBuilder(n, reading) {
  if (!n.verifies || !reading || !n.target || !reading.branch) return
  const record = MEMO_RECORDS.find(x => x.node === n.verifies)
  if (record && record.mode !== 'memo' && record.verifiedBy.includes(n.id)) {
    record.verifiedReading = readingValue(reading)
    record.verifiedByNode = n.id
    record.verifiedReadingCallIndex = LIVE_READING_CALLS[n.id]
  }
}
function nodePrompt(prompt, options, n, reading) {
  if (n.gate && reading && n.target) {
    prompt += '\nPINNED GATE TREE: repository ' + JSON.stringify(reading.repository) + ', branch ' + JSON.stringify(reading.branch) + ', commit ' + reading.tip + '. Fetch and check out exactly this commit before executing checks. Return sha equal to this commit; a different or missing sha halts the run.'
    options = { ...options, schema: { ...options.schema, properties: { ...options.schema.properties, sha: { type: 'string', pattern: '^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$' } }, required: [...new Set([...(options.schema.required || []), 'sha'])] } }
  }
  prompt += rejudgeText(n.id)
  if (n.fresh) prompt += '\nFRESH NODE LAUNCH NONCE: ' + A._replayLaunchNonce
  prompt = auditedPrompt(prompt, options)
  return { prompt, options }
}
async function replaySpawn(prompt, options, invoke) {
  if (HALT) return null
  const n = REPLAY_NODES[options.label]
  if (!n) {
    const owner = Object.values(REPLAY_NODES).find(node => String(options.label).startsWith(node.id + ':'))
    if (owner && owner.fresh) prompt += '\nFRESH NODE LAUNCH NONCE: ' + A._replayLaunchNonce
    const before = await pushPrefix(owner, options)
    return reservedSpawn(prompt, options, (p, o) => guardedInvoke(owner, p, o, invoke, before))
  }
  // Fixed-command preflight evidence belongs to this native invocation.
  // A captured result has no current worker transcript to bind its commands.
  const fixedReading = n.contract === 'NativeReadingsV1'
  if (fixedReading) PREFIX_OPEN = false
  let reading = LIVE_READINGS[n.id] || null
  const rejudge = (A.rejudge || {})[n.id]
  let rejudgeRecord = null
  if (rejudge) {
    REJUDGE_REACHED.add(n.id)
    rejudgeRecord = { gate: n.id, nonce: rejudge.nonce, reason: rejudge.reason, readingCallIndex: LIVE_READING_CALLS[n.id],
      ...(rejudge.note === undefined ? {} : { note: rejudge.note }), tree: readingValue(reading),
      priorVerdict: (A._priorResults || {})[n.id] === undefined ? null : A._priorResults[n.id], sourceRunId: A._resumeFromRunId || null, sourceArtifactSha256: A._resumeArtifactSha256 || null }
    REJUDGES.push(rejudgeRecord)
  }
  const originalPrompt = prompt, originalOptions = options
  ;({ prompt, options } = nodePrompt(originalPrompt, originalOptions, n, reading))
  const pushBefore = await pushPrefix(n, options)
  if (n.gate && n.target && !LIVE_READING_FRESH[n.id] && !prefixMatches(prompt, options)) {
    if (!await treeCheck('before ' + n.id, options.phase, n.target, true)) { halt(TREE.unknown ? 'TREE-UNKNOWN' : 'TREE-MOVED'); return null }
    reading = LIVE_READINGS[n.id]
    ;({ prompt, options } = nodePrompt(originalPrompt, originalOptions, n, reading))
  }
  const payload = { campaign, compiler: REPLAY_COMPILER, launch: A._launch,
    id: n.id, prompt, opts: options, tree: n.gate ? readingValue(reading) : null, ancestors: replayAncestorKeys(n) }
  const key = sha256(canonical(payload))
  const entry = (A._memo || {})[key]
  const prefixEntry = (A._prefix || [])[PREFIX_INDEX]
  const nativePrefix = prefixMatches(prompt, options)
  let eligible = !fixedReading && !nativePrefix && n.replay && !n.fresh && !n.irreversible && !n.redTeam && !rejudge && allAncestorsReplayed(n)
  if (eligible && n.mutates) {
    eligible = n.verifiedBy.length > 0 && entry && entry.verifiedReading && n.target
    if (eligible) {
      if (!await treeCheck('replay ' + n.id, options.phase, n.target, true)) { halt(TREE.unknown ? 'TREE-UNKNOWN' : 'TREE-MOVED'); return null }
      reading = LIVE_READINGS[n.id]
      eligible = sameReading(reading, entry.sourceFinalReading || entry.verifiedReading)
    }
  }
  if (eligible && entry && (entry.promptSha256 || sha256(entry.prompt)) === sha256(prompt) && (entry.payloadSha256 || sha256(canonical(entry.payload))) === key && (entry.resultSha256 || entry.result !== null && entry.result !== undefined)) {
    const cached = CAPTURE && entry.resultSha256 ? capturedResult(entry) : entry.result
    NODE_STATE[n.id] = { key, replayed: true }
    MEMO_RECORDS.push({ node: n.id, key, payload, prompt, result: cached, mode: 'memo', source: { runId: entry.runId, artifactSha256: entry.artifactSha256, agentId: entry.agentId }, verifies: n.verifies, verifiedBy: n.verifiedBy, reading: readingValue(reading), readingCallIndex: LIVE_READING_CALLS[n.id], replay: n.replay, fresh: n.fresh, irreversible: n.irreversible, contract: n.contract, ...(n.mutates ? { verifiedReading: entry.verifiedReading, verifiedByNode: entry.verifiedByNode, sourceFinalReading: entry.sourceFinalReading || entry.verifiedReading } : {}) })
    note(n.id, 'REPLAYED-MEMO', entry.runId + '/' + entry.agentId, { key, head: entry.verifiedReading ? entry.verifiedReading.tip : reading && reading.tip })
    return cached
  }
  const result = await reservedSpawn(prompt, options, (p, o) => guardedInvoke(n, p, o, invoke, pushBefore))
  const call = CALL_STATE[options.label]
  if (rejudgeRecord) rejudgeRecord.gateCallIndex = call ? CALL_RECORDS.indexOf(call) : null
  const mode = call && call.mode === 'prefix' ? 'prefix' : 'live'
  NODE_STATE[n.id] = { key, replayed: mode === 'prefix' }
  const rec = { node: n.id, key, payload, prompt, result: result === undefined ? null : result, mode, callIndex: call ? CALL_RECORDS.indexOf(call) : null, verifies: n.verifies, verifiedBy: n.verifiedBy, replay: n.replay, fresh: n.fresh, irreversible: n.irreversible, contract: n.contract, target: n.target }
  if (reading) { rec.reading = readingValue(reading); rec.readingCallIndex = LIVE_READING_CALLS[n.id] }
  if (call && call.source) rec.source = call.source
  MEMO_RECORDS.push(rec)
  if (n.gate && reading && n.target && result && (typeof result.sha !== 'string' || result.sha.toLowerCase() !== reading.tip.toLowerCase())) {
    note(n.id, 'TREE-MISMATCH', 'gate returned a sha different from the fixed sentinel reading')
    halt('TREE-MISMATCH')
    return null
  }
  annotateBuilder(n, reading)
  return result
}
async function reservedSpawn(prompt, options, invoke) {
  if (HALT) return null
  if (budget.total && budget.remaining() - RESERVED < NODE_FLOOR) {
    INCOMPLETE = true
    if (typeof DECLINED !== 'undefined') { DECLINED++; DECLINED_LABELS.add(String(options.label)) }
    note(String(options.label), 'SKIPPED', 'token floor reserved by in-flight calls')
    return null
  }
  RESERVED += NODE_FLOOR
  try { return await invoke(prompt, options) } finally { RESERVED -= NODE_FLOOR }
}
async function runGraphNode(id, body) {
  if (HALT) return
  try { await body() } catch (error) {
    note(id, 'ERROR', String(error && error.message || error)); halt('NODE-ERROR')
  }
  if (!NODE_STATE[id]) NODE_STATE[id] = { key: sha256(canonical({ id, result: RESULTS[id] === undefined ? null : RESULTS[id] })), replayed: false }
}
