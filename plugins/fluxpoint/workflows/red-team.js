// Code counterpart of graph-audit.js: immutable review ranges, independent
// surface attackers, duplicate reduction, and skeptical executable refutation.
// Workflow globals only: no filesystem, process, clock, or random access.
//
// args: {pluginRoot, subject: [{repository, base, head, files, additions,
// deletions}], lenses?: [id | {id, focus}], settled?: [title], sampled?: bool}.
// base/head must be resolved Git object ids. Resolve ranges and git diff stats
// before launching; every agent checks the same identities, and a fresh final
// agent checks them again. A missing or malformed result can never become SHIP.
export const meta = {
  name: 'red-team',
  description: 'Multi-lens adversarial code review with pinned ranges, duplicate reduction, independent refuters and explicit RedTeamV1 coverage.',
  whenToUse: 'Run by /fluxpoint:red-team for a broad change or as an adversarial gate before irreversible work.',
  phases: [
    { title: 'Lenses', detail: 'independent attackers on five surfaces plus composition' },
    { title: 'Reduce', detail: 'merge duplicate exploit paths without judging them' },
    { title: 'Verify', detail: 'reproduction, reachability and scope refuters; no-claim coverage skeptic' },
    { title: 'Snapshot', detail: 'fresh final identity and coverage recheck' },
  ],
}

const SEVERITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']
const RANK = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 }
const LENSES = [
  { id: 'authority', focus: 'Authority and identity: missing checks, confused owner/operator roles, cross-tenant/network replay, key rotation, caps and allow-lists.' },
  { id: 'value', focus: 'Value and arithmetic: conservation, double counting, overflow, units, rounding, boundary quantities, fees and shared-resource accounting.' },
  { id: 'state', focus: 'State and provenance: stale snapshots, check/use races, forged receipts, mismatched parsed/hashed data, mutable assumptions and partial writes.' },
  { id: 'input', focus: 'Input and execution: malformed data, path/command injection, hostile dependency output, retry storms, timeouts and surviving subprocesses.' },
  { id: 'operations', focus: 'Operations and infrastructure: secret exposure, excessive authority, configuration-dependent bypasses, resource exhaustion and recovery paths.' },
  { id: 'composition', focus: 'A distinct composition pass: combine operations, identities, replay windows, shared resources, concurrency, partial failures and multi-step sequences across components and repositories.' },
]
const REFUTERS = [
  { id: 'reproduce', focus: 'Reconstruct the minimal executable case on the reviewed snapshot. Establish that the valid control works and the attack reaches the claimed boundary. Refute only with contradictory executable evidence; fixture failures or unavailable tools are UNRESOLVED.' },
  { id: 'reachability', focus: 'Trace real entry points, deployment configurations and attacker preconditions. Check whether an enforced, tested guard blocks the exploit before its effect. Advice, an unsupported assumption or rejection at an unrelated earlier guard does not refute it.' },
  { id: 'scope', focus: 'Check the violated invariant, supported configurations, actual impact and proposed repair against this range. Reproduce or inspect evidence independently. Refute only a demonstrated out-of-scope or false claim; an uncertain severity or a settled title alone is not a refutation.' },
]
const SUBJECT = {
  type: 'array', minItems: 1,
  items: { type: 'object', required: ['repository', 'base', 'head', 'files', 'additions', 'deletions'], properties: {
    repository: { type: 'string', minLength: 1 }, base: { type: 'string', minLength: 1 }, head: { type: 'string', minLength: 1 },
    files: { type: 'integer', minimum: 0 }, additions: { type: 'integer', minimum: 0 }, deletions: { type: 'integer', minimum: 0 },
  } },
}
const FINDINGS = {
  type: 'object', required: ['subject', 'completed', 'sampled', 'evidence', 'blockers', 'findings'], properties: {
    subject: SUBJECT, completed: { type: 'boolean' }, sampled: { type: 'boolean' },
    evidence: { type: 'string', minLength: 10 }, blockers: { type: 'array', items: { type: 'string' } },
    findings: { type: 'array', items: { type: 'object', required: ['severity', 'title', 'failurePath', 'evidence', 'rewire'], properties: {
      severity: { type: 'string', enum: SEVERITIES }, title: { type: 'string', minLength: 8 },
      failurePath: { type: 'string', minLength: 20 }, evidence: { type: 'string', minLength: 10 }, rewire: { type: 'string', minLength: 10 },
    } } },
  },
}
const CLUSTERS = { type: 'object', required: ['clusters'], properties: {
  clusters: { type: 'array', items: { type: 'object', required: ['members', 'settled'], properties: {
    members: { type: 'array', minItems: 1, items: { type: 'string' } }, settled: { type: 'string' },
  } } },
} }
const VOTE = { type: 'object', required: ['resolution', 'evidence'], properties: {
  resolution: { type: 'string', enum: ['CONFIRMED', 'REFUTED', 'UNRESOLVED'] }, evidence: { type: 'string', minLength: 10 },
} }
const SNAPSHOT = { type: 'object', required: ['stable', 'subject', 'evidence', 'blockers'], properties: {
  stable: { type: 'boolean' }, subject: SUBJECT, evidence: { type: 'string', minLength: 10 },
  blockers: { type: 'array', items: { type: 'string' } },
} }

