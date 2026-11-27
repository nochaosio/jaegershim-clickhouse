# Security Policy

Please report vulnerabilities privately through GitHub Security Advisories
("Report a vulnerability" on the repository's Security tab). Do not open public issues for security reports.

Deployment notes:
- Use a dedicated read-only ClickHouse user with `SETTINGS PROFILE` limits (see [examples/stack/clickhouse/initdb/02-jaeger-reader.sql](examples/stack/clickhouse/initdb/02-jaeger-reader.sql)).
- The bundled Jaeger UI has no authentication: put jaegershim behind an authenticating proxy and TLS.
