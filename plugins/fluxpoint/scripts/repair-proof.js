// Embedded into repair graphs; host reads use a fixed-command sentinel, not
// fixer/checker reports. No filesystem, clock, crypto or other host API here.
const REPAIR_AXES = ['field', 'encoding', 'caller', 'network', 'role', 'path']
const REPAIR_SURFACES = ['outbound-calls', 'alarms', 'timers', 'caches', 'parsers', 'guards', 'other']
const REPAIR_SURFACE_PATTERNS = [
  ['outbound-calls', /\b(fetch|requests?|https?|providers?|clients?|rpc|outbound)\b/i],
  ['alarms', /\b(alarm\w*|alert\w*|pag(?:e|es|ing)|notify\w*)\b/i],
  ['timers', /\b(timer\w*|sleep|interval\w*|cadence|timeout\w*|deadline\w*|latency|rate.?limit\w*)\b/i],
  ['caches', /\b(cache\w*|memo\w*|ttl)\b/i], ['parsers', /\b(pars\w*|decod\w*|encod\w*|json|wrappers?)\b/i],
  ['guards', /\b(if|assert|validat\w*|guard\w*|requir\w*)\b/i],
]
function repairSurfaces(content) { const s = REPAIR_SURFACE_PATTERNS.filter(p => p[1].test(content)).map(p => p[0]); return s.length ? s : ['other'] }
const repairObservation = o => !!o && ['command', 'result'].every(k => repairText(o[k]))
const repairFinite = (v, positive = false) => typeof v === 'number' && Number.isFinite(v) && (positive ? v > 0 : v >= 0)
function repairBytes(encoded) {
  if (typeof encoded !== 'string' || encoded.length % 4 || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) throw new Error('invalid raw patch base64')
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
  const bytes = []
  for (let i = 0; i < encoded.length; i += 4) {
    const a = alphabet.indexOf(encoded[i]), b = alphabet.indexOf(encoded[i + 1])
    const c = alphabet.indexOf(encoded[i + 2]), d = alphabet.indexOf(encoded[i + 3])
    if (c < 0 && (b & 15) || d < 0 && c >= 0 && (c & 3)) throw new Error('noncanonical raw patch base64')
    bytes.push(a << 2 | b >> 4)
    if (c >= 0) bytes.push((b & 15) << 4 | c >> 2)
    if (d >= 0) bytes.push((c & 3) << 6 | d)
  }
  return bytes
}
function repairSHA256(input) {
  const k = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]
  const h = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]
  const data = input.slice(), bits = input.length * 8
  data.push(128)
  while (data.length % 64 !== 56) data.push(0)
  for (let i = 7; i >= 0; i--) data.push(Math.floor(bits / 2 ** (i * 8)) & 255)
  const rotr = (x, n) => x >>> n | x << (32 - n)
  for (let offset = 0; offset < data.length; offset += 64) {
    const w = []
    for (let i = 0; i < 16; i++) w[i] = (data[offset + i * 4] << 24 | data[offset + i * 4 + 1] << 16 | data[offset + i * 4 + 2] << 8 | data[offset + i * 4 + 3]) >>> 0
    for (let i = 16; i < 64; i++) {
      const a = w[i - 15], b = w[i - 2]
      w[i] = (w[i - 16] + (rotr(a,7) ^ rotr(a,18) ^ a >>> 3) + w[i - 7] + (rotr(b,17) ^ rotr(b,19) ^ b >>> 10)) >>> 0
    }
    let [a,b,c,d,e,f,g,j] = h
    for (let i = 0; i < 64; i++) {
      const t = (j + (rotr(e,6) ^ rotr(e,11) ^ rotr(e,25)) + (e & f ^ ~e & g) + k[i] + w[i]) >>> 0
      const u = ((rotr(a,2) ^ rotr(a,13) ^ rotr(a,22)) + (a & b ^ a & c ^ b & c)) >>> 0
      j=g;g=f;f=e;e=(d+t)>>>0;d=c;c=b;b=a;a=(t+u)>>>0
    }
    ;[a,b,c,d,e,f,g,j].forEach((v, i) => { h[i] = (h[i] + v) >>> 0 })
  }
  return h.map(v => v.toString(16).padStart(8, '0')).join('')
}
function repairUTF8(bytes) {
  let encoded = ''
  for (const byte of bytes) encoded += '%' + byte.toString(16).padStart(2, '0')
  return decodeURIComponent(encoded)
}
function repairManifest(bytes) {
  const lines = repairUTF8(bytes).split('\n'), hunks = []
  let path = null
  lines.forEach((line, i) => {
    if (line.startsWith('diff --git ')) path = line.slice(11)
    else if (line.startsWith('+++ ')) path = line.slice(4)
    else if (line.startsWith('@@ ')) {
      const m = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/.exec(line)
      if (!m || path === null) throw new Error('malformed patch hunk')
      const added = []
      for (let j = i + 1; j < lines.length && !lines[j].startsWith('@@ ') && !lines[j].startsWith('diff --git '); j++) {
        if (lines[j].startsWith('+')) added.push(lines[j].slice(1))
      }
      if (!added.length) return
      const content = added.join('\n')
      const surfaces = repairSurfaces(content), obligations = []
      if (surfaces.some(s => ['outbound-calls', 'timers'].includes(s))) obligations.push('latency')
      if (surfaces.includes('alarms')) obligations.push('alarm-rate')
      if (/rate.?limit|token.?bucket|burst/i.test(content)) obligations.push('provider-pool')
      hunks.push({ id:'h'+i, path, newStart:Number(m[1]), newLines:m[2] === undefined ? 1 : Number(m[2]), added, surfaces, obligations })
    } else if (line === 'GIT binary patch' || line.startsWith('Binary files ')) {
      if (path === null) throw new Error('binary patch lacks file identity')
      hunks.push({ id:'h'+i,path,newStart:0,newLines:0,added:[],surfaces:['other'],obligations:[],binary:true })
    }
  })
  const changed = lines.filter(s => /^[+-]/.test(s) && !s.startsWith('+++ ') && !s.startsWith('--- ')).map(s => s.slice(1)).join('\n')
  return { patchSha256:repairSHA256(bytes),hunks,changedSurfaces:repairSurfaces(changed) }
}
function repairSiblingIssue(check, context, fixed) {
  if (!repairTargets(check.targetClasses) || JSON.stringify([...check.targetClasses].sort()) !== JSON.stringify([...context.targetClasses].sort())) return 'SIBLINGS-UNPROVEN: checker targetClasses differ from pinned slice'
  const rows = check.siblings, claims = fixed.siblings === undefined ? [] : fixed.siblings
  if (!Array.isArray(rows) || context.targetClasses.length && !rows.length || !Array.isArray(claims)) return 'SIBLINGS-UNPROVEN: explicit checker observations and fixer claims are required'
  const key = r => JSON.stringify([r.finding,r.axis,r.surface])
  if (rows.some(r => !r || !['finding','invariant','surface','reason'].every(k => repairText(r[k])) || !REPAIR_AXES.includes(r.axis) || !['DEFENDED','NOT-APPLICABLE'].includes(r.outcome) || !repairObservation(r.attack) || !repairObservation(r.control) || r.outcome === 'NOT-APPLICABLE' && !/[^\s:]+:\d+/.test(r.reason))) return 'SIBLINGS-UNPROVEN: malformed, unexecuted or uncited sibling observation'
  if (new Set(rows.map(key)).size !== rows.length) return 'SIBLINGS-UNPROVEN: duplicate observation'
  if (claims.some(c => !c || !rows.some(r => key(r) === key(c) && ['attack','control'].every(k => r[k].command === c[k]?.command)))) return 'SIBLINGS-UNPROVEN: checker must rerun every fixer-listed attack and control'
  const listed = new Set(claims.map(key))
  if (context.targetClasses.some(finding => REPAIR_AXES.some(axis => !rows.some(r => r.finding === finding && r.axis === axis && !listed.has(key(r)))))) return 'SIBLINGS-UNPROVEN: each class needs an unlisted sibling on every axis'
  return null
}
function repairCoverageIssue(check, measured, fixed) {
  const c = check.diffCoverage, rows = c?.hunks
  if (!c || c.patchSha256 !== measured.patchSha256) return 'DIFF-UNPROVEN: coverage names another patch SHA-256'
  if (!Array.isArray(rows) || rows.length !== measured.hunks.length || rows.some(r => !r) || new Set(rows.map(r => r.id)).size !== rows.length || measured.hunks.some(h => !rows.some(r => r.id === h.id))) return 'DIFF-UNPROVEN: every harness-listed hunk needs exactly one row'
  if (rows.some(r => !REPAIR_SURFACES.includes(r.surface) || !repairObservation(r.attack) || !repairObservation(r.control))) return 'DIFF-UNPROVEN: each hunk needs a surface, attack and control'
  const obligations = measured.hunks.flatMap(h => h.obligations), operational = c.operational || {}
  if (obligations.includes('latency')) {
    const r = operational.latency
    if (!repairObservation(r) || !repairFinite(r.deadlineMs,true) || !repairFinite(r.observedMs) || r.observedMs > r.deadlineMs) return 'DIFF-UNPROVEN: latency must meet a declared measured deadline'
    if (obligations.includes('provider-pool') && (!repairText(r.pool) || !(Array.isArray(fixed.sharedResources)?fixed.sharedResources:[]).some(p => p?.resource === r.pool))) return 'DIFF-UNPROVEN: rate-limited provider needs a declared shared pool'
  }
  if (obligations.includes('alarm-rate')) {
    const r = operational.alarmRate
    if (!repairObservation(r) || !repairText(r.seriesPath) || !/^(?:[A-Za-z]:[\\/]|[\\/])/.test(r.seriesPath) || typeof r.seriesSha256 !== 'string' || !/^[0-9a-f]{64}$/.test(r.seriesSha256) || !repairFinite(r.durationSeconds,true) || !Number.isSafeInteger(r.falsePositives) || r.falsePositives < 0 || !repairFinite(r.maxPerDay) || r.falsePositives*86400/r.durationSeconds > r.maxPerDay) return 'DIFF-UNPROVEN: alarm rate needs recorded series provenance and an acceptable measured rate'
  }
  return null
}
const REPAIR_SENTINEL_SCHEMA = {type:'object',required:['status'],properties:{status:{type:'string'},rawPatch:{type:'string'},base:{type:'string'},patch:{type:'string'},item:{type:'string'},round:{type:'integer'},snapshot:{type:'string'},token:{type:'string'},patchSha256:{type:'string'},observations:{type:'array',items:{type:'object'}},issue:{type:'string'}}}
const repairQuote = s => "'" + String(s).replace(/'/g, "'\\''") + "'"
function repairIdentity(context) { const p = context.integrated || context.current; return {base:context.base,item:context.item,round:context.round,patch:p.patch} }
async function repairSentinel(mode, context, extra = {}, id = '') {
  const identity = repairIdentity(context)
  const args = mode === 'cleanup' ? extra : {...identity,root:REPAIR_LAUNCH.root,...extra}
  const command = 'bash ' + repairQuote(REPAIR_PROOF_HELPER) + ' repair-proof.py ' + mode + ' ' + Object.entries(args).map(([k,v]) => '--' + k + ' ' + repairQuote(v)).join(' ')
  return agent('FIXED REPAIR WITNESS. Run exactly this command and return its raw JSON verbatim. Do not inspect or accept fixer/checker reasoning, manually edit source/artifacts, infer a digest, or substitute another command. The helper may create/clean only its owned scratch archives. A command failure returns status INCOMPLETE with its issue.\nCOMMAND: ' + command + '\nREAD PHASE: '+id+'\nFRESH LAUNCH READ: ' + A._replayLaunchNonce,
    {label:'repair-witness:'+mode+':'+context.item+':r'+context.round+':'+id,effort:'low',schema:REPAIR_SENTINEL_SCHEMA})
}
async function repairReadPatch(context, readId = 'before-check') {
  if (typeof A._replayLaunchNonce !== 'string' || !/^[0-9a-f]{64}$/.test(A._replayLaunchNonce)) return {issue:'DIFF-UNPROVEN: fresh _replayLaunchNonce is required at every launch/resume'}
  const r = await repairSentinel('read',context,{},readId)
  const id = repairIdentity(context)
  if (!r || r.status !== 'READ' || Object.keys(id).some(k => r[k] !== id[k])) return {issue:'DIFF-UNPROVEN: fixed patch witness missing or identity mismatched'}
  try { return {...repairManifest(repairBytes(r.rawPatch)),...id} }
  catch (e) { return {issue:'DIFF-UNPROVEN: '+String(e.message || e)} }
}
function repairProofBrief(measured, context, fixed) {
  return 'HARNESS PATCH MANIFEST (derived from raw patch bytes, never your own inventory): '+JSON.stringify(measured)+'\nReturn targetClasses exactly '+JSON.stringify(context.targetClasses)+' and siblings for every targeted class on ALL six axes: field, encoding, caller, network, role, path. Rerun every fixer-listed sibling attack/control yourself, then attack at least one unlisted sibling per axis. NOT-APPLICABLE requires inspected snapshot file:line. Map EVERY harness hunk id in diffCoverage with the exact patchSha256, surface and your own attack/control command/results. Latency requires a declared deadline and measured bound; alarm-rate requires a recorded production series path/digest and measured acceptable false-positive rate. Rate-limited providers need declared shared pool resources. Missing proof is INCOMPLETE.\nFIXER SIBLING CLAIMS (not evidence): '+JSON.stringify(fixed.siblings || [])+'\n'
}
function repairCheckerEvidenceIssue(check, context, measured) {
  const witness = check.checkerWitness, observations = witness?.observations
  if (!witness || witness.status !== 'CLEANED' || witness.base !== context.base || witness.patchSha256 !== measured.patchSha256 || witness.readNonce !== A._replayLaunchNonce || !Array.isArray(observations) || !observations.length || observations.some(o => !o || o.base !== context.base || o.patchSha256 !== measured.patchSha256 || o.reproduces !== false)) return 'CHECKER-UNPROVEN: independent checker witness is missing or records an undefeated harm'
  const same = (a,b) => repairObservation(a) && repairObservation(b) && a.command === b.command && a.result === b.result
  const pairs = [...(check.siblings || []), ...(check.diffCoverage?.hunks || [])]
  if (pairs.some(r => !observations.some(o => o.reproduces === false && same(r.attack,o.attack) && same(r.control,o.control)))) return 'CHECKER-UNPROVEN: every defended sibling and hunk row must match an actually defended checker execution'
  const operational = check.diffCoverage?.operational || {}, latency = operational.latency, alarm = operational.alarmRate
  if (latency && !observations.some(o => same(latency,o.attack) && repairFinite(o.attack.elapsedMs) && latency.observedMs === o.attack.elapsedMs)) return 'CHECKER-UNPROVEN: latency must use the helper-measured elapsed time'
  if (alarm && !observations.some(o => same(alarm,o.attack) && alarm.seriesPath === o.seriesPath && alarm.seriesSha256 === o.seriesSha256 && o.metrics && ['durationSeconds','falsePositives','maxPerDay'].every(k => alarm[k] === o.metrics[k]))) return 'CHECKER-UNPROVEN: alarm measurements must match executed JSON output and the helper-read production series'
  return null
}
function repairSnapshotBrief(snapshot) {
  return 'SNAPSHOT IDENTITY: '+JSON.stringify(snapshot)+'\nThe harness already applied the complete current patch to this fresh archive of the exact launch base. Work only there; no second snapshot or second patch application. Run ALL attack/control cases through bash '+repairQuote(REPAIR_PROOF_HELPER)+' repair-proof.py execute --snapshot '+repairQuote(snapshot.snapshot)+' --token '+repairQuote(snapshot.token)+' --control <valid-command> --attack <case-command>. Copy the actual command/result observations into each sibling and hunk row. Latency observedMs must copy attack.elapsedMs; for alarms pass --series <absolute-production-series>, read its frozen copy from FPL_PRODUCTION_SERIES, and have the command print JSON {durationSeconds,falsePositives,maxPerDay}, then copy its measured output and helper-read series digest. These actual executions, not descriptions, are independently logged outside the snapshot and collected by the cleanup sentinel.\n'
}
async function repairCheckedSpawn(context, measured, prompt, options) {
  const snapshot = await repairSentinel('materialize',context,{sha256:measured.patchSha256},options.label)
  if (!snapshot || snapshot.status !== 'READY' || snapshot.base !== context.base || snapshot.patchSha256 !== measured.patchSha256 || !repairText(snapshot.snapshot) || typeof snapshot.token !== 'string' || !/^[0-9a-f]{64}$/.test(snapshot.token)) return {verdict:'INCOMPLETE',findings:[],blockers:['checker fresh base archive unavailable'],evidence:['harness could not materialize checker snapshot']}
  let result = null, cleanup = null
  try { result = await spawn(repairSnapshotBrief(snapshot)+prompt,options) }
  finally { cleanup = await repairSentinel('cleanup',context,{snapshot:snapshot.snapshot,token:snapshot.token},options.label) }
  const valid = cleanup && cleanup.status === 'CLEANED' && cleanup.snapshot === snapshot.snapshot && cleanup.token === snapshot.token
  if (!valid) return {verdict:'INCOMPLETE',findings:[],blockers:['checker snapshot/process cleanup unconfirmed'],evidence:['harness refused undrained or missing checker snapshot']}
  return result && typeof result === 'object' ? {...result,checkerWitness:{...cleanup,spawnId:options.label,base:context.base,patchSha256:measured.patchSha256,readNonce:A._replayLaunchNonce}} : null
}
function repairOriginalTitles() {
  const f = Array.isArray(A.findings?.findings) ? A.findings.findings : A.findings
  return Array.isArray(f) ? f.map(x => typeof x === 'string' ? x : x?.finding || x?.title).filter(repairText) : repairText(f) ? [f] : []
}
async function repairDiffAttack(context, measured, phaseName) {
  const prefix = 'repair-diff:'+context.item+':r'+context.round
  const receipt = {version:1,...repairIdentity(context),patchSha256:measured.patchSha256,readNonce:A._replayLaunchNonce,settled:repairOriginalTitles(),attackers:[],refuters:[],findings:[],snapshots:[],blockers:[],clean:false}
  const attackSchema = {type:'object',required:['base','patchSha256','completed','evidence','blockers','findings'],properties:{base:{type:'string'},patchSha256:{type:'string'},completed:{type:'boolean'},evidence:{type:'array',minItems:1,items:{type:'string'}},blockers:{type:'array',items:{type:'string'}},findings:C.FixCheckV1.properties.findings}}
  const voteSchema = {type:'object',required:['resolution','attack','control','contradiction'],properties:{resolution:{enum:['REFUTED','CONFIRMED','UNRESOLVED']},attack:{type:'object'},control:{type:'object'},contradiction:{type:'string'}}}
  const subject = 'PINNED PATCH SUBJECT: '+JSON.stringify(measured)+'\nORIGIN REPLAY CONTEXT: '+JSON.stringify(context)+'\nORIGINAL FINDINGS: '+JSON.stringify(A.findings || [])+'\nSETTLED ORIGINAL FINDINGS (context, never evidence): '+JSON.stringify(repairOriginalTitles())
  const snapshotPrompt = s => 'SNAPSHOT IDENTITY: '+JSON.stringify(s)+'\nFresh base-archive snapshot: '+s.snapshot+'; ownership token: '+s.token+'. Run all controls/attacks through bash '+repairQuote(REPAIR_PROOF_HELPER)+' repair-proof.py execute --snapshot '+repairQuote(s.snapshot)+' --token '+repairQuote(s.token)+' --control <valid-command> --attack <case-command>. Copy its actual attack/control observations into refutation votes. That helper drains owned subprocesses and writes an independent execution log before returning. An evidence string without a logged execution cannot complete this seat. Do not run detached commands or mutate shared files. Missing tools/controls are blockers. '
  const observed = r => Array.isArray(r?._executed) && r._executed.length > 0 && r._executed.every(o => o && o.base === context.base && o.patchSha256 === measured.patchSha256 && repairObservation(o.attack) && repairObservation(o.control) && typeof o.reproduces === 'boolean')
  async function runSeat(id, prompt, schema) {
    if (!affordable(id)) { receipt.blockers.push(id+': budget floor'); return null }
    const snapshot = await repairSentinel('materialize',context,{sha256:measured.patchSha256},id)
    if (!snapshot || snapshot.status !== 'READY' || snapshot.base !== context.base || snapshot.patchSha256 !== measured.patchSha256 || !repairText(snapshot.snapshot) || typeof snapshot.token !== 'string' || !/^[0-9a-f]{64}$/.test(snapshot.token)) { receipt.blockers.push(id+': fresh base archive unavailable'); return null }
    let result = null, executions = []
    try { result = await spawn(subject+'\n'+snapshotPrompt(snapshot)+'\n'+prompt,{label:id,phase:phaseName,schema,effort:'medium'}) }
    catch (e) { receipt.blockers.push(id+': agent failed: '+String(e.message || e)) }
    finally {
      const cleaned = await repairSentinel('cleanup',context,{snapshot:snapshot.snapshot,token:snapshot.token},id)
      if (!cleaned || cleaned.status !== 'CLEANED' || cleaned.snapshot !== snapshot.snapshot || cleaned.token !== snapshot.token) receipt.blockers.push(id+': snapshot/process cleanup unconfirmed')
      else { executions = cleaned.observations; receipt.snapshots.push({...cleaned,spawnId:id}) }
    }
    return result && typeof result === 'object' ? {...result,_executed:executions} : null
  }
  // This catalogue is embedded from workflows/red-team.js by the compiler.
  // The full small diff is scoped to each applicable surface plus composition.
  const surfaces = new Set(measured.changedSurfaces)
  const ids = new Set(['state','composition'])
  if (surfaces.has('guards') || surfaces.has('other') || context.targetClasses.length) ids.add('authority')
  if (surfaces.has('parsers') || surfaces.has('other')) ids.add('input')
  if (['outbound-calls','alarms','timers','caches','other'].some(x => surfaces.has(x))) ids.add('operations')
  if (surfaces.has('other') || surfaces.has('outbound-calls')) ids.add('value')
  const lenses = REPAIR_REVIEW_LENSES.filter(l => ids.has(l.id))
  const raw = []
  async function attack(lens) {
    const spawnId = prefix+':attack:'+lens.id
    const r = await runSeat(spawnId,'Read '+REPAIR_REVIEW_AGENT+'. Attack the complete added/changed code and same-invariant sibling fields, encodings, callers, networks, roles and paths. LENS '+lens.id+': '+lens.focus+'\nReturn the supplied repair review schema (not a final RedTeamV1 verdict): pinned base and patchSha256, completed only with executed attack/control evidence, all blockers and FixCheckV1-shaped findings including measured origin replay. You receive neither fixer reasoning nor checker verdict. With no claims, inspect coverage for false negatives rather than merely repeating the original attack.',attackSchema)
    const droppedHarm = Array.isArray(r?._executed) && r._executed.some(o => o.reproduces === true && !(Array.isArray(r.findings)?r.findings:[]).some(f => {
      const e=f?.originEvidence, current=e?.integrated || (Array.isArray(e?.rounds)?e.rounds:[]).find(p => p && repairPatchKey(p) === repairPatchKey(context.integrated || context.current))
      return current && current.reproduces === true && current.command === o.attack.command && current.result === o.attack.result && current.controlCommand === o.control.command && current.controlResult === o.control.result
    }))
    const completed = !!r && observed(r) && !droppedHarm && r.base === context.base && r.patchSha256 === measured.patchSha256 && r.completed === true && repairStrings(r.evidence) && r.evidence.length > 0 && Array.isArray(r.blockers) && !r.blockers.length && Array.isArray(r.findings)
    receipt.attackers.push({spawnId,lens:lens.id,base:r?.base,patchSha256:r?.patchSha256,completed,evidence:r?.evidence || [],observations:r?._executed || []})
    if (!completed) receipt.blockers.push(spawnId+': missing, incomplete or mismatched attacker')
    if (Array.isArray(r?.findings)) for (const f of r.findings) {
      const issue = repairOriginIssue(f,context)
      if (issue) receipt.blockers.push(spawnId+': '+issue)
      else raw.push({...f,id:'P'+(raw.length+1),source:spawnId})
    }
  }
  await parallel(lenses.map(lens => () => attack(lens)))
  if (!raw.length && !receipt.blockers.length) await attack({id:'coverage-skeptic',focus:'Independently sample representative defended sibling and added-hunk cases for false negatives. Inspect all six sibling axes and new operational behavior; report concrete claims through findings or incomplete coverage through blockers.'})
  await parallel(raw.map(f => async () => {
    const votes = await parallel(REPAIR_REVIEW_REFUTERS.map(lens => async () => {
      const spawnId = prefix+':refute:'+f.id+':'+lens.id
      const r = await runSeat(spawnId,'Read '+REPAIR_REVIEW_AGENT+'. Independently test this claim, never the fixer/checker reasoning: '+JSON.stringify(f)+'\nREFUTER '+lens.id+': '+lens.focus+'\nReturn the supplied refutation schema. REFUTED requires contradictory EXECUTED attack and valid-control command/results plus the specific contradiction. Missing/null/uncertain observations are UNRESOLVED. Never refute from a settled title or inference.',voteSchema)
      const valid = !!r && observed(r) && ['REFUTED','CONFIRMED','UNRESOLVED'].includes(r.resolution) && repairObservation(r.attack) && repairObservation(r.control) && r._executed.some(o => o.attack.command === r.attack.command && o.attack.result === r.attack.result && o.control.command === r.control.command && o.control.result === r.control.result) && (r.resolution !== 'REFUTED' || repairText(r.contradiction) && r._executed.every(o => o.reproduces === false))
      const vote = {spawnId,resolution:valid?r.resolution:'UNRESOLVED',attack:r?.attack || null,control:r?.control || null,contradiction:r?.contradiction || '',observations:r?._executed || []}
      receipt.refuters.push(vote)
      return vote
    }))
    receipt.findings.push({...f,votes})
  }))
  const open = receipt.findings.filter(f => f.votes.some(v => v.resolution !== 'REFUTED'))
  const reread = await repairReadPatch(context,'after-review')
  if (reread.issue || reread.patchSha256 !== measured.patchSha256) receipt.blockers.push('patch changed or fresh post-review read unavailable')
  receipt.clean = !receipt.blockers.length && !open.length
  REPAIR_DIFF_REVIEWS.push(receipt)
  return {receipt,findings:open,issue:receipt.blockers.length ? receipt.blockers.join('; ') : null}
}
