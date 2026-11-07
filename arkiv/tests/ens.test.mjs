/** Purpose: exercise ENSv2 read safety without a wallet or public RPC.
 * Behavior: test hostile names, bytecode/proxy/interface failures and hierarchy observations.
 * Refs: institutional phases F03.1; contracts/ens/sepolia.json; Phase: F03.1.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { encodeAbiParameters, parseAbiParameters } from 'viem';
import { namehash } from 'viem/ens';
import { EnsReader, checkedName, checkRuntime, pin, RECORD_KEYS } from '../src/ens.mjs';

const OWNER='0x1111111111111111111111111111111111111111';
const RESOLVER='0x2222222222222222222222222222222222222222';
const ZERO='0x0000000000000000000000000000000000000000';
const block={number:100n,hash:'0x'+'a'.repeat(64),timestamp:1000n};
const base=()=>({getChainId:async()=>11155111,getBlock:async()=>block});

test('normalizes only bounded explicit ASCII .eth names',()=>{
  assert.equal(checkedName('ocean.orca.eth'),'ocean.orca.eth');
  for(const name of ['','eth','ORCA.eth','a..eth','a.com','../orca.eth','orca.eth/','orca.eth\0','😀.eth','a.b.c.d.e.f.eth','a'.repeat(64)+'.eth']) assert.throws(()=>checkedName(name));
});
test('runtime pins mask only declared immutable offsets',()=>{
  const runtime={bytes:3,immutable_ranges:[{start:1,length:1}],normalized_sha256:createHash('sha256').update(Buffer.from([1,0,3])).digest('hex')};
  checkRuntime('0x01ff03',runtime);
  for(const code of ['0x','0x0102','0x02ff03','0x01ff04',undefined]) assert.throws(()=>checkRuntime(code,runtime));
});
test('wrong network and absent finalized block fail closed',async()=>{
  await assert.rejects(new EnsReader({...base(),getChainId:async()=>1}).start(),/Wrong ENS chain/);
  await assert.rejects(new EnsReader({...base(),getBlock:async()=>({...block,hash:null})}).start(),/Finalized/);
});
test('all read calls bind to recorded finalized block and disable CCIP gateways',async()=>{
  const reader=new EnsReader({...base(),readContract:async args=>{assert.equal(args.blockNumber,100n);assert.equal(args.ccipRead,false);return true;}});
  await reader.start();await reader.read(OWNER,[],'example');
});
test('implementation, missing code and unpinned direct contract rejected',async()=>{
  const reader=new EnsReader({...base(),getCode:async()=> '0x01'});await reader.start();
  await assert.rejects(reader.contract(pin.contracts.UserRegistryImpl.address,'UserRegistryImpl',true),/Implementation address/);
  await assert.rejects(reader.contract(OWNER,'RootRegistry'),/Unpinned/);
  reader.client.getCode=async()=> '0x';
  await assert.rejects(reader.contract(OWNER,'UserRegistryImpl',true),/bytecode missing/);
});
test('proxy claiming unsupported implementation is rejected before interface trust',async()=>{
  const reader=new EnsReader({...base(),getCode:async()=> '0x01',getStorageAt:async()=> '0x'+'0'.repeat(24)+OWNER.slice(2)});await reader.start();
  await assert.rejects(reader.contract(RESOLVER,'PermissionedResolverImpl',true),/Unsupported proxy implementation/);
});
function recognizedReader({badInterface=false,badFactory=false,invalidERC165=false}={}) {
  const deployment=structuredClone(pin);
  for (const c of Object.values(deployment.contracts)) c.runtime={bytes:1,immutable_ranges:[],normalized_sha256:createHash('sha256').update(Buffer.from([1])).digest('hex')};
  const client={...base(),getCode:async()=> '0x01',getStorageAt:async()=> '0x'+'0'.repeat(24)+deployment.contracts.PermissionedResolverImpl.address.slice(2),readContract:async({functionName,args})=>{
    if(functionName==='verifyContract') return badFactory?OWNER:deployment.contracts.PermissionedResolverImpl.address;
    if(functionName==='supportsInterface') return args[0]==='0xffffffff'?invalidERC165:!badInterface;
    throw Error('Unexpected call');
  }};
  return new EnsReader(client,deployment);
}
test('proxy recognition requires pinned factory provenance and both resolver interfaces',async()=>{
  const reader=recognizedReader();await reader.start();
  const result=await reader.contract(RESOLVER,'PermissionedResolverImpl',true);
  assert.equal(result.implementation,pin.contracts.PermissionedResolverImpl.address);
  assert.equal(reader.cache.size,2);
});
for (const [label,options,message] of [['fake factory',{badFactory:true},/provenance/],['unsupported interface',{badInterface:true},/interface/],['lying ERC165',{invalidERC165:true},/Invalid ERC165/]]) {
  test(`proxy recognition rejects ${label}`,async()=>{
    const reader=recognizedReader(options);await reader.start();
    await assert.rejects(reader.contract(RESOLVER,'PermissionedResolverImpl',true),message);
  });
}
test('reorganized observation is not accepted',async()=>{
  let calls=0;const reader=new EnsReader({...base(),getBlock:async()=>({...block,hash:++calls===1?block.hash:'0x'+'b'.repeat(64)})});
  await reader.start();await assert.rejects(reader.finish(),/block changed/);
});

function hierarchyReader({status=2,expiry=2000n,owner=OWNER,resolver=RESOLVER,offset=0,huge=false}={}) {
  const reader=new EnsReader({...base(),readContract:async args=>{
    if(args.functionName==='getState') return {status,expiry,latestOwner:owner,tokenId:12n,resource:34n};
    if(args.functionName==='getResolver') return resolver;
    if(args.functionName==='getSubregistry') return pin.contracts.ETHRegistry.address;
    if(args.functionName==='findResolver') return [resolver,namehash('orca.eth'),BigInt(offset)];
    if(args.functionName==='resolve') return [encodeAbiParameters(parseAbiParameters('string'),[huge?'x'.repeat(2049):'public-value']),resolver];
    throw Error('Unexpected call');
  }});
  // Contract recognition is isolated here; separate cases cover recognition failure paths.
  reader.contract=async()=>({});return reader;
}
test('hierarchy observation includes five actual record reads and never grants authority',async()=>{
  const value=await hierarchyReader().inspect('orca.eth');
  assert.equal(value.status,'NAME_OBSERVED_NOT_AUTHORIZED');assert.equal(value.authority_verified,false);
  assert.equal(value.research_dispatch_enabled,false);assert.equal(value.delegation_isolation,'NOT_PROVEN');
  assert.equal(value.hierarchy.length,2);assert.deepEqual(Object.keys(value.records),RECORD_KEYS);
  assert.equal(value.block_hash,block.hash);assert.equal(value.hierarchy[1].resource,'34');
});
for(const [label,options] of [['expired',{expiry:1000n}],['reserved',{status:1}],['no owner',{owner:ZERO}],['wildcard inheritance',{offset:5}],['oversized record',{huge:true}]]) {
  test(`rejects ${label}`,async()=>{await assert.rejects(hierarchyReader(options).inspect('orca.eth'));});
}
