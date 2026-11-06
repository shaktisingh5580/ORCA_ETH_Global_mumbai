/** Purpose: publish/query signed agent task, claim, assignment, result and review references.
 * Behavior: use real pinned Arkiv SDK reads, owner checks and durable pre-send reservations.
 * Refs: H04-agent-coordination.json; canonical signing/database guides; Phase: H04.
 */
import { readFile, writeFile, rename, unlink, mkdir, open } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash, createPublicKey, verify as verifySignature } from "node:crypto";
import { createPublicClient, createWalletClient, ExpirationTime } from "@arkiv-network/sdk";
import { rabdi } from "@arkiv-network/sdk/chains";
import { defineSchema, str } from "@arkiv-network/sdk/schema";
import { eq } from "@arkiv-network/sdk/query";
import { http, isAddress, createWalletClient as evmWallet, parseEther } from "viem";
import { privateKeyToAccount, generatePrivateKey } from "viem/accounts";

const fields = ["schema_version", "domain", "audience", "chain_id", "kind", "event_id", "task_id", "assignment_generation", "actor_id", "binding_version", "subject_id", "parent_key", "recipe_digest", "reference_digest", "issued_at", "deadline"];
const columns = Object.fromEntries([...fields, "content_digest", "key_id", "signature", "public_key"].map(k => [k, ["task_id", "kind", "actor_id", "event_id"].includes(k) ? str().indexed() : str()]));
const Coordination = defineSchema("orca.coordination.hackathon.v1", columns);
const hash = value => "sha256:" + createHash("sha256").update(value).digest("hex");
// Public-profile payloads/statements contain strings only and ASCII keys. For this restricted
// domain, sorted JSON.stringify is exactly RFC8785; general scientific JSON stays in Python JCS.
export function canonicalStrings(value) {
  if (!value || Array.isArray(value) || typeof value !== "object" || Object.entries(value).some(([k,v]) => !/^[a-z_]+$/.test(k) || typeof v !== "string")) throw new Error("Restricted canonical string object required");
  return JSON.stringify(Object.fromEntries(Object.keys(value).sort().map(k => [k, value[k]])));
}
export function validateRecord(envelope, publicKey) {
  const p = envelope?.payload;
  if (!p || Object.keys(p).length !== fields.length || fields.some(k => !Object.hasOwn(p,k)) ||
      Object.values(p).some(v => typeof v !== "string" || Buffer.byteLength(v) > 128) ||
      p.schema_version !== "orca.public-coordination/1.0" || p.domain !== "orca.agent-coordination/hackathon-v1" || p.audience !== "public" || p.chain_id !== "7794095" ||
      !["task","claim","assignment","result","review","cancellation"].includes(p.kind) || !/^[0-9]+$/.test(p.assignment_generation) ||
      !/^sha256:[0-9a-f]{64}$/.test(p.recipe_digest) || !/^sha256:[0-9a-f]{64}$/.test(p.reference_digest) ||
      envelope.algorithm !== "Ed25519" || hash(canonicalStrings(p)) !== envelope.content_digest) throw new Error("Invalid public coordination payload");
  for (const key of ["task_id", "event_id", "actor_id", "binding_version", "subject_id"]) if (!/^[0-9a-f-]{36}$/i.test(p[key])) throw new Error("Invalid coordination identity");
  if (p.parent_key && !/^0x[0-9a-f]{64}$/i.test(p.parent_key)) throw new Error("Invalid parent key");
  const statement = { schema_version: "orca.detached-agent-signature/1.0", domain:p.domain, audience:p.audience, task_id:p.task_id, assignment_generation:p.assignment_generation, binding_version:p.binding_version, content_digest:envelope.content_digest, key_id:envelope.key_id };
  const bytes = Buffer.from(publicKey, "base64url");
  if (bytes.length !== 32 || !verifySignature(null, Buffer.from(canonicalStrings(statement)), createPublicKey({key:Buffer.concat([Buffer.from("302a300506032b6570032100","hex"),bytes]),format:"der",type:"spki"}), Buffer.from(envelope.signature,"base64url"))) throw new Error("Invalid service signature");
  return { ...p, content_digest:envelope.content_digest, key_id:envelope.key_id, signature:envelope.signature, public_key:publicKey };
}
function decode(entity) {
  const values = Object.fromEntries([...fields,"content_digest","key_id","signature","public_key"].map(k => [k,entity[k]?.value]));
  const envelope = {payload:Object.fromEntries(fields.map(k => [k,values[k]])),content_digest:values.content_digest,key_id:values.key_id,signature:values.signature,algorithm:"Ed25519"};
  validateRecord(envelope, values.public_key);
  return {entity_key:entity.$key,owner:entity.$owner,expires_at:String(entity.$expiresAt),envelope,public_key:values.public_key};
}
async function atomic(path, value) {
  const temp = path + ".next";
  const handle = await open(temp,"w",0o600);
  try { await handle.writeFile(JSON.stringify(value)); await handle.sync(); } finally { await handle.close(); }
  await rename(temp,path);
}
async function read(client, key, owner, expected) {
  if (!/^0x[0-9a-f]{64}$/i.test(key ?? "") || !isAddress(owner ?? "")) throw new Error("Invalid chain key/owner");
  let row;
  for (let attempt=0;attempt<5;attempt++) {
    try { row=decode(await client.getEntity(key,Coordination)); break; }
    catch(error) { if(error?.name!=="NoEntityFoundError" || attempt===4) throw error; await new Promise(r=>setTimeout(r,2000)); }
  }
  if(row.owner.toLowerCase()!==owner.toLowerCase() || (expected && row.envelope.content_digest!==expected)) throw new Error("Chain owner/digest differs from expected record");
  return {...row,verification:"current_chain_owner_and_service_signature_verified",institutional_identity:"not_verified",observed_at:new Date().toISOString()};
}
export async function run(input) {
  if(input.command==="validate") return {digest:hash(canonicalStrings(input.envelope.payload)),values:validateRecord(input.envelope,input.public_key)};
  if(input.command==="initialize_wallet") {
    const filename=resolve(input.env_file);
    const privateKey=generatePrivateKey();
    await writeFile(filename,`ARKIV_PRIVATE_KEY=${privateKey}\nARKIV_ACCESS_KEY=${process.env.ARKIV_ACCESS_KEY ?? ""}\n`,{flag:"wx",mode:0o600});
    return {owner:privateKeyToAccount(privateKey).address};
  }
  const chain=rabdi({accessKey:process.env.ARKIV_ACCESS_KEY ?? ""});
  const transport=http(undefined,{timeout:20_000,retryCount:0});
  const client=createPublicClient({chain,transport});
  if(await client.getChainId()!==7794095) throw new Error("RPC chain mismatch");
  if(input.command==="wallet") {
    const owner=privateKeyToAccount(process.env.ARKIV_PRIVATE_KEY).address;
    return {owner,balance_wei:String(await client.getBalance({address:owner}))};
  }
  if(input.command==="fund") {
    if(!isAddress(input.recipient) || input.amount!=="0.1") throw new Error("Only bounded demo provider funding is permitted");
    const current=await client.getBalance({address:input.recipient});
    if(current>0n) return {funded:true,already_funded:true,balance_wei:String(current)};
    const filename=resolve(input.receipt_file);
    await mkdir(dirname(filename),{recursive:true});
    const reservation=await open(filename,"wx",0o600);
    await reservation.writeFile(JSON.stringify({state:"funding_started",recipient:input.recipient})); await reservation.sync(); await reservation.close();
    const account=privateKeyToAccount(process.env.ARKIV_PRIVATE_KEY);
    const wallet=evmWallet({chain,transport,account});
    const tx=await wallet.sendTransaction({to:input.recipient,value:parseEther("0.1")});
    await atomic(filename,{state:"submitted",recipient:input.recipient,tx_hash:tx});
    const receipt=await client.waitForTransactionReceipt({hash:tx});
    if(receipt.status!=="success") throw new Error("Provider funding reverted");
    await atomic(filename,{state:"confirmed",recipient:input.recipient,tx_hash:tx});
    return {funded:true,tx_hash:tx};
  }
  if(input.command==="read") return await read(client,input.entity_key,input.owner,input.content_digest);
  if(input.command==="query") {
    if(!/^[0-9a-f-]{36}$/i.test(input.task_id)) throw new Error("Invalid task filter");
    const rows=[]; let truncated=false;
    for(let offset=0;offset<500;offset+=100) {
      const page=await client.select("*").from(Coordination).where(eq("task_id",input.task_id)).limit(100).offset(offset).fetch();
      for(const entity of page.entities) { try { rows.push(decode(entity)); } catch { /* Untrusted schemas/signatures are not accepted. */ } }
      if(page.entities.length<100) return {records:rows,truncated:false,observed_at:new Date().toISOString()};
      truncated=true;
    }
    return {records:rows,truncated,observed_at:new Date().toISOString()};
  }
  if(input.command!=="publish") throw new Error("Unsupported coordination command");
  const values=validateRecord(input.envelope,input.public_key);
  const account=privateKeyToAccount(process.env.ARKIV_PRIVATE_KEY);
  if(account.address.toLowerCase()!==input.owner.toLowerCase()) throw new Error("Configured wallet differs from pinned writer");
  const filename=resolve(input.receipt_file);
  await mkdir(dirname(filename),{recursive:true});
  let existing;
  try { existing=JSON.parse(await readFile(filename,"utf8")); } catch(error) { if(error.code!=="ENOENT") throw error; }
  if(existing) {
    if(existing.content_digest!==input.envelope.content_digest || existing.owner.toLowerCase()!==input.owner.toLowerCase()) throw new Error("Reserved operation differs; refusing replay");
    const verified=await read(client,existing.entity_key,input.owner,existing.content_digest);
    return {...existing,state:"indexed",read_back:verified,reconciled:true};
  }
  const lock=resolve(input.lock_file);
  await mkdir(dirname(lock),{recursive:true});
  const held=await open(lock,"wx",0o600);
  await held.writeFile(JSON.stringify({event_id:values.event_id,receipt_file:filename})); await held.sync(); await held.close();
  let confirmed=false, reservedBeforeSend=false;
  try {
    const [predicted]=await client.predictEntityKeys({owner:account.address,count:1});
    const reservation={state:"submission_started",chain_id:7794095,owner:account.address,entity_key:predicted.key,salt:String(predicted.salt),content_digest:input.envelope.content_digest,event_id:values.event_id};
    const reserved=await open(filename,"wx",0o600);
    try { await reserved.writeFile(JSON.stringify(reservation)); await reserved.sync(); } finally { await reserved.close(); }
    reservedBeforeSend=true;
    const wallet=createWalletClient({chain,transport,account});
    const created=await wallet.createEntity(Coordination,values,{expiration:ExpirationTime.fromDays(7),flags:{readonly:true},salt:predicted.salt});
    const receipt={...reservation,state:"confirmed",entity_key:created.entityKey,tx_hash:created.txHash,expires_at:String(created.expiresAt)};
    await atomic(filename,receipt);
    confirmed=true;
    if(created.entityKey.toLowerCase()!==predicted.key.toLowerCase()) throw new Error("Writer nonce changed outside this adapter; reconcile operation");
    return {...receipt,state:"indexed",read_back:await read(client,created.entityKey,account.address,input.envelope.content_digest)};
  } finally {
    // An uncertain submission keeps the writer lock; no later job can blindly use its nonce.
    if(confirmed || !reservedBeforeSend) await unlink(lock);
  }
}
if(process.argv[1] && resolve(process.argv[1])===fileURLToPath(import.meta.url)) {
  try {
    const chunks=[]; let size=0;
    for await(const chunk of process.stdin) { size+=chunk.length; if(size>65536) throw new Error("Oversized bridge input"); chunks.push(chunk); }
    console.log(JSON.stringify(await run(JSON.parse(Buffer.concat(chunks).toString("utf8")))));
  } catch { console.error("ARKIV_COORDINATION_UNAVAILABLE_OR_RECONCILIATION_REQUIRED"); process.exitCode=1; }
}
