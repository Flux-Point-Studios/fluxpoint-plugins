// fluxpoint graph-audit: the multi-lens semantic audit behind
// /fluxpoint:graph-audit. Hand-written and shipped with the plugin; unlike a
// compiled .graph.js it has no IR behind it, so edit it here.
//
// One graph-auditor per round converged falsely (issue #97): three rounds
// reported 3, 4 and 6 findings with nothing above MEDIUM, and the next round
// run as six lenses with refuting verifiers found two real HIGHs, one of them
// raised independently by five of the six lenses, and refuted nine findings a
// single auditor would have handed over as repairs. A falling count from one
// reader measures where that reader stopped, not what is left. So a round is:
//
//   Lenses  one auditor per lens, same scope and evidence rule
//   Reduce  one cheap agent merges duplicates across lenses, judging nothing
//   Verify  refuters per distinct finding, told to refute and to default to
//           refuted when uncertain: three lenses (reproduce, reachability,
//           scope) at HIGH and above with a 2-of-3 rule, one combined below
//
// SOUND means every lens returned and no distinct finding survived or went
// unchecked. A verifier that returns nothing leaves its finding UNVERIFIED,
// which is never confirmed, never refuted, and never SOUND.
//
// args (a JSON object):
//   graph       the WORK.md or GRAPH.*.md under audit (required)
//   pluginRoot  the fluxpoint plugin root; agents read
//               <pluginRoot>/agents/graph-auditor.md (required)
//   check       the compile-graph.py --check summary line (optional)
//   settled     titles earlier rounds fixed or refuted (optional)
//   lenses      replaces the default lenses: ids from LENSES below, or
//               {id, focus} objects (optional)
//   mode        'single' for the cheap round: one auditor, no reduce, no
//               refutation, every finding UNVERIFIED (optional)
export const meta = {
  name: 'graph-audit',
  description: 'Multi-lens semantic audit of a Graph Engineering IR: lens-scoped auditors, a reduce that merges duplicates, refuting verifiers, and per-stage counts ending SOUND or REWIRE.',
  whenToUse: 'Run by /fluxpoint:graph-audit after the compile check is clean; not a general code review.',
  phases: [
    { title: 'Lenses', detail: 'one graph-auditor per lens, same scope and evidence rule' },
    { title: 'Reduce', detail: 'merge duplicates across lenses without judging them' },
    { title: 'Verify', detail: 'refuters per finding: three lenses at HIGH and above, one below' },
    { title: 'Single', detail: 'cheap mode: one graph-auditor, no reduce, no refutation' },
  ],
}

const SEVERITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']
const RANK = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 }

// The six lenses of the round that found what one reader missed. Each is a
// full graph-auditor with one place to spend its reading.
const LENSES = [
  { id: 'commands', focus: 'Every command, path and agentType the IR names or its nodes will run. Does it exist, does it run from where the node runs it, does it read the state the node assumes (fresh or stale refs, the right branch, the right tree), and does its exit code mean what the gate reads it as?' },
  { id: 'data-flow', focus: 'What each node receives and what it hands on: every after edge, every {{prev}} and {{prev.<field>}}, contract fields produced and never read or read and never produced, state a prompt assumes that no edge delivers, and context packets that paste whole files where spans would do.' },
  { id: 'verdicts', focus: 'Every verdict, gate and haltWhen. Can the contract produce the failing value? Can the gate pass while the thing it guards fails? Is a verifier invited to confirm instead of refute? Does a filter, marker, tag or skip let work through every gate uncounted?' },
  { id: 'runtime', focus: 'How the compiled script executes: isolation and worktree bases, resume and memoized replay, repeat and dry rules against round ceilings, node floors and budget ceilings, dead-worker handling, cacheTtl and effort transitions, and what a partial failure leaves behind.' },
  { id: 'merge-deploy', focus: 'Everything after the last green: merge, release, deploy and any irreversible effect. Does the terminal step read current state rather than a stale copy? Is every irreversible effect marked irreversible, behind a rehearsal whose gate can fail? Does the campaign end merged or explicitly parked, and does anything let graph output override the Stop-hook gate?' },
  { id: 'walkthrough', focus: 'Walk the executor end to end: from launch, visit every node in order. At each node say what it receives, what it does and what it hands on, and report each place where the campaign would claim a success it did not earn.' },
]