const A = typeof args === 'string' ? JSON.parse(args) : (args || {})
const text = (v, min = 1) => typeof v === 'string' && v.trim().length >= min
if (!text(A.pluginRoot)) throw new Error('red-team: args.pluginRoot is required')
const subjects = A.subject || A.repositories || (A.repository ? [A] : null)
if (!Array.isArray(subjects) || !subjects.length) throw new Error('red-team: args.subject requires at least one pinned repository range')
const SUBJECTS = subjects.map((s, i) => {
  if (!s || !text(s.repository) || ![s.base, s.head].every(v => typeof v === 'string' && /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i.test(v))
    || !['files', 'additions', 'deletions'].every(k => Number.isSafeInteger(s[k]) && s[k] >= 0)) {
    throw new Error(`red-team: subject ${i + 1} needs repository, resolved base/head object ids, and nonnegative integer files/additions/deletions`)
  }
  return { repository: s.repository, base: s.base.toLowerCase(), head: s.head.toLowerCase(), files: s.files, additions: s.additions, deletions: s.deletions }
})
if (new Set(SUBJECTS.map(s => s.repository)).size !== SUBJECTS.length) throw new Error('red-team: subject repository appears twice')
if (A.sampled !== undefined && typeof A.sampled !== 'boolean') throw new Error('red-team: args.sampled must be boolean')
if (A.settled !== undefined && (!Array.isArray(A.settled) || !A.settled.every(s => text(s)))) throw new Error('red-team: args.settled must be an array of titles')
const norm = s => String(s).toLowerCase().replace(/\s+/g, ' ').trim()
const unique = xs => xs.filter((x, i) => xs.indexOf(x) === i)
const SETTLED = unique((A.settled || []).map(s => s.trim()))
const settledTitle = s => s ? SETTLED.find(x => norm(x) === norm(s)) || '' : ''
const bySeverity = (a, b) => RANK[a.severity] - RANK[b.severity]
function resolveLenses(spec) {
  if (spec === undefined) return LENSES
  if (!Array.isArray(spec) || !spec.length) throw new Error('red-team: args.lenses must be a nonempty array')
  const out = spec.map((l, i) => {
    if (typeof l === 'string' && LENSES.some(x => x.id === l)) return LENSES.find(x => x.id === l)
    if (l && typeof l === 'object' && text(l.id) && text(l.focus)) return { id: l.id.trim(), focus: l.focus.trim() }
    throw new Error(`red-team: invalid lens ${i + 1}; use a known id or {id, focus}`)
  })
  if (new Set(out.map(l => l.id)).size !== out.length) throw new Error('red-team: each lens needs a unique id')
  if (!out.some(l => l.id === 'composition')) throw new Error('red-team: custom lenses must include a separate composition lens')
  if (out.length < 2) throw new Error('red-team: a gate requires independent surface and composition attackers')
  return out
}
const RUN_LENSES = resolveLenses(A.lenses)
const scope = 'PINNED SUBJECT (same for every attacker and refuter):\n' + JSON.stringify(SUBJECTS, null, 2)
const settled = 'SETTLED TITLES (context, never evidence that a current exploit is false):\n' + JSON.stringify(SETTLED)
const sameSubject = actual => Array.isArray(actual) && actual.length === SUBJECTS.length && SUBJECTS.every((s, i) =>
  actual[i] && Object.keys(s).every(k => actual[i][k] === s[k]))
