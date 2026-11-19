/** Purpose: exercise the P11 schemas and Rabdi reader through the pinned SDK.
 * Behavior: check disclosure, indexed fields, ownership and expiration without chain writes.
 * Refs: main_implementation plan.md P11.1; api design.md §4.7. Phase: P11.
 */
import assert from 'node:assert/strict';
import test from 'node:test';
import { createPublicClient } from '@arkiv-network/sdk';
import { rabdi } from '@arkiv-network/sdk/chains';
import { custom } from 'viem';
import { preflight, queryTaskRecords, readPublicRecord } from '../src/public-client.mjs';
import { publicationOptions, publicSchema, schemaInventory, validatePublicAttributes } from '../src/public-schema.mjs';

const owner = '0x' + 'a'.repeat(40);
const other = '0x' + 'b'.repeat(40);
const key = '0x' + '1'.repeat(64);
const task = {
  taskId:'12345678-1234-4567-8123-0123456789ab',
  capability:'sst_reproduction',
  regionCode:'indian',
  status:'open',
  deadlineRef:'2026-11-22T12:00:00Z',
};
const request = {kind:'task', key, expectedOwner:owner};

function transport({currentBlock = 120, chainId = 7794095, missing = false, overrides = {}, after} = {}) {
  const calls = [];
  const rpc = {
    key, owner, creator:owner, expiresAt:'0x79',
    creationFlags:{readonly:true, permissionlessExtension:false},
    attributes:Object.entries({'arkiv.schema':'orca.task.v1', ...task})
      .map(([name, value]) => ({name, type:'str', value})),
    ...overrides,
  };
  const client = createPublicClient({
    chain:rabdi({accessKey:''}),
    transport:custom({request:async ({method, params}) => {
      calls.push(method);
      if (method === 'eth_chainId') return '0x' + chainId.toString(16);
      if (method === 'arkiv_getBlockTiming') {
        return {current_block:'0x' + currentBlock.toString(16), current_block_time:0, duration:2};
      }
      if (method === 'arkiv_query') {
        const [query, options] = params;
        for (const filter of ['arkiv.schema', 'orca.task.v1', '$owner', '$creator', 'taskId', task.taskId]) {
          assert.ok(query.includes(filter), query);
        }
        assert.ok(query.toLowerCase().includes(owner), query);
        assert.match(query, /ORDER BY \$key(?::key)? ASC/);
        if (after) assert.ok(query.includes(`$key > key(${after})`), query);
        assert.equal(options.limit, '0x2');
        return {
          data:after ? [{...rpc, key:'0x' + '2'.repeat(64)}] : [rpc, {...rpc, key:'0x' + '2'.repeat(64)}],
          blockNumber:'0x76',
        };
      }
      assert.equal(method, 'arkiv_getEntity');
      assert.deepEqual(params, [key]);
      return missing ? null : rpc;
    }}, {retryCount:0}),
  });
  return {client, calls};
}

test('test_size_limits', () => {
  assert.deepEqual(validatePublicAttributes('task', task), task);
  assert.throws(() => validatePublicAttributes('task', {...task, capability:'é'.repeat(65)}), /capability/);
  assert.throws(() => validatePublicAttributes('task', {...task, deadlineRef:'2026-11-31T12:00:00Z'}), /deadlineRef/);
});

test('test_index_fields', () => {
  const inventory = schemaInventory();
  assert.equal(inventory.length, 6);
  for (const {schema, indexes, attributes} of inventory) {
    const kind = schema.split('.')[1];
    const columns = publicSchema(kind).columns;
    assert.deepEqual(Object.keys(columns), attributes);
    assert.deepEqual(attributes.filter(name => columns[name].isIndexed).sort(), [...indexes].sort());
  }
  assert.deepEqual(inventory.find(item => item.schema === 'orca.task.v1').indexes,
    ['taskId', 'capability', 'regionCode', 'status']);
});

test('test_schema_tag_not_auth', async () => {
  const observed = await readPublicRecord(transport().client, request);
  assert.deepEqual(observed.attributes, task);
  assert.equal(observed.assignment_authorized, false);
  assert.equal(observed.institution_verified, false);
  assert.equal(observed.scientific_result_verified, false);
  await assert.rejects(readPublicRecord(transport({overrides:{owner:other}}).client, request), /writer/);
  await assert.rejects(readPublicRecord(transport({overrides:{creator:other}}).client, request), /writer/);
});

test('test_expiry_and_readonly_not_eternal', async () => {
  assert.deepEqual(publicationOptions('task', 3600).flags, {readonly:true});
  assert.throws(() => publicationOptions('task', 3 * 86400 + 2), /lifetime/);
  const expired = await readPublicRecord(transport({currentBlock:121}).client, request);
  assert.equal(expired.status, 'expired');
  assert.equal(expired.expires_at_block, '121');
  assert.equal(expired.attributes, undefined);
  const absent = await readPublicRecord(transport({missing:true}).client, request);
  assert.equal(absent.status, 'not_found');
  const editable = transport({overrides:{creationFlags:0}});
  await assert.rejects(readPublicRecord(editable.client, request), /creation flags/);
});

test('test_no_sensitive_geolocation', () => {
  for (const attribute of ['latitude', 'coordinates', 'userQuestion', 'datasetUrl']) {
    assert.throws(() => validatePublicAttributes('task', {...task, [attribute]:'1,2'}), /Unexpected/);
  }
  assert.throws(() => validatePublicAttributes('task', {...task, regionCode:'19.5041,63.0388'}), /regionCode/);
});

test('preflight checks the RPC network before reading its block', async () => {
  assert.equal((await preflight(transport().client)).observed_block, '120');
  const wrong = transport({chainId:1});
  await assert.rejects(preflight(wrong.client), /Expected Arkiv Rabdi/);
  assert.deepEqual(wrong.calls, ['eth_chainId']);
});

test('test_index_lag_visible', async () => {
  const params = {kind:'task', taskId:task.taskId, expectedOwner:owner, limit:1};
  const page = await queryTaskRecords(transport().client, params);
  assert.equal(page.index_block, '118');
  assert.equal(page.index_lag_blocks, '2');
  assert.equal(page.truncated, true);
  assert.equal(page.records.length, 1);
  assert.deepEqual(page.records[0].attributes, task);
  const expired = await queryTaskRecords(transport({currentBlock:121}).client, params);
  assert.equal(expired.records[0].status, 'expired');
  await assert.rejects(queryTaskRecords(transport({overrides:{owner:other}}).client, params), /writer/);
  await assert.rejects(queryTaskRecords(transport({currentBlock:117}).client, params), /ahead/);
  const invalid = transport();
  await assert.rejects(queryTaskRecords(invalid.client, {...params, kind:'review'}), /indexed task or digest/);
  assert.deepEqual(invalid.calls, []);
});

test('test_paginate_and_expire', async () => {
  const params = {kind:'task', taskId:task.taskId, expectedOwner:owner, limit:1};
  const first = await queryTaskRecords(transport().client, params);
  assert.equal(first.next_cursor, key);
  const second = await queryTaskRecords(transport({after:key, currentBlock:121}).client, {...params, after:key});
  assert.equal(second.records.length, 1);
  assert.notEqual(second.records[0].entity_key, key);
  assert.equal(second.records[0].status, 'expired');
  assert.equal(second.next_cursor, null);
});