const SINGLE = { id: 'auditor', focus: 'the whole checklist' }

// Refuter lenses for HIGH and above. Below HIGH one refuter asks all three.
const REFUTERS = [
  { id: 'reproduce', focus: 'Reproduce the failure path from the repository as it is. Read the cited files and lines and run what can be run without changing anything. Refute if the failure does not happen as described.' },
  { id: 'reachability', focus: 'Establish whether any launch, resume, round or input actually reaches the broken step, or whether an edge, gate, contract, halt condition or ceiling already blocks it. Refute if it is unreachable or already guarded.' },
  { id: 'scope', focus: 'Establish whether the finding concerns this graph and the code its nodes touch rather than a general concern or a preference, whether the stated severity matches the stakes, and whether the minimal rewire would close it. Refute if it is out of scope, overstated by more than one severity level, or a settled item raised again without new measured evidence.' },
]
const COMBINED = {
  id: 'combined',
  focus: 'Answer all three questions and refute when any of them fails. ' +
    REFUTERS.map(r => `${r.id}: ${r.focus}`).join(' '),
}

const FINDINGS = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: SEVERITIES },
          title: { type: 'string', minLength: 8 },
          failurePath: { type: 'string', minLength: 20 },
          evidence: { type: 'string', minLength: 10 },
          rewire: { type: 'string', minLength: 10 },
        },
        required: ['severity', 'title', 'failurePath', 'evidence', 'rewire'],
      },
    },
  },
  required: ['findings'],
}

const CLUSTERS = {
  type: 'object',
  properties: {
    clusters: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          members: { type: 'array', items: { type: 'string' }, minItems: 1 },
          settled: { type: 'string' },
        },
        required: ['members', 'settled'],
      },
    },
  },
  required: ['clusters'],
}

const VERDICT = {
  type: 'object',
  properties: {
    refuted: { type: 'boolean' },
    reason: { type: 'string', minLength: 10 },
  },
  required: ['refuted', 'reason'],
}

// ---------------------------------------------------------------- inputs --

const A = typeof args === 'string' ? JSON.parse(args) : (args || {})
if (typeof A.graph !== 'string' || !A.graph.trim()) {
  throw new Error('graph-audit: args.graph is required: the WORK.md or GRAPH.*.md to audit')
}
if (typeof A.pluginRoot !== 'string' || !A.pluginRoot.trim()) {
  throw new Error('graph-audit: args.pluginRoot is required: agents read <pluginRoot>/agents/graph-auditor.md for the checklist and severity scale')
}
const MODE = A.mode === undefined || A.mode === null ? 'multi' : A.mode
if (MODE !== 'multi' && MODE !== 'single') {
  throw new Error(`graph-audit: args.mode must be 'single' or omitted, got ${JSON.stringify(A.mode)}`)
}
if (A.settled !== undefined && A.settled !== null && !Array.isArray(A.settled)) {
  throw new Error('graph-audit: args.settled must be an array of titles')
}

const norm = s => String(s).toLowerCase().replace(/\s+/g, ' ').trim()
const SETTLED = []
for (const s of A.settled || []) {
  const t = typeof s === 'string' ? s.trim() : (s && typeof s.title === 'string' ? s.title.trim() : '')
  if (t && !SETTLED.some(x => norm(x) === norm(t))) SETTLED.push(t)
}
const settledTitle = s => (s ? SETTLED.find(x => norm(x) === norm(s)) || '' : '')

