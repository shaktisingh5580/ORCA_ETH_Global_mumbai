/** Purpose: verify ENS wallet proposals bind registration to the reviewed deployments.
 * Behavior: reject altered receipts, changed authority and resolver substitutions before simulation.
 * Refs: institutional phase F03.2 and pinned Sepolia ABIs. Phase: F03.2 test tooling.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { encodeFunctionData, keccak256, toHex, zeroAddress } from 'viem';
import { EnsReader, pin } from '../src/ens.mjs';
import { prepareAgentResolver } from '../src/ens-setup.mjs';

const CONTROLLER = '0x1111111111111111111111111111111111111111';
const REGISTRY = '0x2222222222222222222222222222222222222222';
const RESOLVER = '0x3333333333333333333333333333333333333333';
const PARENT_RESOLVER = '0x4444444444444444444444444444444444444444';
const HASH = '0x' + 'a'.repeat(64);
const BLOCK_HASH = '0x' + 'b'.repeat(64);
const PARENT = 'orca.eth';

function fixture(context, changes = {}) {
  const parent = {
    label: 'orca', owner: CONTROLLER, subregistry: REGISTRY,
    resolver: PARENT_RESOLVER, expiry: '5000', ...changes.parent,
  };
  context.mock.method(EnsReader.prototype, 'inspect', async () => ({
    block_number: '100', hierarchy: [{ label: 'eth' }, parent],
  }));
  context.mock.method(EnsReader.prototype, 'contract', async () => ({}));
  const implementation = pin.contracts.PermissionedResolverImpl;
  const salt = BigInt(keccak256(toHex('orca.agent-resolver/1.0:analyst.orca.eth')));
  const initialization = encodeFunctionData({
    abi: implementation.abi, functionName: 'initialize',
    args: [[{ account: CONTROLLER, roleBitmap: 16n | (16n << 128n) }], []],
  });
  const input = encodeFunctionData({
    abi: pin.contracts.VerifiableFactory.abi, functionName: 'deployProxy',
    args: [implementation.address, salt, initialization],
  });
  const simulations = [];
  const client = {
    getBlock: async () => ({ number: 120n, timestamp: 1000n }),
    getTransaction: async () => ({
      to: pin.contracts.VerifiableFactory.address, from: CONTROLLER, input,
      value: 0n, blockHash: BLOCK_HASH, ...changes.transaction,
    }),
    getTransactionReceipt: async () => ({
      status: 'success', blockNumber: 110n, blockHash: BLOCK_HASH, ...changes.receipt,
    }),
    readContract: async ({ functionName }) => {
      if (functionName === 'getState') return {
        status: 2, expiry: 5000n, latestOwner: CONTROLLER, ...changes.state,
      };
      if (functionName === 'getSubregistry') return changes.registry ?? REGISTRY;
      if (functionName === 'predictProxyAddress') return RESOLVER;
      if (functionName === 'verifyContract') return changes.implementation ?? implementation.address;
      throw new Error('Unexpected read ' + functionName);
    },
    simulateContract: async (request) => {
      simulations.push(request);
      return { result: RESOLVER };
    },
  };
  return { client, simulations };
}

test('registration uses the verified resolver, controlled registry and bounded expiry', async context => {
  const { client, simulations } = fixture(context, { state: { expiry: 4000n } });
  const proposal = await prepareAgentResolver(client, PARENT, CONTROLLER, 'analyst', HASH);
  assert.equal(proposal.action, 'register_agent_subname');
  assert.equal(proposal.name, 'analyst.orca.eth');
  assert.equal(proposal.transaction.to, REGISTRY);
  assert.equal(proposal.resolver, RESOLVER);
  assert.equal(proposal.expiry, '4000');
  assert.equal(proposal.transaction.value, '0x0');
  assert.equal(simulations.length, 1);
  assert.deepEqual(simulations[0].args, ['analyst', CONTROLLER, zeroAddress, RESOLVER, 0n, 4000n]);
});

for (const [label, changes] of [
  ['failed deployment', { receipt: { status: 'reverted' } }],
  ['changed input', { transaction: { input: '0x' } }],
  ['different sender', { transaction: { from: RESOLVER } }],
  ['different factory', { transaction: { to: RESOLVER } }],
  ['value transfer', { transaction: { value: 1n } }],
  ['receipt block mismatch', { receipt: { blockHash: HASH } }],
  ['changed registry', { registry: RESOLVER }],
  ['changed parent owner', { state: { latestOwner: RESOLVER } }],
  ['expired parent', { state: { expiry: 1000n } }],
  ['different implementation', { implementation: CONTROLLER }],
  ['unfinalized attachment', { parent: { subregistry: zeroAddress } }],
]) {
  test('rejects ' + label + ' before requesting a signature', async context => {
    const { client, simulations } = fixture(context, changes);
    await assert.rejects(prepareAgentResolver(client, PARENT, CONTROLLER, 'analyst', HASH));
    assert.equal(simulations.length, 0);
  });
}
