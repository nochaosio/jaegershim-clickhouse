# Compatibility with Jaeger

## Method
The OTel Collector exports **the same traces** to three backends at once:

| Backend | Role |
|---|---|
| ClickHouse (`otel_traces`) + **jaegershim** | system under test |
| Jaeger **2.20** (in-memory) | reference for the **legacy** `/api/*` API (the one Grafana uses; removed in Jaeger 2.22) |
| Jaeger **2.23** (in-memory) | reference for the **v3** `/api/v3/*` API (the one Jaeger UI 2.x uses) |

Each scenario is executed against both sides and the full responses are normalized and compared
(trace sets, every span field, tags, logs, references, processes, OTLP attributes/events/links/status,
summaries, dependencies, HTTP status codes and error messages). Search results are also checked against
an independent answer key computed from the generator, so both sides cannot be wrong together.
Grafana is exercised through its own `/api/ds/query` endpoint with two built-in Jaeger datasources:
"Jaeger (original 2.20)" and "jaegershim".

Attribute values are compared by their canonical string (OTel `AsString()`); differences **only in type**
are counted separately (see *Known differences*).

## Results (2026-11-22)

| Suite | Scenarios | Result |
|---|---|---|
| `jaeger_compat.py` — 30 multi-service traces | Grafana 25, legacy 30, v3 24, Jaeger UI 4 | **83/83** |
| `jaeger_compat_varied.py` — 24 edge-case traces (1,877 spans) | trace by ID ×3 APIs 75, per-case search 72, feature searches 24, metadata/dependencies 13 | **184/184** |

Edge cases covered: single span; all span kinds; 30-level chain; 300-child fan-out; 1,500-span trace;
orphan spans; multiple roots; async messaging (producer/consumer, cross-trace links); multiple links with
trace state/attributes; 52 events (unnamed, without attributes, exception with stack trace); status
unset/ok/ok+message/error/error+message; int, negative int, double, bool, string array, int array, map,
bytes, empty string, unicode, 10 KB value, key with spaces, Cyrillic key; span vs resource attribute
collision; two instances of one service; missing `service.name`; multiple scopes (incl. empty); trace
state; zero and 1-hour durations; child outside parent (clock skew); Zipkin-style shared span ID;
trace ID with zero high bits; unicode service/operation names; empty span name; 30-minute-old trace.

## Jaeger behaviours reproduced (found by the suites)

| Behaviour | Jaeger | jaegershim |
|---|---|---|
| Searchable synthetic tags | only `error`, `span.kind` | same (others behind `EXTENDED_TAG_SEARCH`) |
| Empty span name | `empty-span-name` | same |
| Missing `service.name` | `missing-service-name` | same (lists, search, traces, dependencies) |
| Client/server sharing a span ID | server gets a new ID (`0000000000000001`), becomes child of client | same + dependency edge |
| Zero duration | end = start + 1 ns, warning | same text |
| Parent not in trace | warning (`@jaeger@warnings` in v3), summaries without root + `orphanSpanCount` | same |
| Link pointing to the parent | single `FOLLOWS_FROM`, no `CHILD_OF` | same |
| Trace ID with zero high 64 bits | 16 chars in legacy API | same |
| Invalid trace ID | `invalid length for TraceID` | same |
| `unspecified` span kind in operations | `""` (legacy), `internal` (v3) | same |

## Known differences
- **Attribute types**: Jaeger keeps `int64`/`float64`/`bool`/`binary`/arrays; ClickHouse stores
  `Map(String,String)`, so everything is a string. 1,806 attributes in the suites differ **only in type**.
- **Jaeger 2.20 bug**: the "parent span not in trace" warning accumulates on every request (the adjuster
  mutates the in-memory trace). Jaeger 2.23 and jaegershim return it once; the suites compare
  warnings without duplicates.
- Latency is not comparable with the in-memory reference (~1 ms vs ~10 ms).