function resolveLenses(spec) {
  if (spec === undefined || spec === null) return LENSES
  if (!Array.isArray(spec) || !spec.length) {
    throw new Error('graph-audit: args.lenses must be a non-empty array of lens ids or {id, focus} objects')
  }
  const out = spec.map((l, i) => {
    const id = typeof l === 'string' ? l : (l && l.id)
    const known = LENSES.find(x => x.id === id)
    if (l && typeof l === 'object' && typeof l.focus === 'string' && l.focus.trim()) {
      return { id: String(id || `custom-${i + 1}`), focus: l.focus.trim() }
    }
    if (known) return known
    throw new Error(`graph-audit: lens ${i + 1} (${JSON.stringify(l)}) is neither one of ${LENSES.map(x => x.id).join(', ')} nor an {id, focus} object`)
  })
  const ids = out.map(l => l.id)
  const dup = ids.find((id, i) => ids.indexOf(id) !== i)
  if (dup) throw new Error(`graph-audit: lens id ${dup} appears twice; each lens needs its own id`)
  return out
}
const RUN_LENSES = MODE === 'single' ? [SINGLE] : resolveLenses(A.lenses)

// --------------------------------------------------------------- prompts --

function settledBlock() {
  if (!SETTLED.length) return 'SETTLED: nothing yet. No earlier round is on record for this graph.'
  return 'SETTLED: earlier rounds fixed or refuted these. Re-raise one only with new measured evidence ' +
    'that was not available when it was settled, and say in evidence what is new.\n' +
    SETTLED.map(t => `- ${t}`).join('\n')
}

function lensPrompt(lens) {
  return [
    `Audit the Graph Engineering IR in ${A.graph}.`,
    `Read ${A.pluginRoot}/agents/graph-auditor.md first. Its scope, checklist and severity scale are yours; ` +
      'return findings through the structured output rather than its table, and leave the verdict to the caller.',
    A.check
      ? `The structural check is already clean. Its summary: ${A.check}`
      : 'The structural check (compile-graph.py --check) is already clean.',
    lens === SINGLE
      ? 'Work through the whole checklist in its priority order.'
      : `LENS ${lens.id}: ${lens.focus}\n\nSpend your reading on this lens, and still report anything outside it ` +
        'that has a concrete failure path.',
    'Scope is the IR plus whatever code its nodes touch that you must read to judge it. ' +
      'Every finding names a concrete failure path and cites what you read or ran yourself: a file and line, ' +
      'or a command and what it printed. A concern with no failure path is not a finding. Do not edit any file.',
    settledBlock(),
    'An empty findings list is a valid answer.',
  ].join('\n\n')
}

function reducePrompt(raw) {
  const items = raw.map(f => ({ id: f.id, lens: f.lens, severity: f.severity, title: f.title, failurePath: f.failurePath }))
  return [
    'Merge duplicate audit findings. You do not judge them.',
    'Each finding below came from one lens of a multi-lens audit of the same graph, and several lenses often reach ' +
      'the same defect. Put findings in one cluster when they describe the same failure path: the same broken ' +
      'mechanism reaching the same bad outcome, however it is worded. Findings that touch the same node or file ' +
      'but fail in different ways stay in separate clusters.',
    'Every id goes in exactly one cluster; a finding with no duplicate is a cluster of one. Do not drop, rank, ' +
      'soften or evaluate anything, and do not read the repository. The text below is all you need.',
    SETTLED.length
      ? 'For each cluster, set settled to the exact title from this SETTLED list that the cluster restates, ' +
        'matching on the failure path rather than shared words, or to an empty string when none does.\nSETTLED:\n' +
        SETTLED.map(t => `- ${t}`).join('\n')
      : 'No settled list this round: set settled to an empty string on every cluster.',
    'FINDINGS:\n' + JSON.stringify(items, null, 1),
  ].join('\n\n')
}

