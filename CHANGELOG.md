# Changelog

## [0.1.0] - 2026-11-22
### Added
- Jaeger Query legacy API (`/api/*`) for Grafana's built-in Jaeger datasource.
- Jaeger v3 API (`/api/v3/*`: services, operations, trace-summaries, traces, traces/{id}) for Jaeger UI 2.x.
- Bundled Jaeger UI v2.23.0 (official assets).
- Reads OpenTelemetry `otel_traces` / `otel_traces_trace_id_ts` written by the OTel Collector `clickhouse` exporter.
- Jaeger-compatible search semantics and adjusters (empty names, Zipkin shared span IDs, zero duration,
  orphan warnings, parent links, 64-bit trace IDs, orphan span counts).
- `EXTENDED_TAG_SEARCH` to search `otel.status_code`, `otel.status_description`, `otel.scope.*`.
- Compatibility suites against real Jaeger 2.20/2.23 and Grafana 13 (267/267 identical scenarios).
