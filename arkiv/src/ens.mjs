/** Purpose: inspect the pinned Sepolia ENSv2 hierarchy without custody or transactions.
 * Behavior: bind all reads to one finalized block, verify code/interfaces/proxies and report records.
 * Refs: institutional plan F03.1; official contracts-v2 revision in contracts/ens/sepolia.json.
 * Phase: F03.1. This observation alone never grants controller or research authority.
 */
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { createPublicClient, http, keccak256, toHex, encodeFunctionData, decodeFunctionResult, parseAbi } from 'viem';
import { normalize, namehash, packetToBytes } from 'viem/ens';
import { sepolia } from 'viem/chains';

export const pin = JSON.parse(await readFile(new URL('../../contracts/ens/sepolia.json', import.meta.url), 'utf8'));
const ZERO = '0x0000000000000000000000000000000000000000';
const SLOT = '0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc';
const same = (a,b) => a.toLowerCase() === b.toLowerCase();
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const profile = parseAbi(['function text(bytes32 node,string key) view returns (string)']);
export const RECORD_KEYS = ['org.orca.endpoint','org.orca.cardDigest','org.orca.serviceKey','org.orca.arkivWallet','org.orca.manifestDigest'];

export function checkedName(input) {
  if (typeof input !== 'string' || input.length > 253 || !/^[a-z0-9.-]+$/.test(input)) throw new Error('Normalized ASCII ENS name required');
  const name = normalize(input);
  const labels = name.split('.');
  if (name !== input || labels.length < 2 || labels.length > 6 || labels.at(-1) !== 'eth' || labels.some(l => !l || l.length > 63)) throw new Error('Supply a normalized name.eth with at most six labels');
  return name;
}

export function checkRuntime(code, runtime) {
  if (!/^0x(?:[0-9a-f]{2})+$/i.test(code ?? '')) throw new Error('Contract bytecode missing');
  const bytes = Buffer.from(code.slice(2),'hex');
  if (bytes.length !== runtime.bytes) throw new Error('Unsupported contract runtime length');
  for (const {start,length} of runtime.immutable_ranges) bytes.fill(0,start,start+length);
  if (sha(bytes) !== runtime.normalized_sha256) throw new Error('Unsupported contract runtime fingerprint');
}