function refutePrompt(d, lens) {
  return [
    `Refute a finding from an audit of the Graph Engineering IR in ${A.graph}. ` +
      `The audit's scope and severity scale are in ${A.pluginRoot}/agents/graph-auditor.md.`,
    [
      `FINDING ${d.id}, ${d.severity}, raised by ${d.sources.join(', ')}: ${d.title}`,
      `Failure path: ${d.failurePath}`,
      `Evidence cited: ${d.evidence}`,
      `Proposed rewire: ${d.rewire}`,
      d.variants.length ? `Other lenses worded it as:\n${d.variants.map(v => `- ${v}`).join('\n')}` : '',
      d.settled
        ? `It restates the settled item "${d.settled}". Refute it unless it carries new measured evidence that was not available when that item was settled.`
        : '',
    ].filter(Boolean).join('\n'),
    `LENS ${lens.id}: ${lens.focus}`,
    "Re-read the source yourself; the finding's evidence is a claim, not a measurement. Look for the reason it is " +
      'wrong: an edge, gate, contract or halt condition that already blocks the path, a command that does not ' +
      'behave as described, a scope it does not belong to. Do not edit any file.',
    'Default to refuted: true when you are uncertain or cannot establish the failure path from what you read or ran. ' +
      'In reason, say what you read or ran and what it showed.',
  ].join('\n\n')
}

// ---------------------------------------------------------------- stages --

const unique = xs => xs.filter((x, i) => xs.indexOf(x) === i)
const bySeverity = (a, b) => RANK[a.severity] - RANK[b.severity]

// Every raw finding lands in exactly one cluster, whatever the reduce agent
// returned: ids it invented are ignored, ids it repeated keep their first
// cluster, ids it left out become clusters of one, and each of those repairs
// is logged. Severity is the highest any member claimed, taken here in code
// so the reduce cannot soften it.
function buildDistinct(raw, reduced) {
  const byId = new Map(raw.map(f => [f.id, f]))
  const owner = new Set()
  const groups = []
  let invented = 0, repeated = 0, unknownSettled = 0
  for (const c of (reduced && Array.isArray(reduced.clusters)) ? reduced.clusters : []) {
    const members = []
    for (const id of Array.isArray(c.members) ? c.members : []) {
      if (!byId.has(id)) { invented++; continue }
      if (owner.has(id)) { repeated++; continue }
      owner.add(id)
      members.push(id)
    }
    if (!members.length) continue
    const claimed = typeof c.settled === 'string' ? c.settled.trim() : ''
    const settled = settledTitle(claimed)
    if (claimed && !settled) unknownSettled++
    groups.push({ members, settled })
  }
  const orphans = raw.filter(f => !owner.has(f.id))
  if (reduced) {
    if (invented) log(`reduce: ignored ${invented} id(s) that no lens produced`)
    if (repeated) log(`reduce: ${repeated} id(s) placed in two clusters; each kept its first`)
    if (unknownSettled) log(`reduce: ${unknownSettled} settled match(es) named a title not on the settled list; treated as unmatched`)
    if (orphans.length) log(`reduce: left ${orphans.length} finding(s) unclustered; each is verified on its own`)
  }
  for (const f of orphans) groups.push({ members: [f.id], settled: '' })

  return groups.map((g, i) => {
    const ms = g.members.map(id => byId.get(id))
    const lead = ms.slice().sort(bySeverity)[0]
    const exact = ms.map(m => settledTitle(m.title)).find(Boolean) || ''
    return {
      id: `D${i + 1}`,
      severity: lead.severity,
      title: lead.title,
      failurePath: lead.failurePath,
      evidence: lead.evidence,
      rewire: lead.rewire,
      sources: unique(ms.map(m => m.lens)),
      variants: unique(ms.filter(m => m !== lead).map(m => m.title)).filter(t => t !== lead.title),
      members: g.members,
      settled: g.settled || exact,
    }
  })
}

