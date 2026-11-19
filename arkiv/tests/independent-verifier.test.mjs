/** Purpose: check independent Arkiv verification and bounded retry with the API offline.
 * Behavior: verify exact public fields and artifact bytes using a read-only SDK transport.
 * Refs: main_implementation plan.md P11.4; api design.md §18.2. Phase: P11.
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { createPublicClient } from '@arkiv-network/sdk';
import { rabdi } from '@arkiv-network/sdk/chains';
import { custom } from 'viem';
import { readWithBackoff } from '../src/read-backoff.mjs';
import { verifyArkivReference } from '../../verifier/verify-arkiv.mjs';

const key = '0x' + 'a'.repeat(64);
const owner = '0x' + 'b'.repeat(40);
const canonicalArtifact = Buffer.from('{"anomaly":0.125,"count":20}');
const intent = {kind:'result', owner, attributes:{
  taskId:'12345678-1234-4567-8123-0123456789ab',
  artifactDigest:'sha256:' + createHash('sha256').update(canonicalArtifact).digest('hex'),
  methodId:'orca.method/1.0', status:'inconclusive',
}};

test('test_orca_api_offline_external_verifier_reads', async () => {
  const methods = [];
  const client = createPublicClient({
    chain:rabdi({accessKey:''}),
    transport:custom({request:async ({method, params}) => {
      methods.push(method);
      if (method === 'eth_chainId') return '0x76edaf';
      if (method === 'arkiv_getBlockTiming') {
        return {current_block:'0x78', current_block_time:0, duration:2};
      }
      assert.equal(method,'arkiv_getEntity');
      assert.deepEqual(params,[key]);
      return {
        key, owner, creator:owner, expiresAt:'0x79', creationFlags:1,
        attributes:Object.entries({'arkiv.schema':'orca.result.v1',...intent.attributes})
          .map(([name,value]) => ({name,type:'str',value})),
      };
    }}, {retryCount:0}),
  });
  const expected = {intent, entityKey:key};
  const verified = await verifyArkivReference(client,{...expected,canonicalArtifact});
  assert.equal(verified.chain_status,'matches_expected_reference');
  assert.equal(verified.artifact_digest_status,'matches');
  assert.equal(verified.scientific_result_verified,false);
  assert.equal(verified.institution_verified,false);
  assert.deepEqual(methods,['eth_chainId','arkiv_getEntity','arkiv_getBlockTiming']);
  const altered = await verifyArkivReference(client,{...expected,canonicalArtifact:Buffer.from('{}')});
  assert.equal(altered.chain_status,'matches_expected_reference');
  assert.equal(altered.artifact_digest_status,'mismatch');
  const spoofed = {...intent,attributes:{...intent.attributes,methodId:'orca.other/1.0'}};
  const failed = await verifyArkivReference(client,{intent:spoofed,entityKey:key});
  assert.equal(failed.chain_status,'attributes_mismatch');
});

test('test_rate_limit_backoff', async () => {
  const rateLimit = Object.assign(new Error('rate limited'), {
    status:429, headers:new Headers({'Retry-After':'2'}),
  });
  const delays = [];
  let calls = 0;
  const result = await readWithBackoff(async () => {
    if (++calls < 3) throw new Error('RPC failed',{cause:rateLimit});
    return 'received';
  }, async ms => delays.push(ms));
  assert.equal(result,'received');
  assert.deepEqual(delays,[2000,2000]);
  const long = Object.assign(new Error('quota'), {
    status:429, headers:new Headers({'Retry-After':'60'}),
  });
  await assert.rejects(readWithBackoff(async () => {throw long;}, async () => assert.fail('Excessive hint')), /quota/);
  await assert.rejects(readWithBackoff(async () => {throw new Error('provider offline');},
    async () => assert.fail('Unrelated error must not retry')), /provider offline/);
});
