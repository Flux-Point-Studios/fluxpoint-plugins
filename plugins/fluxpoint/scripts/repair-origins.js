// Embedded by the compiler for repair loops. No filesystem or model inference.
const FIX_ORIGIN_NAMES = ['pre-existing', 'incomplete-fix', 'fix-introduced', 'cross-slice', 'spec-text']
const repairText = v => typeof v === 'string' && v.trim().length > 0
const repairStrings = v => Array.isArray(v) && v.every(repairText)
const repairTargets = v => repairStrings(v) && new Set(v).size === v.length
function repairOriginContextIssue(context) {
  if (!context || !repairText(context.base) || !Number.isSafeInteger(context.round) || context.round < 1 || !Array.isArray(context.patches) || !context.patches.length) return 'origin replay requires the pinned base, round and actual patch history'
  if (!repairTargets(context.targetClasses)) return 'origin replay requires explicit pinned targetClasses (use [] for no targeted classes)'
  const patch = p => !!p && repairText(p.item) && repairText(p.patch) && Number.isSafeInteger(p.round) && p.round >= 1 && p.round <= context.round
  if (!context.patches.every(patch) || new Set(context.patches.map(repairPatchKey)).size !== context.patches.length) return 'origin replay context has invalid or duplicate patch identities'
  const current = context.integrated || context.current
  if (!patch(current) || current.round !== context.round || !context.integrated && !context.patches.some(p => repairPatchKey(p) === repairPatchKey(current))) return 'origin replay context must pin the exact current round patch'
  return null
}
function repairResourcesIssue(slice) {
  if (!slice || !Array.isArray(slice.sharedResources)) return 'SliceV1.sharedResources must be an explicit array (use [] for none)'
  if (slice.sharedResources.some(r => !r || !repairText(r.resource) || !repairStrings(r.writers) || !repairStrings(r.readers) || !r.writers.length && !r.readers.length)) return 'shared resource needs resource and named writers[]/readers[]'
  if (new Set(slice.sharedResources.map(r => r.resource)).size !== slice.sharedResources.length) return 'duplicate shared resource declaration'
  return null
}
function repairObserved(o) {
  return !!o && ['command','result','controlCommand','controlResult'].every(k => repairText(o[k])) && o.controlPassed === true && typeof o.reproduces === 'boolean'
}
const repairPatchKey = p => JSON.stringify([p.item, p.round, p.patch])
function repairOriginIssue(f, context) {
  const contextIssue = repairOriginContextIssue(context)
  if (contextIssue) return contextIssue
  if (!f || !FIX_ORIGIN_NAMES.includes(f.origin) || !['finding','defectClass','invariant','classRewire','measurement'].every(k => repairText(f[k])) || !['LOW','MEDIUM','HIGH','CRITICAL'].includes(f.severity)) return 'finding lacks a valid origin or defect/measurement fields'
  if (f.introducedIn !== undefined && (!Number.isSafeInteger(f.introducedIn) || f.introducedIn < 1 || f.introducedIn > context.round)) return 'introducedIn is outside the completed repair rounds'
  const e = f.originEvidence
  if (!e || typeof e !== 'object' || Array.isArray(e)) return 'finding needs executed originEvidence'
  if (f.origin === 'spec-text') {
    const t = e.text
    return t && ['requirement','scope','argv','graph'].includes(t.kind) && ['path','command','result'].every(k => repairText(t[k])) ? null : 'spec-text origin needs measured requirement/scope/argv/graph text evidence'
  }
  if (!repairObserved(e.base) || e.base.snapshot !== context.base || !repairTargets(e.targetClasses)) return 'origin replay needs a valid control and reproduction on the exact launch base and declared targetClasses'
  if (JSON.stringify([...e.targetClasses].sort()) !== JSON.stringify([...context.targetClasses].sort())) return 'origin targetClasses differ from the slice targets'
  if (!Array.isArray(e.rounds) || e.rounds.length !== context.patches.length) return 'origin replay must cover every actual round patch'
  const seen = new Set()
  for (const row of e.rounds) {
    if (!repairObserved(row) || !Number.isSafeInteger(row.round) || row.round < 1 || !repairText(row.item) || !repairText(row.patch)) return 'invalid or unexecuted round replay'
    if (row.command !== e.base.command || row.controlCommand !== e.base.controlCommand) return 'origin replay changed the reproduction or valid control between snapshots'
    const key = repairPatchKey(row)
    if (seen.has(key) || !context.patches.some(p => repairPatchKey(p) === key)) return 'origin replay names a duplicate, stale or mismatched patch/round'
    seen.add(key)
  }
  if (context.integrated) {
    if (!repairObserved(e.integrated) || repairPatchKey(e.integrated) !== repairPatchKey(context.integrated) || !e.integrated.reproduces) return 'integration finding must reproduce on the exact current integrated patch'
    if (e.integrated.command !== e.base.command || e.integrated.controlCommand !== e.base.controlCommand) return 'integrated origin replay changed the case or control'
  } else {
    const current = e.rounds.find(p => repairPatchKey(p) === repairPatchKey(context.current))
    if (!current || !current.reproduces || e.integrated !== undefined) return 'finding does not reproduce on the current slice patch'
  }
  let expected
  const first = e.rounds.filter(p => p.reproduces).sort((a,b) => a.round - b.round)[0]
  if (e.base.reproduces) expected = context.targetClasses.includes(f.defectClass) ? 'incomplete-fix' : 'pre-existing'
  else if (first) expected = 'fix-introduced'
  else if (context.integrated) expected = 'cross-slice'
  else return 'a runtime finding needs a reproduced failure'
  if (f.origin !== expected) return `origin ${f.origin} contradicts replay (${expected})`
  if (expected === 'fix-introduced' && (!Number.isSafeInteger(f.introducedIn) || f.introducedIn !== first.round || f.introducedIn > context.round)) return 'introducedIn must name the actual first reproducing round'
  if (f.introducedIn !== undefined && (!Number.isSafeInteger(f.introducedIn) || f.introducedIn < 1 || f.introducedIn > context.round)) return 'introducedIn is outside the completed repair rounds'
  return null
}
function repairCheckOrigins(check, context) {
  const contextIssue = repairOriginContextIssue(context)
  if (contextIssue) return { issue: contextIssue }
  if (!check || !['PASS','FAIL','INCOMPLETE'].includes(check.verdict) || !Array.isArray(check.findings) || !repairStrings(check.evidence) || !check.evidence.length || !repairStrings(check.blockers)) return { issue: 'invalid FixCheckV1 verdict, findings, evidence or blockers' }
  if (check.verdict === 'PASS' && (check.findings.length || check.blockers.length)) return { issue: 'PASS contradicts findings or blockers' }
  if (check.commits !== undefined && (!Array.isArray(check.commits) || !check.commits.length || check.commits.some(c => typeof c !== 'string' || !/^[0-9a-f]{7,40}$/i.test(c)) || new Set(check.commits).size !== check.commits.length)) return { issue: 'commits must name unique checked Git commit ids' }
  const origins = Object.fromEntries(FIX_ORIGIN_NAMES.map(k => [k, 0]))
  for (const finding of check.findings) {
    const issue = repairOriginIssue(finding, context)
    if (issue) return { issue }
    origins[finding.origin]++
  }
  return { origins }
}
