/** Purpose: independently observe P11 public entities through Arkiv's Rabdi SDK.
 * Behavior: pin the chain, compare owner and creator to the expected wallet, and check expiry.
 * Refs: main_implementation plan.md P11.1/P11.3/P11.4; api design.md §§3.12,4.7. Phase: P11.
 */
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createPublicClient, NoEntityFoundError } from '@arkiv-network/sdk';
import { rabdi } from '@arkiv-network/sdk/chains';
import { asc, eq, gt } from '@arkiv-network/sdk/query';
import { addr, key, str } from '@arkiv-network/sdk/schema';
import { http, isAddress, zeroAddress } from 'viem';
import { entityKey, publicId, publicSchema, schemaInventory, validatePublicAttributes } from './public-schema.mjs';
import { readWithBackoff } from './read-backoff.mjs';

export const RABDI_CHAIN_ID = 7794095;
const retryableReads = new Set([
  'eth_chainId', 'eth_call', 'eth_getTransactionReceipt', 'eth_getBlockByNumber',
  'arkiv_getBlockTiming', 'arkiv_getEntity', 'arkiv_query',
]);

export function createRabdiReader(accessKey) {
  const transport = http(undefined, {timeout:12000, retryCount:0});
  return createPublicClient({
    chain:rabdi({accessKey}),
    transport:parameters => {
      const connection = transport(parameters);
      return {...connection, request:args => retryableReads.has(args.method)
        ? readWithBackoff(() => connection.request(args))
        : connection.request(args)};
    },
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
    return {chain_id:RABDI_CHAIN_ID, entity_key:key, status:'not_found',
      observed_block:String(await readBlock(client))};
  }
  return observeRecord(kind, schema, row, key, owner, await readBlock(client));
}

export function queryTaskRecords(client, {kind, expectedOwner, taskId, limit, after}) {
  return queryPublicRecords(client, {kind, expectedOwner, field:'taskId', value:taskId, limit, after});
}

export async function queryPublicRecords(client, {kind, expectedOwner, field, value, limit = 25, after}) {
  const schema = publicSchema(kind);
  const owner = expectedWallet(expectedOwner);
  const validValue = field === 'taskId' ? publicId.test(value ?? '')
    : ['resultDigest', 'artifactDigest'].includes(field) && /^sha256:[0-9a-f]{64}$/.test(value ?? '');
  if (!validValue || !schema.columns[field]?.isIndexed ||
      !Number.isInteger(limit) || limit < 1 || limit > 50 ||
      (after !== undefined && !entityKey.test(after))) {
    throw new Error('Expected an indexed task or digest query and a valid page limit and cursor');
  }
  await checkChain(client);
  let query = client.select('*').where(
    eq('arkiv.schema', str(schema.name)), eq('$owner', addr(owner)),
    eq('$creator', addr(owner)), eq(field, str(value)),
  );
  if (after !== undefined) query = query.where(gt('$key', key(after)));
  const page = await query.orderBy(asc('$key')).limit(limit + 1).fetch();
  const head = await readBlock(client);
  if (page.blockNumber > head) throw new Error('Arkiv query block is ahead of the observed chain');
  let previousKey = after?.toLowerCase() ?? '';
  const records = page.entities.slice(0, limit).map(row => {
    if (!entityKey.test(row.$key ?? '')) throw new Error('Invalid Arkiv query entity key');
    const currentKey = row.$key.toLowerCase();
    if (currentKey <= previousKey) throw new Error('Arkiv query returned unordered entity keys');
    previousKey = currentKey;
    const observed = observeRecord(kind, schema, row, row.$key, owner, head);
    if (observed.status === 'observed' && observed.attributes[field] !== value) {
      throw new Error('Arkiv query returned a different reference');
    }
    return observed;
  });
  return {
    chain_id:RABDI_CHAIN_ID, index_block:String(page.blockNumber), observed_block:String(head),
    index_lag_blocks:String(head - page.blockNumber), truncated:page.entities.length > limit,
    next_cursor:page.entities.length > limit ? records.at(-1).entity_key : null, records,
  };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const [command, kind, reference, expectedOwner, after, ...extra] = process.argv.slice(2);
    if (!['preflight', 'read', 'query-task', 'query-result', 'query-review'].includes(command) || extra.length ||
        (['preflight', 'read'].includes(command) && after !== undefined) ||
        (command === 'preflight' && kind !== undefined)) throw new Error('Unexpected arguments');
    const client = createRabdiReader(process.env.ARKIV_ACCESS_KEY ?? '');
    const result = command === 'preflight'
      ? await preflight(client)
      : command === 'read'
        ? await readPublicRecord(client, {kind, key:reference, expectedOwner})
        : await queryPublicRecords(client, {
          kind, value:reference, expectedOwner, after,
          field:{'query-task':'taskId', 'query-result':'artifactDigest', 'query-review':'resultDigest'}[command],
        });
    console.log(JSON.stringify(result, null, 2));
  } catch {
    // SDK transport errors can contain the access key in the RPC URL.
    console.error('ARKIV_PUBLIC_READ_UNAVAILABLE_OR_INVALID');
    process.exitCode = 1;
  }
}
