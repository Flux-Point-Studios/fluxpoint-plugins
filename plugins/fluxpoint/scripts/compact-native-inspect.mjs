// Inspect only after the pinned native ledger verifies the same exact bytes.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

try {
  const args = new Map();
  for (let index = 2; index < process.argv.length; index += 2) args.set(process.argv[index], process.argv[index + 1]);
  const native = JSON.parse(args.get('--native'));
  assert(Array.isArray(native) && native.length && native.every(value => typeof value === 'string'));
  const request = readFileSync(0);
  assert(request.length <= 4 * 1024 * 1024, 'native request exceeds 4 MiB');
  const result = spawnSync(native[0], [...native.slice(1), '--registry', args.get('--registry')], { input: request, encoding: 'utf8', timeout: 120000, maxBuffer: 4 * 1024 * 1024 });
  assert.equal(result.status, 0, 'native verifier did not complete');
  const verified = JSON.parse(result.stdout);
  assert(typeof verified.valid === 'boolean' && typeof verified.code === 'string');
  let metrics = null;
  if (verified.valid) {
    assert.equal(verified.code, 'verified');
    const require = createRequire(join(resolve(args.get('--modules')), '../package.json'));
    const L = await import(pathToFileURL(require.resolve('@midnight-ntwrk/ledger-v8')).href);
    const Z = await import(pathToFileURL(require.resolve('@midnight-ntwrk/zkir-v2')).href);
    const input = JSON.parse(request);
    const tx = L.Transaction.deserialize('signature', 'proof', 'binding', Buffer.from(input.transactionBase64, 'base64'));
    const calls = [...tx.intents.values()].flatMap(intent => intent.actions).filter(action => action instanceof L.ContractCall);
    assert.equal(calls.length, 1, 'native measurement needs exactly one call');
    const call = calls[0];
    const transcript = call.guaranteedTranscript;
    assert(transcript && !call.fallibleTranscript, 'measurement needs one guaranteed transcript');
    const gas = Object.fromEntries(Object.entries(transcript.gas).map(([key, value]) => {
      const number = Number(value); assert(Number.isSafeInteger(number) && number >= 0); return [key, number];
    }));
    metrics = { k: Z.Zkir.fromJson(readFileSync(args.get('--ir'), 'utf8')).getK(), proofBytes: call.proof.serialize().length, transcriptGas: gas };
  }
  console.log(JSON.stringify({ valid: verified.valid, code: verified.code, metrics }));
} catch (error) {
  console.error('compact-native-inspect: native inspection refused: ' + error.message);
  process.exitCode = 2;
}
