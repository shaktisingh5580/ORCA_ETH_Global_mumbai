/** Purpose: handle transient rate limits on the Arkiv public reader.
 * Behavior: honor retry hints within a short budget and stop on other errors.
 * Refs: main_implementation plan.md P11.4; api design.md §18.2. Phase: P11.
 */
import { setTimeout as delay } from 'node:timers/promises';

function rateLimitError(error) {
  for (let depth = 0; error && depth < 8; depth++, error = error.cause) {
    if (error.status === 429 || error.name === 'LimitExceededRpcError') return error;
  }
  return null;
}

export async function readWithBackoff(read, wait = delay) {
  let budget = 5000;
  for (let attempt = 0; ; attempt++) {
    try { return await read(); }
    catch (error) {
      const limit = rateLimitError(error);
      if (!limit || attempt >= 2) throw error;
      const hint = limit.headers?.get?.('Retry-After');
      const requested = /^\d+$/.test(hint ?? '') ? Number(hint) * 1000
        : (hint ? Date.parse(hint) - Date.now() : 0);
      const waitMs = Math.max(500 * 2 ** attempt, Number.isNaN(requested) ? 0 : requested);
      if (waitMs > budget) throw error;
      budget -= waitMs;
      await wait(waitMs);
    }
  }
}