// A finding is decided only when every verifier it was sent to answered.
// Any missing vote leaves it UNVERIFIED: a dead refuter is neither a
// refutation nor a confirmation, and counting it as either is the silent
// cap this workflow exists to remove.
async function verify(d) {
  const high = RANK[d.severity] <= RANK.HIGH
  const lenses = high ? REFUTERS : [COMBINED]
  const need = high ? 2 : 1
  const votes = await parallel(lenses.map(l => () =>
    agent(refutePrompt(d, l), { label: `refute:${l.id}:${d.id}`, phase: 'Verify', schema: VERDICT })))
  const cast = votes.filter(Boolean)
  const standing = cast.filter(v => v.refuted === false).length
  const dead = lenses.length - cast.length
  const status = dead ? 'UNVERIFIED' : (standing >= need ? 'CONFIRMED' : 'REFUTED')
  const tally = `${standing}/${lenses.length} not refuted${dead ? `, ${dead} verifier(s) returned nothing` : ''}`
  log(`${d.id} ${d.severity} ${status} (${tally}): ${d.title}`)
  return {
    ...d,
    status,
    verifiers: lenses.length,
    deadVerifiers: dead,
    votes: lenses.map((l, i) => votes[i]
      ? { lens: l.id, refuted: votes[i].refuted, reason: votes[i].reason }
      : { lens: l.id, refuted: null, reason: 'verifier returned nothing' }),
  }
}

function publicFinding(d) {
  return {
    id: d.id, severity: d.severity, title: d.title, failurePath: d.failurePath,
    rewire: d.rewire, evidence: d.evidence, sources: d.sources, settled: d.settled,
    votes: d.votes,
  }
}

function summarize(raw, judged, dead, verifiers, deadVerifiers) {
  const pick = s => judged.filter(d => d.status === s).sort(bySeverity)
  const confirmed = pick('CONFIRMED'), refuted = pick('REFUTED'), unverified = pick('UNVERIFIED')
  const severityCounts = {}
  for (const s of SEVERITIES) {
    const at = judged.filter(d => d.severity === s)
    severityCounts[s] = {
      distinct: at.length,
      confirmed: at.filter(d => d.status === 'CONFIRMED').length,
      refuted: at.filter(d => d.status === 'REFUTED').length,
      unverified: at.filter(d => d.status === 'UNVERIFIED').length,
    }
  }
  const settledMatched = judged.filter(d => d.settled)
  const counts = {
    lenses: RUN_LENSES.length,
    lensesReturned: RUN_LENSES.length - dead.length,
    raw: raw.length,
    distinct: judged.length,
    settledMatched: settledMatched.length,
    confirmed: confirmed.length,
    refuted: refuted.length,
    unverified: unverified.length,
    verifiers,
    deadVerifiers,
    bySeverity: severityCounts,
  }
  const sound = !confirmed.length && !unverified.length && !dead.length
  const why = []
  if (dead.length) {
    why.push(MODE === 'single'
      ? 'the auditor returned nothing, so nothing was audited'
      : `lens ${dead.join(', ')} returned nothing, so its ground is unaudited`)
  }
  if (confirmed.length) {
    why.push(`${confirmed.length} finding(s) survived refutation, worst ${confirmed[0].severity}: ${confirmed[0].title}`)
  }
  if (unverified.length) {
    why.push(MODE === 'single'
      ? `${unverified.length} finding(s) from a single auditor, not refuted`
      : `${unverified.length} finding(s) left UNVERIFIED by a verifier that returned nothing`)
  }
  const verdict = sound ? 'SOUND' : 'REWIRE'
  log(`counts: ${counts.lensesReturned}/${counts.lenses} lens(es), ${counts.raw} raw -> ${counts.distinct} distinct ` +
    `(${counts.settledMatched} settled) -> ${counts.confirmed} confirmed, ${counts.refuted} refuted, ` +
    `${counts.unverified} unverified; ${verifiers} verifier(s), ${deadVerifiers} returned nothing`)
  log(`verdict: ${verdict}${why.length ? ` (${why.join('; ')})` : ''}`)
  return {
    mode: MODE,
    graph: A.graph,
    lenses: RUN_LENSES.map(l => l.id),
    deadLenses: dead,
    settled: SETTLED,
    counts,
    confirmed: confirmed.map(publicFinding),
    unverified: unverified.map(publicFinding),
    refuted: refuted.map(d => d.title),
    settledMatched: settledMatched.map(d => ({ id: d.id, title: d.title, settled: d.settled, status: d.status })),
    verdict,
    why: why.length ? why.join('; ') : (MODE === 'single'
      ? 'the single auditor found no failure path'
      : `${counts.lenses} lens(es) returned and ${counts.raw ? 'every distinct finding was refuted' : 'none found a failure path'}`),
  }
}

