# Roadmap

## v0.1.x — usable (current)
- [x] Jaeger legacy API (Grafana) and v3 API (Jaeger UI 2.x)
- [x] Jaeger adjusters, search semantics and error messages verified against real Jaeger
- [x] Read-only ClickHouse user with server-side limits
- [x] CI (gofmt, vet, race tests, multi-arch image build) and tagged releases to GHCR

## v0.2 — production hardening
- [ ] Prometheus `/metrics` (request latency, ClickHouse query latency/errors) and OTel self-tracing
- [ ] Concurrency limit + short cache for `services` / `operations`
- [ ] Load tests with realistic volume (millions of spans/day, concurrent searches); publish results
- [ ] Optional DDL helpers: `TraceId` projection and a dependencies materialized view on `otel_traces`
- [ ] Helm chart (Artifact Hub), signed images (cosign) and SBOM attestation
- [ ] Compatibility suites in CI against the latest Jaeger UI / Grafana / exporter

## v0.3 — ecosystem
- [ ] **Jaeger Remote Storage gRPC API** (TraceReader + DependencyReader) so the official Jaeger v2
      binary can use `otel_traces` as a `grpc` storage backend
- [ ] Support the exporter's JSON/typed attribute schema (preserves attribute types)
- [ ] Multi-tenancy (tenant header → database/table)

## Publishing checklist
1. Public repo, Apache-2.0, README in English, CHANGELOG, SECURITY ✅
2. First tag `v0.1.0` → image `ghcr.io/nochaosio/clickhouse-jaeger` (release workflow)
3. Submit to the [OpenTelemetry Registry](https://opentelemetry.io/ecosystem/registry/) (PR to `open-telemetry/opentelemetry.io`, `data/registry/`)
4. Propose a "third-party storage / compatible query service" mention to Jaeger (issue/discussion in `jaegertracing/jaeger`), ideally after the Remote Storage gRPC API
5. Share with ClickHouse (ClickStack community / integrations docs) and Grafana community (Jaeger datasource works with it)
6. Helm chart on Artifact Hub; announce (CNCF Slack `#jaeger`, ClickHouse community Slack, blog post with the compatibility results)
