/** Purpose: publish consented P11 task and claim references with one durable intent per writer.
 * Behavior: bind the wallet, reserve the predicted key before send and reconcile by reading only.
 * Refs: main_implementation plan.md P11.2; api design.md §§3.12,4.7,5.2. Phase: P11.
 */
import { mkdir, open, readFile, unlink } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { createWalletClient } from '@arkiv-network/sdk';
import { rabdi } from '@arkiv-network/sdk/chains';
import { http, isAddress, zeroAddress } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { createRabdiReader, preflight, RABDI_CHAIN_ID, readPublicRecord } from './public-client.mjs';
import { entityKey, publicationOptions, publicSchema, validatePublicAttributes } from './public-schema.mjs';

function address(value) {
  if (typeof value !== 'string' || !isAddress(value, {strict:false}) || value.toLowerCase() === zeroAddress) {
    throw new Error('Expected a nonzero writer wallet');
  }
  return value.toLowerCase();
}

function intentFor(input) {
  const {kind, lifetimeSeconds} = input;
  if (kind !== 'task' && kind !== 'claim') throw new Error('Expected a task or claim intent');
  const owner = address(input.owner);
  const attributes = validatePublicAttributes(kind, input.attributes);
  publicationOptions(kind, lifetimeSeconds);
  let parent = null;
  if (kind === 'claim') {
    if (!entityKey.test(input.parent?.key ?? '')) throw new Error('Expected a public task key');
    parent = {key:input.parent.key.toLowerCase(), owner:address(input.parent.owner)};
    if (parent.owner === owner) throw new Error('Task and claim require separate wallets');
  }
  return {kind, owner, attributes, lifetimeSeconds, parent};
}

function pathsFor(root, intent) {
  const folder = resolve(root, intent.owner);
  const basename = `${intent.kind}-${intent.attributes.taskId}`;
  return {
    folder, lock:join(folder, 'writer.lock'),
    reservation:join(folder, basename + '.reserved.json'),
    submitted:join(folder, basename + '.submitted.json'),
  };
}

async function readOptional(path) {
  try { return JSON.parse(await readFile(path, 'utf8')); }
  catch (error) {
    if (error.code !== 'ENOENT') throw error;
    return null;
  }
}

async function reserve(path, value) {
  const descriptor = await open(path, 'wx', 0o600);
  try {
    await descriptor.writeFile(JSON.stringify(value));
    await descriptor.sync();
  } finally {
    await descriptor.close();
  }
}

async function reconcile(reader, paths, intent, reservation) {
  if (!isDeepStrictEqual(reservation.intent, intent) ||
      reservation.chain_id !== RABDI_CHAIN_ID || !entityKey.test(reservation.key ?? '')) {
    throw new Error('Publication differs from its durable reservation');
  }
  const submitted = await readOptional(paths.submitted);
  if (submitted && (!entityKey.test(submitted.tx_hash ?? '') || submitted.key !== reservation.key)) {
    return {
      status:'manual_review', entity_key:reservation.key,
      returned_entity_key:entityKey.test(submitted.key ?? '') ? submitted.key : null,
      tx_hash:entityKey.test(submitted.tx_hash ?? '') ? submitted.tx_hash : null,
    };
  }
  const observed = await readPublicRecord(reader, {
    kind:intent.kind, key:reservation.key, expectedOwner:intent.owner,
  });
  if (observed.status !== 'observed') {
    return {status:'unresolved', entity_key:reservation.key, observation:observed.status,
      tx_hash:submitted?.tx_hash ?? null};
  }
  if (!isDeepStrictEqual(observed.attributes, intent.attributes)) {
    throw new Error('Reserved entity differs from its public intent');
  }
  return {status:'indexed', entity_key:reservation.key, tx_hash:submitted?.tx_hash ?? null, observation:observed};
}

export async function reconcilePublicIntent(reader, root, input) {
  const intent = intentFor(input);
  const paths = pathsFor(root, intent);
  const reservation = await readOptional(paths.reservation);
  if (!reservation) return {status:'unreserved'};
  return reconcile(reader, paths, intent, reservation);
}