// ------------------------------------------------------------------ body --

log(`graph-audit: ${MODE} mode over ${A.graph}, ${RUN_LENSES.length} lens(es) ` +
  `[${RUN_LENSES.map(l => l.id).join(', ')}], ${SETTLED.length} settled title(s)`)

const lensPhase = MODE === 'single' ? 'Single' : 'Lenses'
phase(lensPhase)
// Barrier on purpose: the reduce merges across lenses, so it needs all of them.
const lensResults = await parallel(RUN_LENSES.map(l => () =>
  agent(lensPrompt(l), { label: MODE === 'single' ? 'auditor' : `lens:${l.id}`, phase: lensPhase, schema: FINDINGS })))

const dead = []
const raw = []
let malformed = 0
RUN_LENSES.forEach((l, i) => {
  const r = lensResults[i]
  if (!r || !Array.isArray(r.findings)) { dead.push(l.id); return }
  r.findings.forEach(f => {
    if (!f || !Object.prototype.hasOwnProperty.call(RANK, f.severity) || typeof f.title !== 'string') { malformed++; return }
    raw.push({ ...f, id: `F${raw.length + 1}`, lens: l.id })
  })
})
if (malformed) log(`lenses: dropped ${malformed} finding(s) with no title or an unknown severity`)
log(`lenses: ${RUN_LENSES.length - dead.length}/${RUN_LENSES.length} returned, ${raw.length} raw finding(s)` +
  (MODE === 'single' ? '' : ` [${RUN_LENSES.map((l, i) => `${l.id} ${lensResults[i] && Array.isArray(lensResults[i].findings) ? lensResults[i].findings.length : 'dead'}`).join(', ')}]`))
if (dead.length) log(`lens(es) returned nothing: ${dead.join(', ')}. Their ground is unaudited and the round cannot be SOUND`)

if (MODE === 'single') {
  // The cheap round: nothing merges and nothing refutes, so every finding
  // stays UNVERIFIED. It is a progress check between repairs.
  const judged = buildDistinct(raw, null).map(d => ({
    ...d, status: 'UNVERIFIED', verifiers: 0, deadVerifiers: 0, votes: [],
  }))
  return summarize(raw, judged, dead, 0, 0)
}

let distinct = []
if (raw.length) {
  phase('Reduce')
  const reduced = await agent(reducePrompt(raw), { label: 'reduce', phase: 'Reduce', schema: CLUSTERS, effort: 'low' })
  if (!reduced) log(`reduce: the agent returned nothing; verifying all ${raw.length} raw finding(s) unmerged`)
  distinct = buildDistinct(raw, reduced)
  log(`reduce: ${raw.length} raw -> ${distinct.length} distinct, ${distinct.filter(d => d.settled).length} matching the settled list`)
} else {
  log('reduce: skipped, no lens reported a finding')
}

let judged = []
if (distinct.length) {
  phase('Verify')
  const high = distinct.filter(d => RANK[d.severity] <= RANK.HIGH).length
  log(`verify: ${high} finding(s) at HIGH or above get ${REFUTERS.length} refuters each, ` +
    `${distinct.length - high} below get one; ${high * REFUTERS.length + distinct.length - high} verifier(s)`)
  const out = await pipeline(distinct, d => verify(d))
  // A stage that threw comes back null; that finding was not checked.
  judged = distinct.map((d, i) => out[i] || {
    ...d, status: 'UNVERIFIED', verifiers: 0, deadVerifiers: 0,
    votes: [{ lens: 'stage', refuted: null, reason: 'verification stage failed before any verdict' }],
  })
}
const verifiers = judged.reduce((n, d) => n + d.verifiers, 0)
const deadVerifiers = judged.reduce((n, d) => n + d.deadVerifiers, 0)
return summarize(raw, judged, dead, verifiers, deadVerifiers)