const stringList = v => Array.isArray(v) && v.every(x => typeof x === 'string')
function lensPrompt(lens) {
  return [
    `Read ${A.pluginRoot}/agents/red-team-reviewer.md first. Attack this code change under that evidence and severity discipline; do not edit shared source or deploy anything.`,
    scope, `LENS ${lens.id}: ${lens.focus}`,
    'Verify git object identities and diff file/addition/deletion counts yourself. Experiments must run in isolated scratch copies containing exactly the reviewed head, with its dependencies and deployment assumptions. Return the observed subject exactly; mismatched snapshots block this gate.',
    'Build this lens\'s coverage matrix. Run valid controls and attacks at the intended boundary. Return completed only after the applicable work executed, sampled true if any inspected surface was sampled, evidence naming commands/artifacts and outcomes, and blockers for every unresolved or unexecuted part. With no executable surface, justify that from the complete diff. Empty findings alone do not complete coverage.',
    'Report concrete claims with severity, title, failurePath, evidence and rewire. The workflow assigns fresh independent refuters; do not claim your own skepticism discharged a finding. Return all required structured fields.',
    settled,
  ].join('\n\n')
}
function reducePrompt(raw) {
  return [
    'Merge duplicate code-review findings by the same failure mechanism and bad outcome. Judge nothing. Every id belongs in exactly one cluster; different failures in the same file stay separate. Never lower severity, drop an id, or treat a settled title as proof.',
    settled,
    'Set settled to an exact listed title only when its failure path matches, otherwise empty.',
    'FINDINGS:\n' + JSON.stringify(raw.map(f => ({ id: f.id, lens: f.lens, severity: f.severity, title: f.title, failurePath: f.failurePath })), null, 1),
  ].join('\n\n')
}
function refutePrompt(d, lens) {
  return [
    `Read ${A.pluginRoot}/agents/red-team-reviewer.md. Independently refute a code exploit against the same snapshot.`, scope,
    `FINDING ${d.id}, ${d.severity}, raised by ${d.sources.join(', ')}: ${d.title}\nFailure path: ${d.failurePath}\nEvidence claimed: ${d.evidence}\nMinimal fix: ${d.rewire}`,
    d.merged.length ? 'Every merged claim must be checked. REFUTED requires contradictory evidence against EVERY failure path:\n' + JSON.stringify(d.merged) : '',
    `LENS ${lens.id}: ${lens.focus}`,
    settled,
    'Inspect source and run the minimal case yourself in an isolated copy at the pinned head. Return CONFIRMED only with supported failure/impact, REFUTED only with concrete contradictory evidence, otherwise UNRESOLVED. Missing tools, uncertainty, snapshot drift, broken fixtures and an unavailable control are UNRESOLVED. Do not edit shared files.',
  ].filter(Boolean).join('\n\n')
}

function buildDistinct(raw, reduced) {
  const byId = new Map(raw.map(f => [f.id, f]))
  const owner = new Set()
  const groups = []
  let invented = 0, repeated = 0, unknownSettled = 0, sameLens = 0
  for (const c of (reduced && Array.isArray(reduced.clusters)) ? reduced.clusters : []) {
    if (!c || typeof c !== 'object') continue
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
    // Split a cluster holding two findings from one lens, each into the
    // first part that has none from its lens yet.
    const parts = []
    for (const id of members) {
      const lens = byId.get(id).lens
      let part = parts.find(pt => !pt.some(x => byId.get(x).lens === lens))
      if (!part) { part = []; parts.push(part) }
      part.push(id)
    }
    if (parts.length > 1) sameLens++
    for (const pt of parts) groups.push({ members: pt, settled })
  }
  const orphans = raw.filter(f => !owner.has(f.id))
  if (reduced) {
    if (invented) log(`reduce: ignored ${invented} id(s) that no lens produced`)
    if (repeated) log(`reduce: ${repeated} id(s) placed in two clusters; each kept its first`)
    if (sameLens) log(`reduce: split ${sameLens} cluster(s) that merged two findings from one lens; one auditor does not report the same failure twice`)
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
      merged: ms.filter(m => m !== lead).map(m => ({
        lens: m.lens, severity: m.severity, title: m.title, failurePath: m.failurePath, evidence: m.evidence,
      })),
      members: g.members,
      settled: g.settled || exact,
    }
  })
}

