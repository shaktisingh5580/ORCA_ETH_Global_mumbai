/** Purpose: construct the typed client from the implemented runtime contract.
 * Behavior: accept caller-provided fetch options; retain HTTP status and response types.
 * Refs: main_implementation plan.md P14.3; api design.md §19. Phase: P14.
 */

import createClient, { type ClientOptions } from "openapi-fetch";
import type { paths } from "./runtime.js";

export function createOrcaClient(options: ClientOptions = {}) {
  return createClient<paths>(options);
}
