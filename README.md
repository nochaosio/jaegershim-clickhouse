# ClickHouse Jaeger

**Jaeger Query API + Jaeger UI over OpenTelemetry traces already stored in ClickHouse.**

ClickHouse Jaeger reads the `otel_traces` table written by the OpenTelemetry Collector
[`clickhouse` exporter](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/clickhouseexporter)
(the same schema used by ClickStack/HyperDX and the Grafana ClickHouse plugin) and serves it through
the Jaeger APIs. Think of it as what ClickHouse's native PromQL support does for metrics, but for traces:
**no second copy of your traces, no new schema, no extra pipeline.**

```
Grafana (built-in Jaeger datasource) ──legacy /api/*──┐
                                                      ├──▶ clickhouse-jaeger ──SQL──▶ ClickHouse (otel_traces)
Jaeger UI 2.23 (bundled) ─────────────v3 /api/v3/*────┘
```

> **Status: v0.1.0, beta.** Behaviour is verified against real Jaeger (267/267 identical scenarios, see
> [Compatibility](#compatibility)), but it has not run in large production deployments yet. See
> [Production readiness](#production-readiness).

## Why
- You already ship traces to ClickHouse with the OTel Collector and want the **Jaeger search/trace UX**.
- **Grafana's built-in Jaeger datasource does not work with Jaeger ≥ 2.22** (the legacy `/api/*` API was
  removed). ClickHouse Jaeger serves **both** the legacy API (Grafana) and the v3 API (current Jaeger UI).
- Jaeger 2.18+ has a native ClickHouse backend (alpha) with its own schema; ClickHouse Jaeger instead
  works on the OTel schema you may already have, shared with ClickStack/HyperDX.

## Quick start

```bash
docker run -d -p 16686:16686 \
  -e CLICKHOUSE_ADDR=clickhouse:9000 \
  -e CLICKHOUSE_USER=jaeger_reader -e CLICKHOUSE_PASSWORD=... \
  ghcr.io/nochaosio/clickhouse-jaeger:latest      # after the first release; or build: docker build -t clickhouse-jaeger .
```
Open http://localhost:16686 (Jaeger UI), or add a **Jaeger** datasource in Grafana pointing to
`http://clickhouse-jaeger:16686`.

Full lab (ClickHouse, OTel Collector, Grafana, HyperDX, PromQL dashboards, MCP, load generators):
```bash
cd examples/stack && cp .env.example .env && docker compose up -d
```

## API

| API | Endpoints | Used by |
|---|---|---|
| legacy | `GET /api/services`, `/api/services/{svc}/operations`, `/api/operations?service=&spanKind=` | Grafana |
| legacy | `GET /api/traces?service=&operation=&tags=&minDuration=&maxDuration=&limit=&start=&end=&traceID=` | Grafana |
| legacy | `GET /api/traces/{id}`, `GET /api/dependencies?endTs=&lookback=` | Grafana, Jaeger UI |
| v3 | `GET /api/v3/services`, `/api/v3/operations`, `/api/v3/trace-summaries`, `/api/v3/traces`, `/api/v3/traces/{id}` | Jaeger UI 2.x |
| — | `GET /health`, `/` (Jaeger UI, SPA routes) | |

### Search semantics (same as Jaeger)
- All filters (operation, duration, tags) must match **the same span**; results are whole traces.
- Tags match span attributes, resource attributes (process tags) and event attributes (log fields).
- Searchable synthetic tags: `error=true|false`, `span.kind`. With `EXTENDED_TAG_SEARCH=true` also
  `otel.status_code`, `otel.status_description`, `otel.scope.name`, `otel.scope.version` (Jaeger shows them but cannot search them).

### Jaeger adjusters reproduced
Empty operation → `empty-span-name`; missing `service.name` → `missing-service-name`; Zipkin-style shared
span IDs (SpanIDUniquifier); zero-duration sanitizer; "parent span not in trace" warning (`@jaeger@warnings`
in v3); link to the parent span; 64-bit trace IDs printed with 16 chars in the legacy API; `orphanSpanCount`
in trace summaries; dependency edge for shared spans. Details in [docs/compatibility.md](docs/compatibility.md).

## Configuration

| Env var | Default | |
|---|---|---|
| `CLICKHOUSE_ADDR` | `localhost:9000` | native protocol |
| `CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD` | `default` / empty | use a read-only user (below) |
| `CLICKHOUSE_DATABASE` | `default` | |
| `TRACES_TABLE` / `TRACES_ID_TS_TABLE` | `otel_traces` / `otel_traces_trace_id_ts` | exporter defaults |
| `QUERY_TIMEOUT` | `25s` | keep below the user's `max_execution_time` |
| `SERVICES_LOOKBACK` | `168h` | window for service/operation lists |
| `EXTENDED_TAG_SEARCH` | `false` | see above |
| `CLICKHOUSE_MAX_OPEN_CONNS` | `10` | |
| `LISTEN_ADDR` / `UI_DIR` | `:16686` / `/ui` | empty `UI_DIR` disables the UI |

Recommended ClickHouse user ([examples/stack/clickhouse/initdb/02-jaeger-reader.sql](examples/stack/clickhouse/initdb/02-jaeger-reader.sql)):
```sql
CREATE SETTINGS PROFILE jaeger_reader_limits SETTINGS readonly = 2 CONST,
  max_execution_time = 30 MAX 30, max_memory_usage = 4000000000 MAX 4000000000, max_result_rows = 2000000 MAX 2000000;
CREATE USER jaeger_reader IDENTIFIED BY '...' SETTINGS PROFILE 'jaeger_reader_limits';
GRANT SELECT ON default.otel_traces TO jaeger_reader;
GRANT SELECT ON default.otel_traces_trace_id_ts TO jaeger_reader;
```
`readonly=2` (not 1) because the driver sends `max_execution_time` derived from the query timeout; `MAX`
caps it so clients cannot raise limits.

## Compatibility

Tested side by side with real Jaeger receiving **the same traces** from the same OTel Collector
(Jaeger 2.20 for the legacy API, Jaeger 2.23 for v3), including through Grafana's Jaeger datasource:

| Suite | Coverage | Result |
|---|---|---|
| [`test/compat/jaeger_compat.py`](test/compat/jaeger_compat.py) | 30 multi-service traces; 15 searches; trace by ID; dependencies; errors; via Grafana, legacy API, v3 API, Jaeger UI | **83/83 identical** |
| [`test/compat/jaeger_compat_varied.py`](test/compat/jaeger_compat_varied.py) | 24 edge-case traces: 1500 spans, 300-wide fan-out, 30-deep chain, orphans, multiple roots, async messaging + links, all span kinds, all attribute types, unicode, empty names, missing service.name, zero/1h durations, clock skew, Zipkin shared IDs, 64-bit trace IDs… | **184/184 identical** |
| `go test ./...` | params, model conversion, adjusters, HTTP contracts | pass |
| Headless Chromium | Jaeger UI search, trace view, dependency graph | renders, no JS errors |

Run them:
```bash
cd examples/stack && docker compose -f docker-compose.yml -f compat/docker-compose.jaeger-test.yml up -d
cd ../.. && python3 test/compat/jaeger_compat.py && python3 test/compat/jaeger_compat_varied.py
```

## Known limitations
- **Attribute types:** the exporter stores attributes as `Map(String,String)`, so ints/bools/arrays come back
  as strings (Jaeger shows `int64`/`bool`). Values and searches are identical.
- No archive storage, deep-dependency graph or Monitor (SPM) tab.
- Dependencies are computed on the fly (parent/child JOIN): expensive for long windows on large volumes.
- `flags` and `schemaUrl` are not stored by the exporter.

## Production readiness
Good for pilots and internal use. Before critical production: load-test with real volume (and consider
a `TraceId` projection / dependencies materialized view on `otel_traces`), add self-metrics, run 2+
replicas behind TLS and an authenticating proxy (Jaeger UI has no login), and keep the compatibility
suites in CI against new Jaeger UI / Grafana / exporter versions. See [docs/roadmap.md](docs/roadmap.md).

## Repository layout
```
main.go, internal/        Go service (api: handlers, models, adjusters; chstore: ClickHouse queries)
Dockerfile                multi-arch image with the official Jaeger UI assets
examples/stack/           full lab: ClickHouse, OTel Collector, Grafana (+PromQL dashboards), HyperDX, MCP
examples/stack/compat/    reference Jaeger / Prometheus overlays used by the test suites
test/compat/              Jaeger vs ClickHouse Jaeger comparison suites and trace generators
test/promql/              ClickHouse PromQL vs Prometheus comparison (lab study)
docs/                     compatibility details, roadmap, full lab report (pt-BR)
```

## License
Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Jaeger is a trademark of The Linux Foundation;
this project is not affiliated with the Jaeger project, ClickHouse, Inc. or Grafana Labs.
