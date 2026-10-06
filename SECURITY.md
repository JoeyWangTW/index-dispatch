# Security and operations

This prototype is for one owner per deployment. Do not expose it as a public multi-user service without an authentication, tenancy and operational review.

Keep `.env`, the data volume, backups, bearer tokens and encryption key private. The app does not log transcripts or upstream errors; deployment proxy logs may contain OAuth query parameters. Disable or redact query-string access logs. Uvicorn access logging is disabled in the container.

Admin and Index tokens are distinct. The Index token can only add requests. Dots gets scoped OAuth access to tasks delivered to its connection; it cannot read notes or administer the server. Access tokens are hashed in storage. Subscription signing secrets are encrypted; transcripts and task results are plaintext on the protected volume. The admin token authorizes both dashboard access and new OAuth connections.

Revocation: use the authenticated dashboard's Disconnect Dots control. This deactivates subscriptions and deletes OAuth registrations and tokens. Update `INDEX_TOKEN`/`ADMIN_TOKEN` through private deployment variables when rotating those secrets. Keep `ENCRYPTION_KEY` stable unless intentionally discarding and reconnecting subscriptions.

Outbound callback requests enforce HTTPS, exact hostname allowlists, public address checks on connection and certificate validation. The transport pins the validated address and does not follow redirects. Never loosen these checks to debug a callback failure.

Tests do not replace a security audit. No production credentials are required for the test suite. Report suspected vulnerabilities privately to the repository owner rather than including secret values in issues.
