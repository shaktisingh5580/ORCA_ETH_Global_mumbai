/** Purpose: prepare the first wallet transaction for an ENSv2 agent namespace.
 * Behavior: verify the finalized parent, check current authority, and simulate deployment.
 * Refs: institutional phases F03.2 and the pinned Sepolia deployment artifacts.
 * Phase: F03.2. The controller signs in their wallet.
 */
import { fileURLToPath } from 'node:url';
import { createPublicClient, encodeFunctionData, http, isAddress, isAddressEqual, keccak256, toHex, zeroAddress } from 'viem';
import { sepolia } from 'viem/chains';
import { EnsReader, pin } from './ens.mjs';

const SET_SUBREGISTRY = 1n << 20n;
const REGISTRAR = 1n;
const SET_TEXT = 1n << 4n;

export async function prepareAgentResolver(client, name, controller, agentLabel) {
  if (!isAddress(controller) || !/^(analyst|reviewer)$/.test(agentLabel)) {
    throw new Error('Expected a controller address and analyst or reviewer label');
  }
  const reader = new EnsReader(client);
  const observation = await reader.inspect(name);
  const parent = observation.hierarchy.at(-1);
  if (observation.hierarchy.length !== 2 || !isAddressEqual(parent.owner, controller)) {
    throw new Error('Expected a finalized second-level parent owned by the controller');
  }
  const { ETHRegistry, VerifiableFactory, PermissionedResolverImpl } = pin.contracts;
  await reader.contract(VerifiableFactory.address, 'VerifiableFactory');
  await reader.contract(PermissionedResolverImpl.address, 'PermissionedResolverImpl');
  const latest = await client.getBlock({ blockTag: 'latest' });
  const state = await client.readContract({
    ...ETHRegistry, functionName: 'getState', args: [BigInt(keccak256(toHex(parent.label)))],
    blockNumber: latest.number,
  });
  if (state.status !== 2 || state.expiry <= latest.timestamp || !isAddressEqual(state.latestOwner, controller)) {
    throw new Error('Parent ownership changed');
  }
  const agentName = `${agentLabel}.${name}`;
  const salt = BigInt(keccak256(toHex(`orca.agent-resolver/1.0:${agentName}`)));
  const initialization = encodeFunctionData({
    abi: PermissionedResolverImpl.abi, functionName: 'initialize',
    args: [[{ account: controller, roleBitmap: SET_TEXT | (SET_TEXT << 128n) }], []],
  });
  const args = [PermissionedResolverImpl.address, salt, initialization];
  const { result } = await client.simulateContract({
    ...VerifiableFactory, functionName: 'deployProxy', args, account: controller, blockNumber: latest.number,
  });
  return {
    chain_id: pin.chain_id, name: agentName, controller, action: 'deploy_agent_resolver',
    predicted_resolver: result, finalized_block: observation.block_number, simulated_block: latest.number.toString(),
    transaction: {
      from: controller, to: VerifiableFactory.address, value: '0x0',
      data: encodeFunctionData({ abi: VerifiableFactory.abi, functionName: 'deployProxy', args }),
    },
  };
}

