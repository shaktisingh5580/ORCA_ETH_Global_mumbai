/** Purpose: verify trusted ORCA public reference attributes independently of the API.
 * Behavior: read Rabdi directly and optionally check the creation receipt and artifact bytes.
 * Refs: main_implementation plan.md P11.4; api design.md §§3.12,18.2. Phase: P11.
 */
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { isDeepStrictEqual } from 'node:util';
import { fileURLToPath } from 'node:url';
import { createRabdiReader, readPublicRecord } from '../arkiv/src/public-client.mjs';
import { validatePublicAttributes } from '../arkiv/src/public-schema.mjs';
import { observeCreationReceipt } from '../arkiv/src/readback.mjs';

export async function verifyArkivReference(reader, {intent, entityKey, txHash, canonicalArtifact}) {
  const {kind, owner} = intent;
  const expected = validatePublicAttributes(kind, intent.attributes);
  const observation = await readPublicRecord(reader, {kind, key:entityKey, expectedOwner:owner});
  const report = {
    kind, entity_key:entityKey,
    chain_status:observation.status,
    chain_observation:observation,
    artifact_digest_status:'not_checked',
    institution_verified:false, scientific_result_verified:false,
  };
  if (canonicalArtifact !== undefined) {
    const expectedDigest = expected.artifactDigest ?? expected.reviewDigest;
    if (!expectedDigest) throw new Error('Artifact bytes require a result or review reference');
    const actualDigest = 'sha256:' + createHash('sha256').update(canonicalArtifact).digest('hex');
    report.artifact_digest_status = actualDigest === expectedDigest ? 'matches' : 'mismatch';
  }
  const receipt = txHash === undefined ? null
    : await observeCreationReceipt(reader, {txHash, entityKey, owner});
  if (receipt) report.creation_receipt = receipt;
  if (observation.status !== 'observed') {
    if (observation.status === 'not_found' && receipt?.status === 'observed' &&
        BigInt(observation.observed_block) >= BigInt(receipt.creation_expiry_block)) {
      report.chain_status = 'absent_at_or_after_recorded_expiry';
    }
    return report;
  }
  if (!isDeepStrictEqual(observation.attributes, expected)) {
    return {...report, chain_status:'attributes_mismatch'};
  }
  if (receipt && receipt.status !== 'observed') {
    return {...report, chain_status:'creation_receipt_unverified'};
  }
  if (receipt && (BigInt(observation.observed_block) < BigInt(receipt.block_number) ||
      BigInt(observation.expires_at_block) < BigInt(receipt.creation_expiry_block))) {
    return {...report, chain_status:'inconsistent_chain_view'};
  }
  return {...report, chain_status:'matches_expected_reference'};
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const [manifestPath, entityKey, ...options] = process.argv.slice(2);
    const values = {};
    if (!manifestPath || !entityKey || options.length % 2) throw new Error('Unexpected arguments');
    for (let index = 0; index < options.length; index += 2) {
      const option = options[index];
      if (!['--tx', '--canonical-artifact'].includes(option) || Object.hasOwn(values, option)) {
        throw new Error('Unexpected option');
      }
      values[option] = options[index + 1];
    }
    const intent = JSON.parse(await readFile(manifestPath, 'utf8'));
    const canonicalArtifact = values['--canonical-artifact']
      ? await readFile(values['--canonical-artifact']) : undefined;
    const report = await verifyArkivReference(createRabdiReader(process.env.ARKIV_ACCESS_KEY ?? ''), {
      intent, entityKey, txHash:values['--tx'], canonicalArtifact,
    });
    console.log(JSON.stringify(report, null, 2));
    if (report.chain_status !== 'matches_expected_reference' ||
        report.artifact_digest_status === 'mismatch') process.exitCode = 2;
  } catch {
    console.error('ARKIV_VERIFICATION_UNAVAILABLE_OR_INVALID');
    process.exitCode = 1;
  }
}
