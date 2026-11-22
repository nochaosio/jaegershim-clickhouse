#!/usr/bin/env python3
"""Gera traces com formatos/tipos de span bem diferentes (casos de borda) via OTLP/HTTP JSON.

Cada caso tem um trace ID próprio e o atributo `test.case=<nome>` em todos os spans,
mais `test.run_id=<run>`. Uso:
  python3 test/compat/gen_traces_varied.py --run RUN [--endpoint http://localhost:4318]
Imprime JSON: {"run":..., "cases": {nome: {"traceIds":[...], "service": svc_principal, ...}}}
"""
import argparse, base64, json, os, random, time, urllib.request

K = {"unspecified": 0, "internal": 1, "server": 2, "client": 3, "producer": 4, "consumer": 5}

def hid(n): return os.urandom(n).hex()

def av(v):
    if isinstance(v, bool): return {"boolValue": v}
    if isinstance(v, int): return {"intValue": str(v)}
    if isinstance(v, float): return {"doubleValue": v}
    if isinstance(v, bytes): return {"bytesValue": base64.b64encode(v).decode()}
    if isinstance(v, list): return {"arrayValue": {"values": [av(x) for x in v]}}
    if isinstance(v, dict): return {"kvlistValue": {"values": [{"key": k, "value": av(x)} for k, x in v.items()]}}
    return {"stringValue": str(v)}

def attrs(d): return [{"key": k, "value": av(v)} for k, v in d.items()]

class B:
    """Acumula spans por (resource, scope) e envia como um único ExportTraceServiceRequest."""
    def __init__(self, run, case):
        self.run, self.case, self.groups = run, case, {}
    def span(self, svc, trace_id, name, kind="internal", parent="", start=0, dur=1_000_000, a=None,
             events=None, links=None, status=None, span_id=None, res_extra=None, scope=("varied", "1.0"),
             trace_state="", no_service=False):
        sid = span_id or hid(8)
        res = {} if no_service else {"service.name": svc}
        res.update({"deployment.environment.name": "test"})
        res.update(res_extra or {})
        key = (json.dumps(res, sort_keys=True), scope)
        g = self.groups.setdefault(key, {"res": res, "scope": scope, "spans": []})
        sp = {"traceId": trace_id, "spanId": sid, "name": name, "kind": K[kind],
              "startTimeUnixNano": str(start), "endTimeUnixNano": str(start + dur),
              "attributes": attrs({"test.run_id": self.run, "test.case": self.case, **(a or {})})}
        if parent: sp["parentSpanId"] = parent
        if trace_state: sp["traceState"] = trace_state
        if events: sp["events"] = events
        if links: sp["links"] = links
        if status: sp["status"] = status
        g["spans"].append(sp)
        return sid
    def payload(self):
        rs = []
        for g in self.groups.values():
            sc = {}
            if g["scope"][0]: sc["name"] = g["scope"][0]
            if g["scope"][1]: sc["version"] = g["scope"][1]
            rs.append({"resource": {"attributes": attrs(g["res"])}, "scopeSpans": [{"scope": sc, "spans": g["spans"]}]})
        return {"resourceSpans": rs}

def ev(t, name, a=None):
    e = {"timeUnixNano": str(t), "name": name}
    if a: e["attributes"] = attrs(a)
    return e

