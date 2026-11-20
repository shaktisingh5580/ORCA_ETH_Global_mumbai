# ORCA on-call

Purpose: operate the research-federated release. References: P14.5; API design §14.

Before staging, assign the roles in the [owner matrix](../releases/owner_matrix.md). Record the release/profile, code revision, image digests and migration revision. Keep contact details and callback credentials in the operator's restricted directory.

Schedule `python infra/observability/health_alert.py` from a runner independent of the ORCA host. Supply `ORCA_MONITOR_ORIGIN` (HTTPS origin), `ORCA_MONITORING_TOKEN` (readiness credential), and `ORCA_PLATFORM_ALERT_URL` (dedicated HTTPS receiver). The receiver must accept a POST with `alert`, `owner`, and `http_status`; it must authenticate or restrict the runner at the receiver boundary. Group repeats by alert name and monitored instance in the receiver. Alert only on repeated failures in the external scheduler if transient noise is unacceptable. This one-shot runner exits 0 when healthy, 1 after acknowledged delivery of unhealthy signals, and 2 when a receiver fails or configuration is invalid. A 2xx acknowledges the request; an operator must still run a staging drill and confirm the named primary and backup actually receive it. Use scheduler alerts for missed runs and delivery failures.

| Signal | First response |
| --- | --- |
| `/livez` non-200 or unavailable | Platform checks routing and process health. Inspect logs by request ID. Escalate to the release owner if the last change correlated with failure. |
| `/readyz` non-200 | Platform checks the protected readiness response from the restricted console. Separate invalid monitoring credentials from a database or approved-science dependency failure. Keep the failing instance out of traffic. |
| Queue stalls or overdue leases | Check `/v1/admin/job-health` with an authorized organization and `ops:read`. Inspect worker progress and fencing before replacing workers. Do not infer a worker is online from a queue count. |
| Source errors | Use `/v1/admin/source-health` with the authorized source operator. Respect source backoff; preserve exact input revisions when the method allows. |
| Run admissions return 429 | Respect `Retry-After`. Read organization quota and usage with authorized access; use the original idempotency key for the same logical request. Rejected admissions do not create a job or reserve budget. A change needs owner approval, the current quota ETag, and an audit check. Usage reservations are separate from actual spend. |
| Unexpected disclosure, license withdrawal, or uncertain chain write | Restrict access; preserve incident evidence; contact the appropriate owner. Follow [security response](security_incident.md) or [Arkiv reconciliation](arkiv_reconciliation.md) rather than replaying an uncertain write. |

For each drill save the UTC time, profile, failure injected, redacted signal, acknowledgement by the receiving person, time to recover and disposition. Do not paste bearer tokens, private URLs, or research records into tickets.