/** The caller must authorize disclosure and supply a pinned, eligible wallet binding. */
export async function publishPublicIntent(reader, writer, root, input) {
  const intent = intentFor(input);
  if (address(writer.account?.address) !== intent.owner || writer.chain?.id !== RABDI_CHAIN_ID) {
    throw new Error('The configured writer differs from the pinned Rabdi wallet');
  }
  const paths = pathsFor(root, intent);
  const existing = await readOptional(paths.reservation);
  if (existing) return reconcile(reader, paths, intent, existing);

  await mkdir(paths.folder, {recursive:true, mode:0o700});
  try { await reserve(paths.lock, {kind:intent.kind, task_id:intent.attributes.taskId}); }
  catch (error) {
    if (error.code !== 'EEXIST') throw error;
    return {status:'writer_locked'};
  }
  let release = true;
  try {
    const duplicate = await readOptional(paths.reservation);
    if (duplicate) return await reconcile(reader, paths, intent, duplicate);
    await preflight(reader);
    const deadline = intent.parent
      ? await claimDeadline(reader, intent)
      : intent.attributes.deadlineRef;
    if (Date.parse(deadline) <= Date.now() + intent.lifetimeSeconds * 1000) {
      throw new Error('Publication lifetime must fit the remaining task deadline');
    }
    const [predicted] = await reader.predictEntityKeys({owner:intent.owner, count:1});
    if (!entityKey.test(predicted?.key ?? '') || typeof predicted.salt !== 'bigint') {
      throw new Error('Invalid predicted entity key');
    }
    const reservation = {chain_id:RABDI_CHAIN_ID, intent, key:predicted.key.toLowerCase(),
      salt:String(predicted.salt)};
    // Any interruption from this point leaves a lock and allows readback only.
    release = false;
    await reserve(paths.reservation, reservation);
    const created = await writer.createEntity(publicSchema(intent.kind), intent.attributes, {
      ...publicationOptions(intent.kind, intent.lifetimeSeconds), salt:predicted.salt,
    });
    await reserve(paths.submitted, {key:created.entityKey?.toLowerCase(), tx_hash:created.txHash});
    release = entityKey.test(created.txHash ?? '') && created.entityKey?.toLowerCase() === reservation.key;
    return await reconcile(reader, paths, intent, reservation);
  } finally {
    if (release) await unlink(paths.lock);
  }
}

async function claimDeadline(reader, intent) {
  const task = await readPublicRecord(reader, {
    kind:'task', key:intent.parent.key, expectedOwner:intent.parent.owner,
  });
  if (task.status !== 'observed' || task.attributes.taskId !== intent.attributes.taskId) {
    throw new Error('Claim requires a current parent task from the expected requester');
  }
  return task.attributes.deadlineRef;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const [command, root, approval, ...extra] = process.argv.slice(2);
    if (!root || extra.length ||
        !(command === 'reconcile' && approval === undefined ||
          command === 'publish' && approval === '--approve-public')) {
      throw new Error('Expected a public publication or read-only reconciliation command');
    }
    const chunks = [];
    let size = 0;
    for await (const chunk of process.stdin) {
      size += chunk.length;
      if (size > 8192) throw new Error('Public intent is oversized');
      chunks.push(chunk);
    }
    const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const accessKey = process.env.ARKIV_ACCESS_KEY ?? '';
    const reader = createRabdiReader(accessKey);
    let result;
    if (command === 'publish') {
      const writer = createWalletClient({
        chain:rabdi({accessKey}), transport:http(undefined, {timeout:12000, retryCount:0}),
        account:privateKeyToAccount(process.env.ARKIV_PRIVATE_KEY ?? ''),
      });
      result = await publishPublicIntent(reader, writer, root, input);
    } else {
      result = await reconcilePublicIntent(reader, root, input);
    }
    console.log(JSON.stringify(result, null, 2));
  } catch {
    // Transport errors can embed credential-bearing URLs or serialized transaction data.
    console.error('ARKIV_PUBLIC_INTENT_FAILED_CHECK_RESERVATION');
    process.exitCode = 1;
  }
}