export async function prepareRegistry(client, name, controller, deploymentHash) {
  if (!isAddress(controller) || controller.toLowerCase() === zeroAddress) {
    throw new Error('A public controller wallet address is required');
  }
  const reader = new EnsReader(client);
  const observation = await reader.inspect(name);
  if (observation.hierarchy.length !== 2) throw new Error('Expected a second-level .eth parent');
  const parent = observation.hierarchy.at(-1);
  if (parent.owner.toLowerCase() !== controller.toLowerCase() || parent.subregistry !== zeroAddress) {
    throw new Error('Expected the controller to own a parent with no subregistry');
  }
  const { ETHRegistry, VerifiableFactory, UserRegistryImpl } = pin.contracts;
  await reader.contract(UserRegistryImpl.address, 'UserRegistryImpl');
  const label = BigInt(keccak256(toHex(parent.label)));
  const latest = await client.getBlock({ blockTag: 'latest' });
  const state = await client.readContract({
    ...ETHRegistry, functionName: 'getState', args: [label], blockNumber: latest.number,
  });
  const subregistry = await client.readContract({
    ...ETHRegistry, functionName: 'getSubregistry', args: [parent.label], blockNumber: latest.number,
  });
  const allowed = await client.readContract({
    ...ETHRegistry, functionName: 'hasRoles', args: [label, SET_SUBREGISTRY, controller],
    blockNumber: latest.number,
  });
  if (state.latestOwner.toLowerCase() !== controller.toLowerCase() ||
      state.status !== 2 || state.expiry <= latest.timestamp || subregistry !== zeroAddress || !allowed) {
    throw new Error('Current parent state does not allow this setup');
  }
  const salt = BigInt(keccak256(toHex(`orca.agent-registry/1.0:${name}`)));
  const initialization = encodeFunctionData({
    abi: UserRegistryImpl.abi, functionName: 'initialize',
    args: [[{ account: controller, roleBitmap: REGISTRAR }]],
  });
  const args = [UserRegistryImpl.address, salt, initialization];
  const data = encodeFunctionData({ abi: VerifiableFactory.abi, functionName: 'deployProxy', args });
  if (deploymentHash !== undefined) {
    if (!/^0x[0-9a-f]{64}$/i.test(deploymentHash)) throw new Error('Full deployment transaction hash required');
    const [deployment, receipt] = await Promise.all([
      client.getTransaction({ hash: deploymentHash }),
      client.getTransactionReceipt({ hash: deploymentHash }),
    ]);
    if (receipt.status !== 'success' || receipt.blockNumber > latest.number ||
        !deployment.to || !isAddressEqual(deployment.to, VerifiableFactory.address) ||
        !isAddressEqual(deployment.from, controller) || deployment.value !== 0n ||
        deployment.input !== data || deployment.blockHash !== receipt.blockHash) {
      throw new Error('Deployment receipt does not match the reviewed registry transaction');
    }
    const registry = await client.readContract({
      ...VerifiableFactory, functionName: 'predictProxyAddress', args: [controller, salt],
      blockNumber: latest.number,
    });
    const implementation = await client.readContract({
      ...VerifiableFactory, functionName: 'verifyContract', args: [registry], blockNumber: latest.number,
    });
    const registrar = await client.readContract({
      address: registry, abi: UserRegistryImpl.abi, functionName: 'hasRoles',
      args: [0n, REGISTRAR, controller], blockNumber: latest.number,
    });
    if (!isAddressEqual(implementation, UserRegistryImpl.address) || !registrar) {
      throw new Error('Registry implementation or controller role mismatch');
    }
    const attachArgs = [label, registry];
    await client.simulateContract({
      ...ETHRegistry, functionName: 'setSubregistry', args: attachArgs, account: controller,
      blockNumber: latest.number,
    });
    return {
      chain_id: pin.chain_id, name, controller, action: 'attach_agent_subregistry',
      deployment_hash: deploymentHash, registry, simulated_block: latest.number.toString(),
      transaction: {
        from: controller, to: ETHRegistry.address, value: '0x0',
        data: encodeFunctionData({ abi: ETHRegistry.abi, functionName: 'setSubregistry', args: attachArgs }),
      },
    };
  }
  const { result } = await client.simulateContract({
    ...VerifiableFactory, functionName: 'deployProxy', args, account: controller,
    blockNumber: latest.number,
  });
  return {
    chain_id: pin.chain_id, name, controller, action: 'deploy_agent_subregistry',
    finalized_block: observation.block_number, simulated_block: latest.number.toString(),
    predicted_registry: result,
    transaction: {
      from: controller, to: VerifiableFactory.address, value: '0x0',
      data,
    },
  };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const client = createPublicClient({
    chain: sepolia, ccipRead: false,
    transport: http('https://ethereum-sepolia-rpc.publicnode.com', { timeout: 12000, retryCount: 0 }),
  });
  try {
    console.log(JSON.stringify(await prepareRegistry(client, process.argv[2], process.argv[3], process.argv[4]), null, 2));
  } catch (error) {
    console.error(error.shortMessage ?? error.message);
    process.exitCode = 1;
  }
}
