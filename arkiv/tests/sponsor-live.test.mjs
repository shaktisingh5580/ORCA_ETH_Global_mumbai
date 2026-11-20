/** Purpose: verify operator-supplied ENS and Arkiv receipts against public testnets.
 * Behavior: accept only readback evidence and require fresh chain observations for live cases.
 * Refs: main_implementation plan.md P14.4; api design.md §17. Phase: P14.
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import test from 'node:test';
import { createPublicClient, encodeFunctionData, getAddress, http } from 'viem';
import { sepolia } from 'viem/chains';
import { EnsReader, pin } from '../src/ens.mjs';
import { createRabdiReader, queryTaskRecords } from '../src/public-client.mjs';
import { verifyArkivReference } from '../../verifier/verify-arkiv.mjs';

const evidenceFile = process.env.ORCA_SPONSOR_LIVE_EVIDENCE;
const config = evidenceFile ? JSON.parse(await readFile(evidenceFile, 'utf8')) : {};
const transactionHash = /^0x[0-9a-f]{64}$/i;

function liveCase(name, section, check) {
  test(name, {skip: config[section] ? false : `Supply authorized ${section} evidence`}, async () => {
    try { await check(config[section]); }
    catch (error) {
      // SDK transport exceptions can include the configured RPC access key.
      if (error instanceof assert.AssertionError) throw error;
      throw new Error('Live sponsor readback unavailable or invalid');
    }
  });
}

liveCase('test_live_ens_permission_revoke_receipts', 'ens', async (evidence) => {
  const {name, siblingName, delegate, grantTx, revokeTx} = evidence;
  assert.match(grantTx, transactionHash);
  assert.match(revokeTx, transactionHash);
  assert.notEqual(grantTx.toLowerCase(), revokeTx.toLowerCase());
  const client = createPublicClient({
    chain: sepolia, transport: http(undefined, {timeout: 12000, retryCount: 0}), ccipRead: false,
  });
  const current = await new EnsReader(client).endpointPermissions(name, siblingName, delegate);
  assert.equal(current.status, 'DISTINCT_RESOLVERS');
  assert.equal(current.chain_id, 11155111);
  assert.ok(current.names.every(item => !item.endpoint_text_role && !item.root_text_role));
  const [target, sibling] = current.names;
  const abi = pin.contracts.PermissionedResolverImpl.abi;
  const role = 1n << 4n;
  const resource = BigInt(current.resource);
  const setter = encodeFunctionData({abi, functionName: 'setText', args: ['0x', current.key, '']});
  const account = getAddress(delegate);
  const calls = [
    encodeFunctionData({abi, functionName: 'grantSetterRoles', args: [setter, account]}),
    encodeFunctionData({abi, functionName: 'revokeRoles', args: [resource, role, account]}),
  ];
  async function verifyReceipt(hash, input) {
    const receipt = await client.getTransactionReceipt({hash});
    const tx = await client.getTransaction({hash});
    const block = await client.getBlock({blockNumber: receipt.blockNumber});
    assert.equal(receipt.status, 'success');
    assert.equal(block.hash, receipt.blockHash);
    assert.ok(receipt.blockNumber <= BigInt(current.block_number));
    assert.equal(getAddress(tx.from), getAddress(target.controller));
    assert.equal(getAddress(tx.to), getAddress(target.resolver));
    assert.equal(tx.input, input);
    assert.equal(tx.value, 0n);
    return receipt.blockNumber;
  }
  const grantBlock = await verifyReceipt(grantTx, calls[0]);
  const revokeBlock = await verifyReceipt(revokeTx, calls[1]);
  assert.ok(grantBlock < revokeBlock);
  for (const [blockNumber, granted] of [[grantBlock, true], [revokeBlock, false]]) {
    for (const [item, expected] of [[target, granted], [sibling, false]]) {
      const common = {abi, address: item.resolver, blockNumber, ccipRead: false};
      assert.equal(await client.readContract({
        ...common, functionName: 'hasRoles', args: [resource, role, account],
      }), expected);
      assert.equal(await client.readContract({
        ...common, functionName: 'hasRootRoles', args: [role, account],
      }), false);
    }
  }
});

liveCase('test_live_arkiv_write_filtered_query', 'arkiv', async ({intent, entityKey, txHash}) => {
  assert.match(txHash, transactionHash);
  const reader = createRabdiReader(process.env.ARKIV_ACCESS_KEY ?? '');
  const observed = await verifyArkivReference(reader, {intent, entityKey, txHash});
  assert.equal(observed.chain_status, 'matches_expected_reference');
  assert.equal(observed.creation_receipt.status, 'observed');
  const page = await queryTaskRecords(reader, {
    kind: intent.kind, expectedOwner: intent.owner, taskId: intent.attributes.taskId,
  });
  assert.equal(page.chain_id, 7794095);
  assert.ok(page.records.some(row => row.status === 'observed' &&
    row.entity_key.toLowerCase() === entityKey.toLowerCase()));
});

liveCase('test_external_verifier_orca_offline', 'arkiv', async (evidence) => {
  const {intent, entityKey, txHash, canonicalArtifact} = evidence;
  assert.match(txHash, transactionHash);
  const bytes = await readFile(resolve(dirname(evidenceFile), canonicalArtifact));
  const reader = createRabdiReader(process.env.ARKIV_ACCESS_KEY ?? '');
  const report = await verifyArkivReference(reader, {intent, entityKey, txHash, canonicalArtifact: bytes});
  assert.equal(report.chain_status, 'matches_expected_reference');
  assert.equal(report.artifact_digest_status, 'matches');
  assert.equal(report.institution_verified, false);
  assert.equal(report.scientific_result_verified, false);
});
