/** Purpose: observe Arkiv creation receipts without submitting transactions.
 * Behavior: verify canonical block, engine event, owner, entity key and creation flags.
 * Refs: main_implementation plan.md P11.4; api design.md §§3.12,18.2. Phase: P11.
 */
import { ENTITY_EVENTS_ABI } from '@arkiv-network/sdk';
import { parseEventLogs, TransactionReceiptNotFoundError, BlockNotFoundError } from 'viem';

// Engine address used by the pinned Arkiv SDK 1.0.1 on Rabdi.
const engine = '0x4400000000000000000000000000000000000044';
const hex32 = /^0x[0-9a-f]{64}$/i;

export async function observeCreationReceipt(reader, {txHash, entityKey, owner}) {
  if (!hex32.test(txHash ?? '')) throw new Error('Invalid Arkiv transaction hash');
  let receipt;
  try { receipt = await reader.getTransactionReceipt({hash:txHash}); }
  catch (error) {
    if (!(error instanceof TransactionReceiptNotFoundError)) throw error;
    return {status:'receipt_not_found'};
  }
  if (!hex32.test(receipt.blockHash ?? '') || typeof receipt.blockNumber !== 'bigint' ||
      receipt.transactionHash?.toLowerCase() !== txHash.toLowerCase()) {
    return {status:'receipt_mismatch'};
  }
  let block;
  try { block = await reader.getBlock({blockNumber:receipt.blockNumber}); }
  catch (error) {
    if (!(error instanceof BlockNotFoundError)) throw error;
    return {status:'block_not_found'};
  }
  if (block.hash?.toLowerCase() !== receipt.blockHash.toLowerCase()) return {status:'block_changed'};
  const anchor = {block_number:String(receipt.blockNumber), block_hash:receipt.blockHash};
  if (receipt.status === 'reverted') return {status:'reverted', ...anchor};
  if (receipt.status !== 'success') return {status:'receipt_mismatch', ...anchor};
  const logs = receipt.logs.filter(log => log.address.toLowerCase() === engine && !log.removed);
  const matching = parseEventLogs({abi:ENTITY_EVENTS_ABI, logs, eventName:'EntityCreated'})
    .filter(log => log.args.entityKey?.toLowerCase() === entityKey.toLowerCase() &&
      log.args.owner?.toLowerCase() === owner.toLowerCase() && Number(log.args.creationFlags) === 1);
  if (matching.length !== 1) return {status:'creation_event_mismatch', ...anchor};
  return {status:'observed', ...anchor, creation_expiry_block:String(matching[0].args.expiresAt)};
}
