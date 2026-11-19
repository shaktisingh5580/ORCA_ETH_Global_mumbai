/** Purpose: verify P11 task and claim publication with separate wallet identities.
 * Behavior: exercise durable reservations, contention and read-only ambiguous-send recovery.
 * Refs: main_implementation plan.md P11.2; api design.md §5.2. Phase: P11.
 */
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { ENTITY_EVENTS_ABI, NoEntityFoundError } from '@arkiv-network/sdk';
import { encodeAbiParameters, encodeEventTopics } from 'viem';
import { publishPublicIntent, reconcilePublicIntent } from '../src/public-outbox.mjs';

const chain = {id:7794095};
const requester = '0x' + 'a'.repeat(40);
const provider = '0x' + 'b'.repeat(40);
const taskId = '12345678-1234-4567-8123-0123456789ab';
const taskKey = '0x' + '1'.repeat(64);
const claimKey = '0x' + '2'.repeat(64);
const transaction = '0x' + '3'.repeat(64);
const assignmentKey = '0x' + '5'.repeat(64);
const blockHash = '0x' + '6'.repeat(64);
const task = {
  kind:'task', owner:requester, lifetimeSeconds:60,
  attributes:{taskId, capability:'sst_reproduction', regionCode:'indian', status:'open',
    deadlineRef:new Date(Date.now() + 1200000).toISOString().replace(/\.\d{3}Z$/, 'Z')},
};
const claim = {
  kind:'claim', owner:provider, lifetimeSeconds:30, parent:{key:taskKey, owner:requester},
  attributes:{taskId, agentId:'87654321-1234-4567-8123-0123456789ab',
    status:'proposed', bindingDigest:'sha256:' + 'a'.repeat(64)},
};

async function setup(t) {
  const root = await mkdtemp(join(tmpdir(), 'orca-arkiv-public-'));
  t.after(() => rm(root, {recursive:true, force:true}));
  const rows = new Map();
  const sent = [];
  const logs = [];
  const reader = {
    getChainId:async () => chain.id,
    getBlockTiming:async () => ({currentBlock:100n}),
    getBlock:async () => ({hash:blockHash}),
    getTransactionReceipt:async () => ({
      status:'success', transactionHash:transaction, blockHash, blockNumber:99n, logs,
    }),
    getEntity:async key => {
      if (!rows.has(key)) throw new NoEntityFoundError(key);
      return rows.get(key);
    },
    predictEntityKeys:async ({owner}) => [{
      key:owner === requester ? (rows.has(taskKey) ? assignmentKey : taskKey) : claimKey, salt:10n,
    }],
  };
  function writer(owner, outcome = 'success') {
    return {account:{address:owner},chain,async createEntity(schema, attributes, options) {
      const kind = schema.name.split('.')[1];
      const reference = attributes.taskId ?? attributes.resultDigest.slice('sha256:'.length);
      const reserved = JSON.parse(await readFile(
        join(root, owner, `${kind}-${reference}.reserved.json`), 'utf8',
      ));
      assert.equal(reserved.intent.owner, owner);
      assert.equal(options.salt, 10n);
      assert.equal(options.flags.readonly, true);
      sent.push({owner,kind});
      if (outcome === 'lost_before_index') throw new Error('Indeterminate send');
      if (outcome === 'nonce_changed') {
        return {entityKey:'0x' + '4'.repeat(64), txHash:transaction};
      }
      logs.push({
        address:'0x4400000000000000000000000000000000000044', removed:false,
        topics:encodeEventTopics({
          abi:ENTITY_EVENTS_ABI, eventName:'EntityCreated', args:{entityKey:reserved.key, owner},
        }),
        data:encodeAbiParameters([{type:'uint64'}, {type:'uint8'}], [140n,1]),
      });
      rows.set(reserved.key, {
        $key:reserved.key, $owner:owner, $creator:owner, $expiresAt:140n,
        $creationFlags:{readonly:true, permissionlessExtension:false},
        $attributes:Object.fromEntries(Object.entries({'arkiv.schema':schema.name,...attributes})
          .map(([name,value]) => [name,{type:'str',value}])),
      });
      if (outcome === 'lost_after_index') throw new Error('Lost transaction response');
      return {entityKey:reserved.key, txHash:transaction};
    }};
  }
  return {root, reader, writer, rows, sent};
}

test('test_wallet_a_task_wallet_b_claim', async t => {
  const {root,reader,writer,sent} = await setup(t);
  await assert.rejects(publishPublicIntent(reader,writer(provider),root,claim), /current parent/);
  const published = await publishPublicIntent(reader,writer(requester),root,task);
  assert.equal(published.status, 'indexed');
  await assert.rejects(publishPublicIntent(reader,writer(requester),root,claim), /pinned Rabdi wallet/);
  await assert.rejects(publishPublicIntent(reader,writer(requester),root,{...claim,owner:requester}), /separate wallets/);
  const claimed = await publishPublicIntent(reader,writer(provider),root,claim);
  assert.equal(claimed.status, 'indexed');
  assert.equal(claimed.observation.owner, provider);
  assert.equal(claimed.observation.assignment_authorized, false);
  assert.deepEqual(sent,[{owner:requester,kind:'task'},{owner:provider,kind:'claim'}]);
});

test('test_duplicate_claim_reconcile', async t => {
  const {root,reader,writer,sent} = await setup(t);
  await publishPublicIntent(reader,writer(requester),root,task);
  const claimant = writer(provider);
  const results = await Promise.all([
    publishPublicIntent(reader,claimant,root,claim), publishPublicIntent(reader,claimant,root,claim),
  ]);
  assert.ok(results.some(result => result.status === 'indexed'));
  assert.equal((await reconcilePublicIntent(reader,root,claim)).entity_key, claimKey);
  assert.equal((await publishPublicIntent(reader,claimant,root,claim)).entity_key, claimKey);
  await assert.rejects(publishPublicIntent(reader,claimant,root,{...claim,lifetimeSeconds:28}), /durable reservation/);
  assert.equal(sent.filter(record => record.kind === 'claim').length, 1);
});