async function verify(d) {
  const lenses = RANK[d.severity] <= RANK.MEDIUM ? REFUTERS : [{ id: 'combined', focus: REFUTERS.map(l => l.focus).join(' ') }]
  const votes = await parallel(lenses.map(l => () => agent(refutePrompt(d, l), { label: `refute:${l.id}:${d.id}`, phase: 'Verify', schema: VOTE })))
  const valid = votes.map(v => v && ['CONFIRMED', 'REFUTED', 'UNRESOLVED'].includes(v.resolution) && text(v.evidence, 10) ? v : null)
  const confirmed = valid.filter(v => v?.resolution === 'CONFIRMED').length
  const refuted = valid.filter(v => v?.resolution === 'REFUTED').length
  const unresolved = valid.some(v => !v || v.resolution === 'UNRESOLVED')
  // Uncertainty never defaults to refutation. A divided panel is unresolved
  // unless a majority independently reproduced the exploit; refutation needs
  // every assigned refuter to contradict all of the merged failure paths.
  const status = unresolved ? 'UNVERIFIED' : refuted === lenses.length ? 'REFUTED'
    : confirmed >= Math.floor(lenses.length / 2) + 1 ? 'CONFIRMED' : 'UNVERIFIED'
  return { ...d, status, verifiers: lenses.length, deadVerifiers: valid.filter(v => !v).length,
    votes: lenses.map((l, i) => ({ lens: l.id, ...(valid[i] || { resolution: 'UNRESOLVED', evidence: 'refuter returned no valid result' }) })) }
}

