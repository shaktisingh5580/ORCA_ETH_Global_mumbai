/** Purpose: independently observe P11 public entities through Arkiv's Rabdi SDK.
 * Behavior: pin the chain, compare owner and creator to the expected wallet, and check expiry.
 * Refs: main_implementation plan.md P11.1/P11.4; api design.md §§3.12,4.7. Phase: P11.
 */
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createPublicClient, NoEntityFoundError } from '@arkiv-network/sdk';
import { rabdi } from '@arkiv-network/sdk/chains';
import { eq } from '@arkiv-network/sdk/query';
import { addr, str } from '@arkiv-network/sdk/schema';
import { http, isAddress, zeroAddress } from 'viem';
import { entityKey, publicId, publicSchema, schemaInventory, validatePublicAttributes } from './public-schema.mjs';

export const RABDI_CHAIN_ID = 7794095;

export function createRabdiReader(accessKey) {
  return createPublicClient({
    chain:rabdi({accessKey}),
    transport:http(undefined, {timeout:12000, retryCount:0}),
  });
}

async function checkChain(client) {
  if (await client.getChainId() !== RABDI_CHAIN_ID) throw new Error('Expected Arkiv Rabdi');
}

async function readBlock(client) {
  const {currentBlock} = await client.getBlockTiming();
  if (typeof currentBlock !== 'bigint' || currentBlock < 0n) throw new Error('Invalid Rabdi block');
  return currentBlock;
}

export async function preflight(client) {
  await checkChain(client);
  return {chain_id:RABDI_CHAIN_ID, observed_block:String(await readBlock(client)), schemas:schemaInventory()};
}

function expectedWallet(value) {
  if (!isAddress(value ?? '', {strict:false}) || value.toLowerCase() === zeroAddress) {
    throw new Error('A valid expected owner is required');
  }
  return value.toLowerCase();
}

function observeRecord(kind, schema, row, key, owner, observedBlock) {
  if (typeof row.$expiresAt !== 'bigint' || row.$expiresAt < 1n) {
    throw new Error('Invalid public record expiry');
  }
  const observation = {
    chain_id:RABDI_CHAIN_ID, entity_key:key, observed_block:String(observedBlock),
    expires_at_block:String(row.$expiresAt),
  };
  if (observedBlock >= row.$expiresAt) return {...observation, status:'expired'};
  if (row.$key?.toLowerCase() !== key.toLowerCase() || row.$owner?.toLowerCase() !== owner ||
      row.$creator?.toLowerCase() !== owner || row.$creationFlags?.readonly !== true ||
      row.$creationFlags?.permissionlessExtension !== false) {
    throw new Error('Public record key, writer or creation flags differ from expectations');
  }
  const {'arkiv.schema':tag, ...attributes} = row.$attributes ?? {};
  if (tag?.type !== 'str' || tag.value !== schema.name ||
      Object.values(attributes).some(field => field?.type !== 'str')) {
    throw new Error('Unexpected ORCA public schema');
  }
  const values = Object.fromEntries(Object.entries(attributes).map(([name, field]) => [name, field.value]));
  return {
    ...observation, status:'observed', owner, schema:schema.name,
    attributes:validatePublicAttributes(kind, values),
    institution_verified:false, scientific_result_verified:false, assignment_authorized:false,
  };
}

export async function readPublicRecord(client, {kind, key, expectedOwner}) {
  const schema = publicSchema(kind);
  if (!entityKey.test(key ?? '')) throw new Error('A valid entity key is required');
  const owner = expectedWallet(expectedOwner);
  await checkChain(client);
  let row;
  try {
    row = await client.getEntity(key);
  } catch (error) {
    if (!(error instanceof NoEntityFoundError)) throw error;
    return {chain_id:RABDI_CHAIN_ID, entity_key:key, status:'not_found'};
  }
  return observeRecord(kind, schema, row, key, owner, await readBlock(client));
}

export async function queryTaskRecords(client, {kind, expectedOwner, taskId, limit = 25}) {
  const schema = publicSchema(kind);
  const owner = expectedWallet(expectedOwner);
  if (!schema.columns.taskId?.isIndexed || !publicId.test(taskId ?? '') ||
      !Number.isInteger(limit) || limit < 1 || limit > 50) {
    throw new Error('Expected a task-indexed schema, task ID and limit from 1 to 50');
  }
  await checkChain(client);
  const page = await client.select('*').where(
    eq('arkiv.schema', str(schema.name)), eq('$owner', addr(owner)),
    eq('$creator', addr(owner)), eq('taskId', str(taskId)),
  ).limit(limit + 1).fetch();
  const head = await readBlock(client);
  if (page.blockNumber > head) throw new Error('Arkiv query block is ahead of the observed chain');
  const records = page.entities.slice(0, limit).map(row => {
    if (!entityKey.test(row.$key ?? '')) throw new Error('Invalid Arkiv query entity key');
    const observed = observeRecord(kind, schema, row, row.$key, owner, head);
    if (observed.status === 'observed' && observed.attributes.taskId !== taskId) {
      throw new Error('Arkiv query returned a different task');
    }
    return observed;
  });
  return {
    chain_id:RABDI_CHAIN_ID, index_block:String(page.blockNumber), observed_block:String(head),
    index_lag_blocks:String(head - page.blockNumber), truncated:page.entities.length > limit,
    records,
  };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const [command, kind, key, expectedOwner, ...extra] = process.argv.slice(2);
    if (!['preflight', 'read', 'query-task'].includes(command) || extra.length ||
        (command === 'preflight' && kind !== undefined)) throw new Error('Unexpected arguments');
    const client = createRabdiReader(process.env.ARKIV_ACCESS_KEY ?? '');
    const result = command === 'preflight'
      ? await preflight(client)
      : command === 'read'
        ? await readPublicRecord(client, {kind, key, expectedOwner})
        : await queryTaskRecords(client, {kind, taskId:key, expectedOwner});
    console.log(JSON.stringify(result, null, 2));
  } catch {
    // SDK transport errors can contain the access key in the RPC URL.
    console.error('ARKIV_PUBLIC_READ_UNAVAILABLE_OR_INVALID');
    process.exitCode = 1;
  }
}
