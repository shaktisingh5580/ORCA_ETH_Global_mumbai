/** Purpose: publish consented minimal research references and verify independently on Arkiv.
 * Behavior: use pinned SDK/Rabdi, explicit disclosure approval, owner checks and bounded reads.
 * Refs: accelerated-research-arkiv-demo.json; official Arkiv SDK 1.0.1; Phase: accelerated P11 demo.
 */
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createHash } from "node:crypto";
import { createPublicClient, createWalletClient, ExpirationTime } from "@arkiv-network/sdk";
import { rabdi } from "@arkiv-network/sdk/chains";
import { defineSchema, str } from "@arkiv-network/sdk/schema";
import { http, isAddress } from "viem";
import { privateKeyToAccount, generatePrivateKey } from "viem/accounts";

const Results = defineSchema("orca.demo.result.v1", {
  resultId: str().indexed(), resultDigest: str().indexed(), method: str(),
  scientificStatus: str(), disclosure: str(), publicExportDigest: str(),
});
const args = process.argv.slice(2);
function option(name) { const i = args.indexOf(name); return i < 0 ? undefined : args[i + 1]; }
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const digestPattern = /^sha256:[0-9a-f]{64}$/;
const digest = (bytes) => "sha256:" + createHash("sha256").update(bytes).digest("hex");
async function exportFile() {
  const filename = option("--file");
  if (!filename) throw new Error("Supply --file containing the backend public-export JSON");
  const raw = await readFile(filename);
  if (raw.length > 4096) throw new Error("Public export is oversized");
  const data = JSON.parse(raw.toString("utf8"));
  const keys = ["schema_version", "result_id", "result_digest", "method", "scientific_status", "disclosure"];
  if (Object.keys(data).length !== keys.length || keys.some(k => !Object.hasOwn(data, k)) ||
      data.schema_version !== "orca.public-demo-result/1.0" || !uuid.test(data.result_id) ||
      !digestPattern.test(data.result_digest) || data.method !== "orca.descriptive-sst/1.0" ||
      data.scientific_status !== "not_validated" || data.disclosure !== "explicitly_approved_public_reference") {
    throw new Error("Invalid public export; extra/private fields are prohibited");
  }
  const canonical = JSON.stringify(Object.fromEntries(Object.entries(data).sort(([a],[b]) => a < b ? -1 : a > b ? 1 : 0)));
  return { data, exportDigest: digest(canonical) };
}
function chainClients() {
  const chain = rabdi({ accessKey: process.env.ARKIV_ACCESS_KEY ?? "" });
  const transport = http(undefined, { timeout: 20_000, retryCount: 1 });
  return { chain, transport, publicClient: createPublicClient({ chain, transport }) };
}
async function readVerified(publicClient, key, owner, expected) {
  if (!/^0x[0-9a-f]{64}$/i.test(key ?? "") || !isAddress(owner ?? "")) throw new Error("Supply a valid entity key and expected owner");
  let entity;
  for (let attempt = 0; attempt < 5; attempt++) {
    try { entity = await publicClient.getEntity(key, Results); break; }
    catch (error) { if (error?.name !== "NoEntityFoundError" || attempt === 4) throw error; await new Promise(r => setTimeout(r, 2000)); }
  }
  if (entity.$owner.toLowerCase() !== owner.toLowerCase()) throw new Error("Entity owner mismatch");
  const result = { resultId: entity.resultId.value, resultDigest: entity.resultDigest.value,
    method: entity.method.value, scientificStatus: entity.scientificStatus.value,
    disclosure: entity.disclosure.value, publicExportDigest: entity.publicExportDigest.value };
  if (!uuid.test(result.resultId) || !digestPattern.test(result.resultDigest) || result.method !== "orca.descriptive-sst/1.0" || result.scientificStatus !== "not_validated" || result.disclosure !== "explicitly_approved_public_reference") throw new Error("Invalid public result record");
  if (expected && (result.resultId !== expected.data.result_id || result.resultDigest !== expected.data.result_digest || result.publicExportDigest !== expected.exportDigest)) throw new Error("Public result differs from expected export");
  return { entityKey: key, owner: entity.$owner, ...result, verification: "schema_and_wallet_owner_verified", institutional_identity: "not_verified" };
}
async function main() {
  if (args[0] === "wallet") {
    const filename = resolve(option("--env") ?? "../.env.arkiv");
    const privateKey = generatePrivateKey();
    const account = privateKeyToAccount(privateKey);
    await writeFile(filename, `# Dedicated Rabdi demo wallet. Never commit this file.\nARKIV_PRIVATE_KEY=${privateKey}\nARKIV_ACCESS_KEY=\n`, { flag: "wx", mode: 0o600 });
    console.log(JSON.stringify({ address: account.address, envFile: filename, faucet: "https://hub.arkiv.network/", chain: "Rabdi", chainId: 7794095 }));
    return;
  }
  if (args[0] === "inspect") {
    const record = await exportFile();
    console.log(JSON.stringify(record, null, 2)); return;
  }
  if (!["publish", "read", "status"].includes(args[0])) throw new Error("Commands: wallet, status, inspect --file, publish --file --approve-public --receipt FILE, read --key KEY --owner ADDRESS [--file FILE]");
  const { chain, transport, publicClient } = chainClients();
  if (args[0] === "status") {
    const privateKey = process.env.ARKIV_PRIVATE_KEY;
    const owner = option("--owner") ?? (/^0x[0-9a-f]{64}$/i.test(privateKey ?? "") ? privateKeyToAccount(privateKey).address : undefined);
    if (!isAddress(owner ?? "")) throw new Error("Supply --owner ADDRESS or configure the demo wallet");
    const [chainId, balance] = await Promise.all([publicClient.getChainId(), publicClient.getBalance({ address: owner })]);
    if (chainId !== chain.id) throw new Error("RPC chain mismatch");
    console.log(JSON.stringify({ chainId, owner, balanceWei: balance.toString(), funded: balance > 0n })); return;
  }
  if (args[0] === "read") {
    console.log(JSON.stringify(await readVerified(publicClient, option("--key"), option("--owner"), option("--file") ? await exportFile() : undefined), null, 2)); return;
  }
  if (!args.includes("--approve-public")) throw new Error("Publication requires --approve-public");
  const receiptFile = option("--receipt");
  if (!receiptFile) throw new Error("Supply --receipt FILE for the durable transaction receipt");
  // Reserve the receipt before any chain write, preventing accidental replay to the same file.
  const record = await exportFile();
  const key = process.env.ARKIV_PRIVATE_KEY;
  if (!/^0x[0-9a-f]{64}$/i.test(key ?? "")) throw new Error("Configure ARKIV_PRIVATE_KEY in an ignored local environment file");
  const account = privateKeyToAccount(key);
  const walletClient = createWalletClient({ chain, transport, account });
  await writeFile(receiptFile, JSON.stringify({ state: "submission_started", owner: account.address, chainId: chain.id, ...record }), { flag: "wx" });
  const created = await walletClient.createEntity(Results, {
    resultId: record.data.result_id, resultDigest: record.data.result_digest,
    method: record.data.method, scientificStatus: record.data.scientific_status,
    disclosure: record.data.disclosure, publicExportDigest: record.exportDigest,
  }, { expiration: ExpirationTime.fromDays(7), flags: { readonly: true } });
  const receipt = { state: "confirmed", chainId: chain.id, owner: account.address, entityKey: created.entityKey, txHash: created.txHash, expiresAt: created.expiresAt.toString(), publicExportDigest: record.exportDigest };
  await writeFile(receiptFile, JSON.stringify(receipt, null, 2));
  const verified = await readVerified(publicClient, created.entityKey, account.address, record);
  console.log(JSON.stringify({ ...receipt, readBack: verified }, null, 2));
}
main().catch(error => {
  // SDK errors may contain credential-bearing RPC URLs. Print only actionable local validation errors.
  console.error(error?.constructor === Error ? error.message : "Arkiv operation failed; check wallet funds, access key and RPC. A submission_started receipt requires reconciliation before retrying.");
  process.exitCode = 1;
});
