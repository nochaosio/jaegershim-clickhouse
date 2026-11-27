# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [SemVer](https://semver.org/).

## [Unreleased]
### Changed
- The project is renamed to **jaegershim** to match the repository. The Go module is now
  `github.com/nochaosio/jaegershim-clickhouse`, and the image is `ghcr.io/nochaosio/jaegershim-clickhouse`
  (v0.1.0 was published as `ghcr.io/nochaosio/clickhouse-jaeger`). The binary, the compose service and
  the Grafana datasource are now `jaegershim`, and the lab variable `CLICKHOUSE_JAEGER_VERSION` is now `JAEGERSHIM_VERSION`.
- The README is shorter. The compatibility workflow moved to CONTRIBUTING.md.

### Added
- Code of conduct, issue/PR templates and Dependabot.

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

[Unreleased]: https://github.com/nochaosio/jaegershim-clickhouse/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nochaosio/jaegershim-clickhouse/releases/tag/v0.1.0
