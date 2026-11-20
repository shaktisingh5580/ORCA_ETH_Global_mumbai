/** Purpose: prove the generated client restricts requests to known runtime operations.
 * Behavior: compile read, authenticated write, and expected invalid examples without I/O.
 * Refs: main_implementation plan.md P14.3; api design.md §19. Phase: P14.
 */

import { createOrcaClient } from "./client.js";

async function checkRuntimeClient() {
  const client = createOrcaClient({ baseUrl: "https://orca.example" });
  const alive = await client.GET("/livez");
  const status: "alive" | undefined = alive.data?.status;
  const health = await client.GET("/readyz", {
    headers: { Authorization: "Bearer placeholder" },
  });
  await client.GET("/v1/research/projects/{project_id}", {
    params: { path: { project_id: "00000000-0000-4000-8000-000000000001" } },
  });
  const created = await client.POST("/v1/research/projects", {
    headers: { Authorization: "Bearer placeholder" },
    params: { header: { "idempotency-key": "sdk-compatibility" } },
    body: {
      organization_id: "00000000-0000-4000-8000-000000000001",
      name: "SDK compatibility",
      slug: "sdk-compatibility",
    },
  });
  // @ts-expect-error This operation exists only in the historical proposal.
  await client.GET("/v1/alerts/subscriptions");
  // @ts-expect-error A project ID is required in the generated path parameters.
  await client.GET("/v1/research/projects/{project_id}", { params: { path: {} } });
  return { status, health, created };
}

export type RuntimeClientCompatibility = ReturnType<typeof checkRuntimeClient>;
