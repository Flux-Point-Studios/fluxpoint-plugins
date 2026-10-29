// Synthetic local proof fixture. No network transaction or real user witness.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';

const out = resolve(process.argv[2]);
const modules = process.env.FPL_COMPACT_NODE_MODULES;
assert(modules, 'FPL_COMPACT_NODE_MODULES must name the pinned fixture node_modules');
if (!existsSync(join(out, 'node_modules'))) symlinkSync(resolve(modules), join(out, 'node_modules'), process.platform === 'win32' ? 'junction' : 'dir');
const require = createRequire(join(resolve(modules), '../package.json'));
const load = name => import(pathToFileURL(require.resolve(name)).href);
const rt = await load('@midnight-ntwrk/compact-runtime');
const L = await load('@midnight-ntwrk/ledger-v8');
const Z = await load('@midnight-ntwrk/zkir-v2');
const { Contract } = await import(pathToFileURL(join(out, 'contract/index.js')).href);
const bytes = path => new Uint8Array(readFileSync(join(out, path)));
const syntheticSecret = new Uint8Array(32).fill(19);
const contextDigest = new Uint8Array(32).fill(27);
const commitment = new Uint8Array(createHash('sha256').update(syntheticSecret).update(contextDigest).digest());
const witnesses = secret => ({ capability: ({ privateState }) => [privateState, secret] });
const contract = new Contract(witnesses(syntheticSecret));
const constructed = contract.initialState(rt.createConstructorContext(undefined, '0'.repeat(64)));
const initial = L.ContractState.deserialize(constructed.currentContractState.serialize());
const op = new L.ContractOperation(); op.verifierKey = bytes('keys/check.verifier');
initial.setOperation('check', op);
initial.maintenanceAuthority = new L.ContractMaintenanceAuthority([], 1, 0n);
writeFileSync(join(out, 'initial-state.bin'), initial.serialize());
const circuitId = 'fluxpoint-compact-native-fixture';
writeFileSync(join(out, 'registry.json'), JSON.stringify({ version: 1, circuits: { [circuitId]: {
  entryPoint: 'check', verifierKeyPath: 'keys/check.verifier', initialContractStatePath: 'initial-state.bin',
} } }));
const paramsDir = process.env.FPL_COMPACT_PARAMS;
assert(paramsDir, 'FPL_COMPACT_PARAMS must name pinned parameters');
const provider = Z.provingProvider({
  async lookupKey(key) {
    assert.equal(key, 'check');
    return { proverKey: bytes('keys/check.prover'), verifierKey: bytes('keys/check.verifier'), ir: bytes('zkir/check.bzkir') };
  },
  async getParams(k) { return new Uint8Array(readFileSync(join(paramsDir, `bls_midnight_2p${k}`))); },
});
const now = Math.floor(Date.now() / 1000);
const ttl = new Date((now + 1800) * 1000);
const deploy = new L.ContractDeploy(initial);
const deployTx = (await L.Transaction.fromParts('preprod', undefined, undefined, L.Intent.new(ttl).addDeploy(deploy)).prove(provider, L.CostModel.initialCostModel())).bind();
let state = L.LedgerState.blank('preprod');
const strict = new L.WellFormedStrictness(); strict.enforceBalancing = false;
const block = { secondsSinceEpoch: BigInt(now), secondsSinceEpochErr: 0, parentBlockHash: '0'.repeat(64), lastBlockTime: BigInt(now - 1) };
const [next, result] = state.apply(deployTx.wellFormed(state, strict, new Date(now * 1000)), new L.TransactionContext(state, block));
assert.equal(result.type, 'success'); state = next;
const referenceStateBase64 = Buffer.from(state.serialize()).toString('base64');
let gas;
async function prove(secret) {
  const current = new Contract(witnesses(secret));
  const context = rt.createCircuitContext(deploy.address, '00'.repeat(32), rt.ContractState.deserialize(initial.serialize()), undefined);
  const executed = current.circuits.check(context, commitment, contextDigest);
  const query = context.currentQueryContext;
  const ledgerQuery = new L.QueryContext(new L.ChargedState(L.StateValue.decode(query.state.state.encode())), deploy.address);
  ledgerQuery.block = query.block; ledgerQuery.effects = query.effects;
  const randomness = L.communicationCommitmentRandomness();
  const binding = L.communicationCommitment(executed.proofData.input, executed.proofData.output, randomness);
  const [guaranteed, fallible] = L.partitionTranscripts([new L.PreTranscript(ledgerQuery, executed.proofData.publicTranscript, binding)], L.LedgerParameters.initialParameters())[0];
  assert(guaranteed && !fallible);
  gas = Object.fromEntries(Object.entries(guaranteed.gas).map(([key, value]) => [key, Number(value)]));
  const call = new L.ContractCallPrototype(deploy.address, 'check', initial.operation('check'), guaranteed, undefined,
    executed.proofData.privateTranscriptOutputs, executed.proofData.input, executed.proofData.output, randomness, 'check');
  const tx = L.Transaction.fromParts('preprod', undefined, undefined, L.Intent.new(ttl).addCall(call));
  return (await tx.prove(provider, L.CostModel.initialCostModel())).bind();
}
const control = (id, tx) => ({ id, circuit: 'check', request: { circuitId, transactionBase64: Buffer.from(tx.serialize()).toString('base64'), referenceStateBase64 } });
const controls = [];
let valid;
try { valid = await prove(syntheticSecret); controls.push(control('genuine', valid)); }
catch (error) {
  if (!String(error).includes('capability mismatch')) throw error;
  controls.push({ id: 'genuine', circuit: 'check', proverRefused: true });
}
if (valid) {
  const transaction = Buffer.from(valid.serialize());
  const call = [...valid.intents.values()][0].actions[0];
  const tagged = Buffer.from(call.proof.serialize());
  const body = tagged.subarray(tagged.indexOf(':', tagged.indexOf(':') + 1) + 1);
  const start = transaction.indexOf(body);
  assert(start > 0 && transaction.indexOf(body, start + 1) === -1);
  transaction[start + Math.floor(body.length / 2)] ^= 1;
  controls.push({ id: 'corrupted', circuit: 'check', request: { circuitId, transactionBase64: transaction.toString('base64'), referenceStateBase64 } });
} else {
  controls.push({ id: 'corrupted', circuit: 'check', proverRefused: true });
}
try { controls.push(control('wrong-secret', await prove(new Uint8Array(32).fill(20)))); }
catch (error) {
  if (!String(error).includes('capability mismatch')) throw error;
  controls.push({ id: 'wrong-secret', circuit: 'check', proverRefused: true });
}
console.log(JSON.stringify({ version: 1, complete: true, registry: 'registry.json', controls,
  witnesses: Object.fromEntries(JSON.parse(readFileSync(join(out, 'compiler/contract-info.json'), 'utf8')).witnesses.map(witness => {
    assert.equal(witness.name, 'capability'); return [witness.name, [Buffer.from(syntheticSecret).toString('base64')]];
  })) }));