test('test_ambiguous_tx_no_blind_resubmit', async t => {
  const {root,reader,writer,sent} = await setup(t);
  await assert.rejects(publishPublicIntent(reader,writer(requester,'lost_before_index'),root,task), /Indeterminate/);
  const repeat = await publishPublicIntent(reader,writer(requester),root,task);
  assert.deepEqual(repeat,{status:'unresolved',entity_key:taskKey,observation:'not_found',tx_hash:null});
  const another = {...task,attributes:{...task.attributes,taskId:'12345678-1234-4567-8123-0123456789ac'}};
  assert.equal((await publishPublicIntent(reader,writer(requester),root,another)).status, 'writer_locked');
  assert.equal(sent.length, 1);
});

test('test_tx_timeout_unknown_reconcile', async t => {
  const {root,reader,writer,sent} = await setup(t);
  await assert.rejects(publishPublicIntent(reader,writer(requester,'lost_after_index'),root,task), /Lost transaction/);
  const result = await reconcilePublicIntent(reader,root,task);
  assert.equal(result.status,'indexed');
  assert.equal(result.tx_hash,null);
  assert.equal(result.entity_key,taskKey);
  assert.equal(sent.length,1);
});

test('test_nonce_collision_handled', async t => {
  const {root,reader,writer,sent} = await setup(t);
  const result = await publishPublicIntent(reader,writer(requester,'nonce_changed'),root,task);
  assert.deepEqual(result,{status:'manual_review',entity_key:taskKey,
    returned_entity_key:'0x' + '4'.repeat(64),tx_hash:transaction});
  assert.deepEqual(await reconcilePublicIntent(reader,root,task), result);
  assert.equal(sent.length,1);
});

test('a changed block hash leaves the transaction unresolved and does not trigger another write', async t => {
  const {root,reader,writer,sent} = await setup(t);
  const original = await publishPublicIntent(reader,writer(requester),root,task);
  assert.equal(original.receipt.block_hash, blockHash);
  reader.getBlock = async () => ({hash:'0x' + '7'.repeat(64)});
  const after = await reconcilePublicIntent(reader,root,task);
  assert.equal(after.status,'unresolved');
  assert.equal(after.receipt.status,'block_changed');
  assert.equal((await publishPublicIntent(reader,writer(requester),root,task)).status,'unresolved');
  assert.equal(sent.length,1);
});

test('test_arkiv_entity_expired_and_deleted', async t => {
  const {root,reader,writer,rows} = await setup(t);
  await publishPublicIntent(reader,writer(requester),root,task);
  rows.delete(taskKey);
  const deletedOrAbsent = await reconcilePublicIntent(reader,root,task);
  assert.equal(deletedOrAbsent.observation,'not_found');
  reader.getBlockTiming = async () => ({currentBlock:141n});
  const pastExpiry = await reconcilePublicIntent(reader,root,task);
  assert.equal(pastExpiry.observation,'absent_at_or_after_recorded_expiry');
  assert.equal(pastExpiry.status,'unresolved');
});

test('only the requester can publish a reference for the exact selected claim', async t => {
  const {root,reader,writer,sent} = await setup(t);
  await publishPublicIntent(reader,writer(requester),root,task);
  await publishPublicIntent(reader,writer(provider),root,claim);
  const assignment = {
    kind:'assignment', owner:requester, lifetimeSeconds:60,
    parent:{key:taskKey, owner:requester}, selectedClaim:{owner:provider, attributes:claim.attributes},
    attributes:{taskId, claimRef:claimKey, status:'selected', assignmentDigest:'sha256:'+'b'.repeat(64)},
  };
  const substituted = {...assignment, selectedClaim:{owner:provider,
    attributes:{...claim.attributes,bindingDigest:'sha256:'+'c'.repeat(64)}}};
  await assert.rejects(publishPublicIntent(reader,writer(requester),root,substituted), /current claim/);
  await assert.rejects(publishPublicIntent(reader,writer(provider),root,assignment), /pinned Rabdi wallet/);
  const result = await publishPublicIntent(reader,writer(requester),root,assignment);
  assert.equal(result.entity_key, assignmentKey);
  assert.equal(result.observation.attributes.claimRef, claimKey);
  assert.equal((await publishPublicIntent(reader,writer(requester),root,assignment)).entity_key, assignmentKey);
  assert.equal(sent.filter(write => write.kind === 'assignment').length, 1);
});

test('result and review intents preserve their owner and digest on readback', async t => {
  const {root,reader,writer,sent} = await setup(t);
  const artifactDigest = 'sha256:' + 'a'.repeat(64);
  const result = {kind:'result', owner:requester, lifetimeSeconds:600, attributes:{
    taskId, artifactDigest, methodId:'orca.native-seasonal-sst/hackathon-v1', status:'inconclusive',
  }};
  const review = {kind:'review', owner:provider, lifetimeSeconds:600, attributes:{
    resultDigest:artifactDigest, reviewDigest:'sha256:'+'b'.repeat(64),
    reviewerId:claim.attributes.agentId, verdict:'challenges',
  }};
  assert.deepEqual((await publishPublicIntent(reader,writer(requester),root,result)).observation.attributes, result.attributes);
  await assert.rejects(publishPublicIntent(reader,writer(requester),root,review), /pinned Rabdi wallet/);
  assert.deepEqual((await publishPublicIntent(reader,writer(provider),root,review)).observation.attributes, review.attributes);
  assert.equal(sent.length, 2);
});
