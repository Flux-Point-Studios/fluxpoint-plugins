"""Proof-capable stubs for tests of other repair-loop invariants.

These tests have no repository at /campaign: this simulates the witness and
unrelated clean diff tier. repair-proof-test.py exercises actual Git archives,
raw reads, subprocesses and recorder corroboration separately.
"""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from repair_diff import compact_manifest, manifest_digest

PATCH = b'diff --git a/fixture b/fixture\n--- a/fixture\n+++ b/fixture\n@@ -0,0 +1 @@\n+fixture\n'


def wire(prefix):
    source = r'''
let proofFixtureToken=0;const proofFixtureSnapshots={};
const agent=async(p,o)=>{
 const label=String(o.label),pairs=[...p.matchAll(/--([a-z0-9]+) '([^']*)'/g)],a=Object.fromEntries(pairs.map(x=>[x[1],x[2]]));
 if(label.startsWith('repair-witness:')){
  if(label.includes(':read:'))return {status:'READ',base:a.base,patch:a.patch,item:a.item,round:Number(a.round),manifest:PATCH_MANIFEST,manifestSha256:'MANIFEST_SHA256'};
  if(label.includes(':materialize:')){const snapshot='/fixture/'+label,observation={command:'test fixture command',result:'executed and defended'};proofFixtureSnapshots[snapshot]=[{base:a.base,patchSha256:a.sha256,attack:observation,control:observation,reproduces:false}];return {status:'READY',base:a.base,patchSha256:a.sha256,snapshot,token:(++proofFixtureToken).toString(16).padStart(64,'0')};}
  return {status:'CLEANED',snapshot:a.snapshot,token:a.token,observations:proofFixtureSnapshots[a.snapshot]};
 }
 if(label.startsWith('repair-diff:')){
  if(label.includes(':refute:'))return {resolution:'REFUTED',attack:{command:'test attack',result:'defended'},control:{command:'test valid',result:'control passed'},contradiction:'independent executed contradiction'};
  const m=JSON.parse(p.split('PINNED PATCH SUBJECT: ')[1].split('\n')[0]);
  return {base:m.base,patchSha256:m.patchSha256,completed:true,evidence:['test fixture executed coverage and valid controls'],blockers:[],findings:[]};
 }
 const r=await rawAgent(p,o);
 if(r?.branch)r.patchSha256=PATCH_MANIFEST.patchSha256;
 if(r?.verdict==='PASS'&&p.includes('HARNESS PATCH MANIFEST')){
  const m=JSON.parse(p.split('HARNESS PATCH MANIFEST (derived from raw patch bytes, never your own inventory): ')[1].split('\n')[0]);
  const c=JSON.parse(p.split('ORIGIN REPLAY CONTEXT: ')[1].split('\n')[0]);
  const observation={command:'test fixture command',result:'executed and defended'};
  return {...r,targetClasses:c.targetClasses,siblings:c.targetClasses.flatMap(finding=>['field','encoding','caller','network','role','path'].map(axis=>({finding,invariant:'test invariant',axis,surface:'unlisted-'+axis,attack:observation,control:observation,outcome:'DEFENDED',reason:'independently executed fixture case'}))),diffCoverage:{patchSha256:m.patchSha256,hunks:m.hunks.map(h=>({id:h.id,surface:'other',attack:observation,control:observation}))}};
 }
 return r;
};
'''.replace('PATCH_MANIFEST', json.dumps(compact_manifest(PATCH))).replace('MANIFEST_SHA256', manifest_digest(compact_manifest(PATCH)))
    return (prefix.replace('const agent=', 'const rawAgent=', 1)
            .replace('_launch:{', "_replayLaunchNonce:'" + '1' * 64 + "',_launch:{", 1) + source)