phase('Lenses')
const lensResults = await parallel(RUN_LENSES.map(l => () => agent(lensPrompt(l), { label: `lens:${l.id}`, phase: 'Lenses', schema: FINDINGS })))
const raw = [], blockers = [], dead = [], reports = [], completedLenses = []
RUN_LENSES.forEach((l, i) => {
  const r = lensResults[i]
  if (!r || !Array.isArray(r.findings)) { dead.push(l.id); blockers.push(`${l.id}: attacker returned no valid report`); return }
  const blockersBefore = blockers.length
  reports.push({ lens: l.id, subject: r.subject, completed: r.completed, sampled: r.sampled, evidence: r.evidence, blockers: r.blockers })
  if (!sameSubject(r.subject) || r.completed !== true || typeof r.sampled !== 'boolean' || !text(r.evidence, 10) || !stringList(r.blockers)) {
    blockers.push(`${l.id}: incomplete coverage, invalid evidence or mismatched reviewed subject`)
  }
  if (stringList(r.blockers)) blockers.push(...r.blockers.map(b => `${l.id}: ${b}`))
  r.findings.forEach(f => {
    if (!f || !Object.prototype.hasOwnProperty.call(RANK, f.severity) || !text(f.title, 8) || !text(f.failurePath, 20) || !text(f.evidence, 10) || !text(f.rewire, 10)) {
      blockers.push(`${l.id}: malformed finding was not verified`); return
    }
    raw.push({ ...f, id: `F${raw.length + 1}`, lens: l.id })
  })
  if (blockers.length === blockersBefore) completedLenses.push(l.id)
})
let skeptic = null
if (!raw.length && !blockers.length) {
  phase('Verify')
  skeptic = await agent([
    `Read ${A.pluginRoot}/agents/red-team-reviewer.md. Independently challenge a no-claims code review; do not edit shared source.`, scope,
    'No attacker claimed an exploit. Reconstruct representative defended cases yourself in an isolated copy at the pinned head. Check valid controls, actual entry-point reachability and that rejection occurred at the intended boundary. Look for failures the attackers missed, including composition. A generic failing test, an earlier unrelated rejection, missing tools or a broken fixture cannot count as a defense.',
    'Return subject, completed, sampled, evidence, blockers and findings. Set completed only after executable sampling with cited commands/artifacts, or a justified complete-diff determination that no executable surface exists. Report newly found exploit paths as findings; uncertainty or unavailable checks are blockers. Your independent sampling does not replace any primary attacker or expand their declared coverage.',
    'PRIMARY COVERAGE REPORTS:\n' + JSON.stringify(reports),
    settled,
  ].join('\n\n'), { label: 'skeptic:coverage', phase: 'Verify', schema: FINDINGS })
  if (!skeptic || !sameSubject(skeptic.subject) || skeptic.completed !== true || typeof skeptic.sampled !== 'boolean'
    || !text(skeptic.evidence, 10) || !stringList(skeptic.blockers) || !Array.isArray(skeptic.findings)) {
    blockers.push('independent no-claims coverage skeptic returned no complete valid verification')
  }
  if (stringList(skeptic?.blockers)) blockers.push(...skeptic.blockers.map(b => `coverage skeptic: ${b}`))
  if (Array.isArray(skeptic?.findings)) skeptic.findings.forEach(f => {
    if (!f || !Object.prototype.hasOwnProperty.call(RANK, f.severity) || !text(f.title, 8) || !text(f.failurePath, 20) || !text(f.evidence, 10) || !text(f.rewire, 10)) {
      blockers.push('coverage skeptic: malformed exploit claim remains unresolved'); return
    }
    raw.push({ ...f, id: `F${raw.length + 1}`, lens: 'coverage-skeptic' })
  })
}
let distinct = []
if (raw.length) {
  phase('Reduce')
  const reduced = await agent(reducePrompt(raw), { label: 'reduce', phase: 'Reduce', schema: CLUSTERS, effort: 'low' })
  distinct = buildDistinct(raw, reduced)
}
let judged = []
if (distinct.length) {
  phase('Verify')
  const out = await pipeline(distinct, d => verify(d))
  judged = distinct.map((d, i) => out[i] || { ...d, status: 'UNVERIFIED', verifiers: 0, deadVerifiers: 0, votes: [] })
}
const confirmed = judged.filter(d => d.status === 'CONFIRMED').sort(bySeverity)
const unverified = judged.filter(d => d.status === 'UNVERIFIED').sort(bySeverity)
const refuted = judged.filter(d => d.status === 'REFUTED').sort(bySeverity)
if (confirmed.length) blockers.push(`${confirmed.length} executable finding(s) survived refutation`)
if (unverified.length) blockers.push(`${unverified.length} claim(s) remain unresolved after refutation`)
let identity = null
if (!blockers.length) {
  phase('Snapshot')
  identity = await agent([
    'Independently recheck the review identity after the attackers and refuters finished. Do not review their conclusions or edit files.', scope,
    'Resolve the repository HEAD and base/head objects, inspect working status and recompute the reviewed diff counts. Verify every experiment and coverage artifact names this head. Return stable true only when this exact reviewed state is still current and all range identities and counts match. Missing access or drift is a blocker. Return observed subject, evidence and blockers.',
    'COVERAGE REPORTS:\n' + JSON.stringify(reports),
    'INDEPENDENT COVERAGE SKEPTIC:\n' + JSON.stringify(skeptic),
    'REFUTATION RECORDS:\n' + JSON.stringify(judged),
  ].join('\n\n'), { label: 'snapshot:final', phase: 'Snapshot', schema: SNAPSHOT })
  if (!identity || identity.stable !== true || !sameSubject(identity.subject) || !text(identity.evidence, 10) || !stringList(identity.blockers)) {
    blockers.push('final snapshot identity recheck missing, invalid or drifted')
  }
  if (stringList(identity?.blockers)) blockers.push(...identity.blockers.map(b => `snapshot: ${b}`))
}
const standing = [...confirmed, ...unverified].sort(bySeverity)
const worstSeverity = standing[0]?.severity || 'NONE'
const verdict = blockers.length ? 'BLOCK' : 'SHIP'
const counts = { lenses: RUN_LENSES.length, lensesReturned: RUN_LENSES.length - dead.length, raw: raw.length, distinct: distinct.length,
  attackersCompleted: completedLenses.length,
  confirmed: confirmed.length, refuted: refuted.length, unverified: unverified.length,
  verifiers: judged.reduce((n, d) => n + d.verifiers, 0), deadVerifiers: judged.reduce((n, d) => n + d.deadVerifiers, 0) }
log(`counts: ${counts.lensesReturned}/${counts.lenses} attackers, ${counts.raw} raw -> ${counts.distinct} distinct -> ${counts.confirmed} confirmed, ${counts.refuted} refuted, ${counts.unverified} unresolved`)
log(`verdict: ${verdict}${blockers.length ? ` (${blockers.join('; ')})` : ''}`)
return {
  verdict, worstSeverity, worstSeverityRank: worstSeverity === 'NONE' ? 0 : 4 - RANK[worstSeverity],
  findings: standing.map(d => ({ severity: d.severity, finding: d.title, exploit_path: d.failurePath, minimal_fix: d.rewire })),
  ...(completedLenses.length ? { coverage: { subject: SUBJECTS, attackers: completedLenses.length, lenses: completedLenses,
    sampled: A.sampled === true || reports.some(r => r.sampled === true) } } : {}),
  review: { subject: SUBJECTS, plannedLenses: RUN_LENSES.map(l => l.id), counts, blockers: unique(blockers), deadLenses: dead,
    reports, skeptic, snapshot: identity, settled: SETTLED,
    confirmed, unverified, refuted: refuted.map(d => ({ ...d, titles: [d.title, ...d.variants] })) },
}
