/** Purpose: verify runtime SDK request and HTTP response handling.
 * Behavior: pass through authentication and retain a structured failure without live I/O.
 * Refs: main_implementation plan.md P14.3; api design.md §19. Phase: P14.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { createOrcaClient } from "./client.ts";

test("SDK preserves scoped headers and problem details", async () => {
  const project = "00000000-0000-4000-8000-000000000001";
  const client = createOrcaClient({
    baseUrl: "https://orca.example",
    fetch: async (request) => {
      assert.equal(request.method, "GET");
      assert.equal(request.url, `https://orca.example/v1/research/projects/${project}`);
      assert.equal(request.headers.get("authorization"), "Bearer test-only");
      return new Response(JSON.stringify({ status: 403, title: "Forbidden" }), {
        status: 403,
        headers: { "Content-Type": "application/problem+json" },
      });
    },
  });
  const result = await client.GET("/v1/research/projects/{project_id}", {
    headers: { Authorization: "Bearer test-only" },
    params: { path: { project_id: project } },
  });
  assert.equal(result.response.status, 403);
  assert.deepEqual(result.error, { status: 403, title: "Forbidden" });
});
