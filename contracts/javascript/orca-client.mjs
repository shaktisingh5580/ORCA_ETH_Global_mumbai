/** Purpose: connect a browser frontend to implemented ORCA identity and research APIs.
 * Behavior: obtain a current user access token for each request and preserve HTTP errors.
 * Refs: API 3.2; backend README frontend connection; Phase: demo frontend integration.
 */
export function createOrcaClient({ baseUrl = "http://127.0.0.1:8000", getAccessToken }) {
  if (typeof getAccessToken !== "function") throw new Error("A current-session access-token callback is required");
  const base = baseUrl.replace(/\/$/, "");
  async function request(path, options = {}) {
    const token = await getAccessToken();
    if (!token) throw new Error("Please sign in with your Supabase user account first");
    const response = await fetch(base + path, {
      ...options, credentials: "omit",
      headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...options.headers, Authorization: `Bearer ${token}` },
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}: ${body?.detail ?? body?.title ?? response.statusText}`);
      error.status = response.status;
      error.body = body;
      throw error;
    }
    return body;
  }
  return {
    me: () => request("/v1/me"),
    organizations: ({ limit = 20, cursor } = {}) => {
      const query = new URLSearchParams({ limit: String(limit) });
      if (cursor) query.set("cursor", cursor);
      return request(`/v1/organizations?${query}`);
    },
    result: (resultId, organizationId) => request(`/v1/demo/research-runs/${encodeURIComponent(resultId)}?${new URLSearchParams({ organization_id: organizationId })}`),
    run: (organizationId, snapshotId) => request("/v1/demo/research-runs", { method: "POST", body: JSON.stringify({ organization_id: organizationId, snapshot_id: snapshotId }) }),
    publicExport: (resultId, organizationId, snapshotId) => request(`/v1/demo/research-runs/${encodeURIComponent(resultId)}/public-export`, { method: "POST", body: JSON.stringify({ organization_id: organizationId, snapshot_id: snapshotId }) }),
  };
}