export class EnsReader {
  constructor(client, deployment = pin) { this.client = client; this.deployment = deployment; this.cache = new Map(); }
  async start() {
    if (await this.client.getChainId() !== this.deployment.chain_id) throw new Error('Wrong ENS chain');
    this.block = await this.client.getBlock({blockTag:'finalized'});
    if (!this.block.hash || this.block.number === null) throw new Error('Finalized block unavailable');
  }
  async read(address, abi, functionName, args = []) {
    return this.client.readContract({address,abi,functionName,args,blockNumber:this.block.number,ccipRead:false});
  }
  async contract(address, kind, proxy = false) {
    const cacheKey = address.toLowerCase()+kind+proxy;
    if (this.cache.has(cacheKey)) return this.cache.get(cacheKey);
    if (same(address,ZERO)) throw new Error('Contract address missing');
    const expected = this.deployment.contracts[kind];
    const code = await this.client.getCode({address,blockNumber:this.block.number});
    if (!code || code === '0x') throw new Error('Contract bytecode missing');
    let implementation = null;
    if (proxy) {
      if (same(address,expected.address)) throw new Error('Implementation address cannot be used as proxy');
      const slot = await this.client.getStorageAt({address,slot:SLOT,blockNumber:this.block.number});
      if (!/^0x0{24}[0-9a-f]{40}$/i.test(slot ?? '')) throw new Error('Unsupported proxy implementation slot');
      implementation = '0x'+slot.slice(-40);
      if (!same(implementation,expected.address)) throw new Error('Unsupported proxy implementation');
      const factory=this.deployment.contracts.VerifiableFactory;
      await this.contract(factory.address,'VerifiableFactory');
      const verified=await this.read(factory.address,factory.abi,'verifyContract',[address]);
      if (!same(verified,implementation)) throw new Error('Proxy provenance verification failed');
      checkRuntime(await this.client.getCode({address:implementation,blockNumber:this.block.number}),expected.runtime);
    } else {
      if (!same(address,expected.address)) throw new Error('Unpinned protocol address');
      checkRuntime(code,expected.runtime);
    }
    const interfaces = kind==='VerifiableFactory'?[]:['0x01ffc9a7',...(kind.includes('Registry')?['0x51f67f40','0xc18bd555']:kind==='PermissionedResolverImpl'?['0x9061b923','0x8c2427cc']:[])];
    for (const id of interfaces) if (await this.read(address,expected.abi,'supportsInterface',[id]) !== true) throw new Error('Required ENS interface unavailable');
    if (interfaces.length && await this.read(address,expected.abi,'supportsInterface',['0xffffffff']) !== false) throw new Error('Invalid ERC165 implementation');
    const observation = {address,implementation,code_sha256:sha(Buffer.from(code.slice(2),'hex')),kind};
    this.cache.set(cacheKey,observation); return observation;
  }
  async finish() {
    const block = await this.client.getBlock({blockNumber:this.block.number});
    if (block.hash !== this.block.hash) throw new Error('Observed block changed');
    return {chain_id:this.deployment.chain_id,block_number:this.block.number.toString(),block_hash:this.block.hash,block_timestamp:this.block.timestamp.toString(),source_revision:this.deployment.source_revision,contracts:[...this.cache.values()]};
  }
  async preflight() {
    await this.start();
    for (const kind of ['RootRegistry','ETHRegistry','UniversalResolverV2','VerifiableFactory']) await this.contract(this.deployment.contracts[kind].address,kind);
    for (const kind of ['UserRegistryImpl','PermissionedResolverImpl']) checkRuntime(await this.client.getCode({address:this.deployment.contracts[kind].address,blockNumber:this.block.number}),this.deployment.contracts[kind].runtime);
    return {status:'DEPLOYMENT_READ_CHECK_PASSED',...await this.finish(),authority_verified:false};
  }
  async inspect(input) {
    const name = checkedName(input); await this.start();
    const hierarchy=[]; let registry=this.deployment.contracts.RootRegistry.address;
    const labels=name.split('.').reverse();
    for (let i=0;i<labels.length;i++) {
      const kind=i===0?'RootRegistry':i===1?'ETHRegistry':'UserRegistryImpl';
      await this.contract(registry,kind,i>1);
      const abi=this.deployment.contracts[kind].abi, label=labels[i];
      const state=await this.read(registry,abi,'getState',[BigInt(keccak256(toHex(label)))]);
      const resolver=await this.read(registry,abi,'getResolver',[label]);
      const subregistry=await this.read(registry,abi,'getSubregistry',[label]);
      hierarchy.push({label,registry,owner:state.latestOwner,status:Number(state.status),expiry:state.expiry.toString(),token_id:state.tokenId.toString(),resource:state.resource.toString(),resolver,subregistry});
      if (Number(state.status)!==2 || same(state.latestOwner,ZERO) || state.expiry<=this.block.timestamp) throw new Error('Name is not actively registered at every hierarchy level');
      if (i<labels.length-1) registry=subregistry;
    }
    const leaf=hierarchy.at(-1), universal=this.deployment.contracts.UniversalResolverV2;
    await this.contract(universal.address,'UniversalResolverV2');
    await this.contract(leaf.resolver,'PermissionedResolverImpl',true);
    const dns=toHex(packetToBytes(name)), found=await this.read(universal.address,universal.abi,'findResolver',[dns]);
    if (!same(found[0],leaf.resolver) || found[1]!==namehash(name) || Number(found[2])!==0) throw new Error('Name uses inherited or inconsistent resolver');
    const records={};
    for (const key of RECORD_KEYS) {
      const data=encodeFunctionData({abi:profile,functionName:'text',args:[namehash(name),key]});
      const [answer,resolver]=await this.read(universal.address,universal.abi,'resolve',[dns,data]);
      if (!same(resolver,leaf.resolver)) throw new Error('Resolver changed during resolution');
      const value=decodeFunctionResult({abi:profile,functionName:'text',data:answer});
      if (Buffer.byteLength(value)>2048) throw new Error('ENS record exceeds ORCA limit');
      records[key]=value;
    }
    return {status:'NAME_OBSERVED_NOT_AUTHORIZED',name,hierarchy,records,...await this.finish(),authority_verified:false,affiliation_verified:false,delegation_isolation:'NOT_PROVEN',research_dispatch_enabled:false};
  }
}

export async function main(command, name) {
  // Public fixed transport; caller cannot choose RPC, URL, chain or signing material.
  const client=createPublicClient({chain:sepolia,transport:http(sepolia.rpcUrls.default.http[0],{timeout:12000,retryCount:0}),ccipRead:false});
  const reader=new EnsReader(client);
  if (command==='preflight') return reader.preflight();
  if (command==='inspect') return reader.inspect(name);
  throw new Error('Commands: preflight | inspect name.eth');
}
if (process.argv[1]===fileURLToPath(import.meta.url)) {
  try { console.log(JSON.stringify(await main(process.argv[2],process.argv[3]))); }
  catch { console.error('ENS_READ_UNAVAILABLE_OR_UNSUPPORTED: no authority granted');process.exitCode=1; }
}
