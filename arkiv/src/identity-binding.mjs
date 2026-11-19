/** Purpose: verify P10 controller, Arkiv wallet and A2A key signatures on one manifest.
 * Behavior: bind signatures to pinned chain domains and the independently expected challenge.
 * Refs: main_implementation plan.md P10.3; api design.md §§4.6,10; Phase: P10.
 */
import { createPublicKey, verify } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { getAddress, hashTypedData, recoverTypedDataAddress } from 'viem';
import { checkedName } from './ens.mjs';

const domains = {
  ens: {name:'ORCA ENS Controller Binding', version:'1', chainId:11155111},
  arkiv: {name:'ORCA Arkiv Wallet Binding', version:'1', chainId:7794095},
};
const fields = [
  ['organizationId','string'], ['agentId','string'], ['audience','string'],
  ['ensName','string'], ['ensResolver','address'], ['ensController','address'],
  ['ensObservedBlock','uint64'], ['ensBlockHash','bytes32'],
  ['arkivWallet','address'], ['serviceKeyId','string'], ['servicePublicKey','string'],
  ['endpointOrigin','string'], ['cardDigest','bytes32'], ['credentialDigest','bytes32'],
  ['scopes','string[]'], ['nonce','bytes32'], ['issuedAt','uint64'], ['expiresAt','uint64'],
  ['revocationEpoch','uint64'],
].map(([name,type])=>({name,type}));
const uuid=/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const word=/^0x[0-9a-f]{64}$/;
const zero='0x'+'0'.repeat(40);

function publicKey(encoded) {
  if (typeof encoded!=='string' || !/^[\w-]{43}$/.test(encoded)) throw new Error('Invalid service key');
  const raw=Buffer.from(encoded,'base64url');
  if (raw.length!==32 || raw.toString('base64url')!==encoded) throw new Error('Invalid service key');
  return createPublicKey({
    format:'der',type:'spki',key:Buffer.concat([Buffer.from('302a300506032b6570032100','hex'),raw]),
  });
}

function validated(value) {
  if (!value || Object.keys(value).length!==fields.length
      || fields.some(({name})=>!Object.hasOwn(value,name))) throw new Error('Unexpected binding fields');
  const result={...value};
  if (![value.organizationId,value.agentId].every(x=>typeof x==='string'&&uuid.test(x))
      || value.audience!=='orca.agent-binding'
      || ![value.cardDigest,value.credentialDigest,value.nonce,value.ensBlockHash]
        .every(x=>typeof x==='string'&&word.test(x))
      || !/^[a-zA-Z0-9_.-]{1,100}$/.test(value.serviceKeyId ?? '')) throw new Error('Invalid binding identity');
  result.ensName=checkedName(value.ensName);
  if (result.ensName.split('.').length<3) throw new Error('An agent subname is required');
  for (const field of ['ensResolver','ensController','arkivWallet']) {
    result[field]=getAddress(value[field]);
    if (result[field]===zero) throw new Error('Zero address in binding');
  }
  publicKey(value.servicePublicKey);
  const origin=new URL(value.endpointOrigin);
  if (origin.protocol!=='https:' || origin.origin!==value.endpointOrigin || origin.port) {
    throw new Error('Expected a canonical HTTPS origin');
  }
  if (!Array.isArray(value.scopes) || !value.scopes.length || value.scopes.length>16
      || !value.scopes.every(x=>typeof x==='string'&&/^[a-z][a-z0-9:._-]{0,63}$/.test(x))
      || new Set(value.scopes).size!==value.scopes.length
      || !value.scopes.every((x,i)=>!i||value.scopes[i-1]<x)) throw new Error('Invalid binding scopes');
  for (const field of ['issuedAt','expiresAt','revocationEpoch','ensObservedBlock']) {
    if (!Number.isSafeInteger(value[field]) || value[field]<0) throw new Error('Invalid binding number');
  }
  if (value.expiresAt<=value.issuedAt || value.expiresAt-value.issuedAt>600) {
    throw new Error('Binding lifetime exceeds 10 minutes');
  }
  result.scopes=[...value.scopes];
  return result;
}

export function bindingTypedData(value) {
  const message=validated(value);
  const typed=(domain)=>({
    domain:{...domain},primaryType:'AgentBinding',
    types:{AgentBinding:fields.map(field=>({...field}))},
    message:{...message,scopes:[...message.scopes]},
  });
  return {ens:typed(domains.ens),arkiv:typed(domains.arkiv)};
}

export function serviceBindingStatement(value) {
  const typed=bindingTypedData(value);
  return Buffer.from(`orca.a2a-binding/1.0:${hashTypedData(typed.ens)}:${hashTypedData(typed.arkiv)}`);
}

export async function verifyBindingSignatures(manifest, expected, signatures, now=Math.floor(Date.now()/1000)) {
  const typed=bindingTypedData(manifest), reference=bindingTypedData(expected);
  const digest=hashTypedData(typed.ens);
  if (digest!==hashTypedData(reference.ens)) throw new Error('Binding differs from the expected challenge');
  if (!Number.isSafeInteger(now) || now<typed.ens.message.issuedAt || now>=typed.ens.message.expiresAt) {
    throw new Error('Binding challenge expired or not yet valid');
  }
  const ens=await recoverTypedDataAddress({...typed.ens,signature:signatures.controller});
  const arkiv=await recoverTypedDataAddress({...typed.arkiv,signature:signatures.wallet});
  if (ens!==typed.ens.message.ensController || arkiv!==typed.arkiv.message.arkivWallet) {
    throw new Error('Wallet signature differs from the expected identity');
  }
  const encoded=signatures.service;
  if (typeof encoded!=='string' || !/^[\w-]{86}$/.test(encoded)) throw new Error('Invalid service signature');
  const signature=Buffer.from(encoded,'base64url');
  if (signature.length!==64 || signature.toString('base64url')!==encoded
      || !verify(null,serviceBindingStatement(typed.ens.message),publicKey(typed.ens.message.servicePublicKey),signature)) {
    throw new Error('Service signature differs from the expected identity');
  }
  return {
    status:'SIGNATURES_VALID',ens_digest:digest,arkiv_digest:hashTypedData(typed.arkiv),
    authority_verified:false,research_dispatch_enabled:false,
  };
}

if (process.argv[1]===fileURLToPath(import.meta.url)) {
  try {
    let bytes=0;
    const chunks=[];
    for await (const chunk of process.stdin) {
      bytes+=chunk.length;
      if (bytes>20000) throw new Error('Binding input exceeds limit');
      chunks.push(chunk);
    }
    const {expected,signatures}=JSON.parse(Buffer.concat(chunks).toString('utf8'));
    console.log(JSON.stringify(await verifyBindingSignatures(expected,expected,signatures)));
  } catch {
    console.log(JSON.stringify({status:'SIGNATURES_REJECTED',authority_verified:false}));
    process.exitCode=1;
  }
}