def build_cases(run, now):
    MS = 1_000_000
    base = now - 120_000 * MS     # 2 min atrás
    cases = {}

    def case(name, service, fn):
        b = B(run, name)
        info = fn(b) or {}
        info.setdefault("service", service)
        cases[name] = (b, info)

    def single(b):
        t = hid(16); b.span("svc-single", t, "only-span", "server", start=base, dur=5 * MS)
        return {"traceIds": [t]}
    case("single_span", "svc-single", single)

    def all_kinds(b):
        t = hid(16); r = b.span("svc-kinds", t, "root", "server", start=base, dur=100 * MS)
        for i, k in enumerate(["unspecified", "internal", "server", "client", "producer", "consumer"]):
            b.span("svc-kinds", t, f"kind-{k}", k, parent=r, start=base + (i + 1) * MS, dur=10 * MS)
        return {"traceIds": [t]}
    case("all_kinds", "svc-kinds", all_kinds)

    def deep(b):
        t = hid(16); p = ""
        for d in range(30):
            p = b.span(["svc-deep-a", "svc-deep-b"][d % 2], t, f"level-{d}", "server" if d % 2 == 0 else "client",
                       parent=p, start=base + d * MS, dur=(200 - d * 5) * MS)
        return {"traceIds": [t], "service": "svc-deep-a"}
    case("deep_chain_30", "svc-deep-a", deep)

    def wide(b):
        t = hid(16); r = b.span("svc-wide", t, "fanout-root", "server", start=base, dur=500 * MS)
        for i in range(300):
            b.span("svc-wide-worker", t, f"task-{i % 10}", "client", parent=r, start=base + i * 1000, dur=2 * MS, a={"task.idx": i})
        return {"traceIds": [t], "service": "svc-wide"}
    case("wide_fanout_300", "svc-wide", wide)

    def large(b):
        t = hid(16); rnd = random.Random(42); ids = [b.span("svc-large-0", t, "large-root", "server", start=base, dur=2000 * MS)]
        for i in range(1, 1500):
            parent = ids[rnd.randrange(len(ids))]
            ids.append(b.span(f"svc-large-{i % 5}", t, f"op-{i % 25}", ["client", "server", "internal"][i % 3], parent=parent,
                              start=base + i * 100_000, dur=(1 + i % 50) * MS, a={"i": i}))
        return {"traceIds": [t], "service": "svc-large-0"}
    case("large_1500_spans", "svc-large-0", large)

    def orphan(b):
        t = hid(16); missing = hid(8)
        b.span("svc-orphan", t, "orphan-1", "server", parent=missing, start=base, dur=10 * MS)
        b.span("svc-orphan", t, "orphan-2", "client", parent=missing, start=base + MS, dur=5 * MS)
        return {"traceIds": [t]}
    case("orphan_no_root", "svc-orphan", orphan)

    def multiroot(b):
        t = hid(16)
        r1 = b.span("svc-multiroot", t, "root-a", "server", start=base, dur=10 * MS)
        b.span("svc-multiroot", t, "root-b", "server", start=base + 2 * MS, dur=10 * MS)
        b.span("svc-multiroot", t, "child-a", "internal", parent=r1, start=base + MS, dur=3 * MS)
        return {"traceIds": [t]}
    case("multi_root", "svc-multiroot", multiroot)

    def messaging(b):
        ta, tb = hid(16), hid(16)
        api = b.span("svc-orders", ta, "POST /orders", "server", start=base, dur=50 * MS)
        prod = b.span("svc-orders", ta, "orders publish", "producer", parent=api, start=base + 5 * MS, dur=2 * MS,
                      a={"messaging.system": "kafka", "messaging.destination.name": "orders"})
        b.span("svc-shipping", ta, "orders process", "consumer", parent=prod, start=base + 20 * MS, dur=30 * MS,
               a={"messaging.system": "kafka"})
        b.span("svc-billing", tb, "orders receive", "consumer", start=base + 25 * MS, dur=15 * MS,
               links=[{"traceId": ta, "spanId": prod, "attributes": attrs({"messaging.operation": "receive"})}],
               a={"messaging.system": "kafka"})
        return {"traceIds": [ta, tb], "service": "svc-orders"}
    case("messaging_async", "svc-orders", messaging)

    def links(b):
        t = hid(16); r = b.span("svc-links", t, "root", "server", start=base, dur=20 * MS)
        b.span("svc-links", t, "multi-linked", "internal", parent=r, start=base + MS, dur=5 * MS, links=[
            {"traceId": hid(16), "spanId": hid(8)},
            {"traceId": hid(16), "spanId": hid(8), "traceState": "vendor=abc", "attributes": attrs({"link.n": 2})},
            {"traceId": t, "spanId": r, "attributes": attrs({"same.trace": True})}])
        return {"traceIds": [t]}
    case("links_multiple", "svc-links", links)

    def many_events(b):
        t = hid(16)
        evs = [ev(base + i * 100_000, f"step-{i}", {"i": i} if i % 2 == 0 else None) for i in range(50)]
        evs.append(ev(base + 6 * MS, "", {"anon": "yes"}))
        evs.append(ev(base + 7 * MS, "exception", {"exception.type": "ValueError", "exception.message": "bad", "exception.stacktrace": "line1\nline2"}))
        b.span("svc-events", t, "eventful", "server", start=base, dur=10 * MS, events=evs)
        return {"traceIds": [t]}
    case("many_events_52", "svc-events", many_events)

    def statuses(b):
        t = hid(16); r = b.span("svc-status", t, "root-unset", "server", start=base, dur=50 * MS)
        b.span("svc-status", t, "ok", "internal", parent=r, start=base + MS, dur=MS, status={"code": 1})
        b.span("svc-status", t, "ok-with-msg", "internal", parent=r, start=base + 2 * MS, dur=MS, status={"code": 1, "message": "ignored?"})
        b.span("svc-status", t, "error-no-msg", "internal", parent=r, start=base + 3 * MS, dur=MS, status={"code": 2})
        b.span("svc-status", t, "error-msg", "internal", parent=r, start=base + 4 * MS, dur=MS, status={"code": 2, "message": "kaboom"})
        return {"traceIds": [t]}
    case("status_variants", "svc-status", statuses)

    def types(b):
        t = hid(16)
        b.span("svc-types", t, "typed", "server", start=base, dur=5 * MS, a={
            "a.str": "hello", "a.int": 42, "a.neg": -7, "a.double": 0.5, "a.double_big": 12345.678, "a.bool_t": True,
            "a.bool_f": False, "a.arr_str": ["x", "y"], "a.arr_int": [1, 2, 3], "a.map": {"k": "v", "n": 1},
            "a.bytes": b"\x01\x02\xff", "a.empty": "", "a.unicode": "ação 日本 ✓", "a.long": "L" * 10000,
            "chave com espaço": "ok", "ключ": "значение"})
        return {"traceIds": [t]}
    case("attribute_types", "svc-types", types)

    def collision(b):
        t = hid(16)
        b.span("svc-collision", t, "collide", "server", start=base, dur=5 * MS, a={"region": "span-value"},
               res_extra={"region": "resource-value"})
        return {"traceIds": [t]}
    case("span_resource_collision", "svc-collision", collision)

    def instances(b):
        t = hid(16); r = b.span("svc-multi-inst", t, "lb", "server", start=base, dur=30 * MS, res_extra={"host.name": "node-1", "service.instance.id": "i-1"})
        b.span("svc-multi-inst", t, "handle", "server", parent=r, start=base + MS, dur=10 * MS, res_extra={"host.name": "node-2", "service.instance.id": "i-2"})
        return {"traceIds": [t]}
    case("two_instances_same_service", "svc-multi-inst", instances)

    def noservice(b):
        t = hid(16); b.span("", t, "anonymous", "server", start=base, dur=5 * MS, no_service=True)
        return {"traceIds": [t], "service": None}
    case("no_service_name", None, noservice)

    def scopes(b):
        t = hid(16); r = b.span("svc-scopes", t, "s-root", "server", start=base, dur=20 * MS, scope=("lib.http", "2.1"))
        b.span("svc-scopes", t, "s-db", "client", parent=r, start=base + MS, dur=5 * MS, scope=("lib.db", ""))
        b.span("svc-scopes", t, "s-anon", "internal", parent=r, start=base + 2 * MS, dur=5 * MS, scope=("", ""))
        return {"traceIds": [t]}
    case("multiple_scopes", "svc-scopes", scopes)

    def tstate(b):
        t = hid(16); b.span("svc-tracestate", t, "with-state", "server", start=base, dur=5 * MS, trace_state="rojo=00f067aa0ba902b7,congo=t61rcWkgMzE")
        return {"traceIds": [t]}
    case("trace_state", "svc-tracestate", tstate)

    def durations(b):
        t = hid(16); r = b.span("svc-durations", t, "one-hour", "server", start=now - 65 * 60_000 * MS, dur=60 * 60_000 * MS)
        b.span("svc-durations", t, "zero", "internal", parent=r, start=now - 64 * 60_000 * MS, dur=0)
        b.span("svc-durations", t, "one-ns", "internal", parent=r, start=now - 63 * 60_000 * MS, dur=1)
        return {"traceIds": [t], "window_min": 70}
    case("zero_and_1h_duration", "svc-durations", durations)

    def skew(b):
        t = hid(16); r = b.span("svc-skew-parent", t, "parent", "client", start=base + 10 * MS, dur=10 * MS)
        b.span("svc-skew-child", t, "child-earlier", "server", parent=r, start=base, dur=40 * MS)
        return {"traceIds": [t], "service": "svc-skew-parent"}
    case("clock_skew_child_outside_parent", "svc-skew-parent", skew)

    def dup(b):
        t = hid(16); shared = hid(8)
        r = b.span("svc-dup-client", t, "rpc", "client", start=base, dur=20 * MS, span_id=shared)
        b.span("svc-dup-server", t, "rpc", "server", start=base + MS, dur=15 * MS, span_id=shared)
        return {"traceIds": [t], "service": "svc-dup-client"}
    case("duplicate_span_id_zipkin_style", "svc-dup-client", dup)

    def zeros(b):
        t = "0" * 16 + hid(8); b.span("svc-zeros", t, "low-64bit-id", "server", start=base, dur=5 * MS)
        return {"traceIds": [t]}
    case("trace_id_leading_zeros", "svc-zeros", zeros)

    def unicode(b):
        t = hid(16); r = b.span("serviço-ação 日本", t, "GET /usuários/{id} ✓", "server", start=base, dur=10 * MS)
        b.span("serviço-ação 日本", t, "SELECT * FROM \"tabela\"", "client", parent=r, start=base + MS, dur=2 * MS)
        return {"traceIds": [t], "service": "serviço-ação 日本"}
    case("unicode_names", "serviço-ação 日本", unicode)

    def emptyname(b):
        t = hid(16); b.span("svc-emptyname", t, "", "server", start=base, dur=5 * MS)
        return {"traceIds": [t]}
    case("empty_span_name", "svc-emptyname", emptyname)

    def old(b):
        t = hid(16); b.span("svc-old", t, "thirty-min-ago", "server", start=now - 30 * 60_000 * MS, dur=5 * MS)
        return {"traceIds": [t], "window_min": 40}
    case("old_timestamp_30min", "svc-old", old)

    return cases

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--endpoint", default="http://localhost:4318")
    a = ap.parse_args()
    now = time.time_ns()
    out = {"run": a.run, "now_ns": now, "cases": {}}
    for name, (b, info) in build_cases(a.run, now).items():
        req = urllib.request.Request(f"{a.endpoint}/v1/traces", json.dumps(b.payload()).encode(), {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
        info["span_count"] = sum(len(g["spans"]) for g in b.groups.values())
        out["cases"][name] = info
    print(json.dumps(out))

if __name__ == "__main__":
    main()
