/** Purpose: check P10 cross-chain binding signature boundaries.
 * Behavior: reject wrong chain, altered nonce, expired challenge and substituted signers.
 * Refs: main_implementation plan.md P10.3; EIP-712; Phase: P10.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { generateKeyPairSync, randomBytes, randomUUID, sign } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { generatePrivateKey, privateKeyToAccount } from 'viem/accounts';
import { bindingTypedData, serviceBindingStatement, verifyBindingSignatures } from '../src/identity-binding.mjs';

async function binding() {
  const controller=privateKeyToAccount(generatePrivateKey());
  const arkiv=privateKeyToAccount(generatePrivateKey());
  const service=generateKeyPairSync('ed25519');
  const manifest={
    organizationId:randomUUID(),agentId:randomUUID(),audience:'orca.agent-binding',
    ensName:'ocean.orca.eth',ensResolver:'0x'+'1'.repeat(40),ensController:controller.address,
    ensObservedBlock:100,ensBlockHash:'0x'+'c'.repeat(64),
    arkivWallet:arkiv.address,serviceKeyId:'service-1',
    servicePublicKey:service.publicKey.export({format:'der',type:'spki'}).subarray(-32).toString('base64url'),
    endpointOrigin:'https://provider.example',cardDigest:'0x'+'a'.repeat(64),
    credentialDigest:'0x'+'b'.repeat(64),scopes:['identity:read'],nonce:'0x'+randomBytes(32).toString('hex'),
    issuedAt:1000,expiresAt:1100,revocationEpoch:0,
  };
  const typed=bindingTypedData(manifest);
  const signatures={
    controller:await controller.signTypedData(typed.ens),wallet:await arkiv.signTypedData(typed.arkiv),
    service:sign(null,serviceBindingStatement(manifest),service.privateKey).toString('base64url'),
  };
  return {controller,arkiv,manifest,signatures,typed};
}

test('test_eip712_domain_chain_and_nonce',async()=>{
  const {controller,manifest,signatures,typed}=await binding();
  const checked=await verifyBindingSignatures(manifest,manifest,signatures,1001);
  assert.equal(checked.status,'SIGNATURES_VALID');
  assert.notEqual(checked.ens_digest,checked.arkiv_digest);
  assert.equal(checked.authority_verified,false);
  const wrongChain=await controller.signTypedData({...typed.ens,domain:{...typed.ens.domain,chainId:1}});
  await assert.rejects(verifyBindingSignatures(manifest,manifest,{...signatures,controller:wrongChain},1001),/Wallet signature/);
  await assert.rejects(verifyBindingSignatures(manifest,{...manifest,nonce:'0x'+'0'.repeat(64)},signatures,1001),/expected challenge/);
  await assert.rejects(verifyBindingSignatures(manifest,{...manifest,revocationEpoch:1},signatures,1001),/expected challenge/);
});

test('test_expired_binding',async()=>{
  const {manifest,signatures}=await binding();
  await assert.rejects(verifyBindingSignatures(manifest,manifest,signatures,1100),/expired/);
});

test('test_stolen_wallet_spoof',async()=>{
  const {controller,manifest,signatures,typed}=await binding();
  const wallet=await controller.signTypedData(typed.arkiv);
  await assert.rejects(verifyBindingSignatures(manifest,manifest,{...signatures,wallet},1001),/Wallet signature/);
  const impostor=generateKeyPairSync('ed25519');
  const service=sign(null,serviceBindingStatement(manifest),impostor.privateKey).toString('base64url');
  await assert.rejects(verifyBindingSignatures(manifest,manifest,{...signatures,service},1001),/Service signature/);
});

test('prepare exports the exact public signing payloads for all three signers',async()=>{
  const {manifest,typed}=await binding();
  const script=fileURLToPath(new URL('../src/identity-binding.mjs',import.meta.url));
  const prepared=spawnSync(process.execPath,[script,'prepare'],{input:JSON.stringify(manifest),encoding:'utf8'});
  assert.equal(prepared.status,0,prepared.stderr);
  assert.deepEqual(JSON.parse(prepared.stdout),{
    ...typed,service_statement:serviceBindingStatement(manifest).toString('utf8'),
  });
  const invalid=spawnSync(process.execPath,[script,'prepare'],{input:'{}',encoding:'utf8'});
  assert.equal(invalid.status,1);
  assert.equal(JSON.parse(invalid.stdout).status,'MANIFEST_REJECTED');
});
