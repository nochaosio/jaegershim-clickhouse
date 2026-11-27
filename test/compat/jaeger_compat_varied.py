#!/usr/bin/env python3
"""De-para com tipos de span/trace variados: Jaeger original x jaegershim.

Usa test/compat/gen_traces_varied.py (24 casos de borda) e compara, por caso:
  - trace por ID na API legacy (Jaeger 2.20 x jaegershim)
  - trace por ID via Grafana (plugin Jaeger: "Jaeger (original 2.20)" x "jaegershim")
  - trace por ID na API v3/OTLP (Jaeger 2.23 x jaegershim)
  - busca por caso (legacy, v3 trace-summaries, Grafana)
e buscas por recurso específico (kinds, tipos de atributo, eventos, colisão, unicode, durações...).

Valores de atributos são comparados pelo valor canônico (como o OTel `AsString()`);
diferenças só de TIPO (int64/bool/float64/binary/array no Jaeger x string no ClickHouse) são
contadas à parte, porque o exporter do ClickHouse grava Map(String,String).
"""
import argparse, json, os, subprocess, sys, time, urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jaeger_compat import (OURS, REF_LEGACY, REF_V3, GRAFANA, G_AUTH, DS_OURS, DS_REF, http, jget, gquery, frame_rows, ensure_reference_datasource)

results = []

def record(section, name, ok, detail="", note=""):
    results.append(dict(section=section, name=name, ok=bool(ok), detail=detail, note=note))
    print(f"  [{'OK  ' if ok else 'DIFF'}] {name}" + (f"  -> {detail}" if detail and not ok else (f"  ({detail})" if detail else "")) + (f"  [obs: {note}]" if note else ""))

# ---------- valores canônicos ----------

def gofloat(f):
    f = float(f)
    return str(int(f)) if f.is_integer() and abs(f) < 1e21 else repr(f)

def any_to_raw(v):
    if not v: return ""
    (k, x), = v.items()
    if k == "stringValue": return x
    if k == "intValue": return int(x)
    if k == "doubleValue": return float(x)
    if k == "boolValue": return bool(x)
    if k == "bytesValue": return x
    if k == "arrayValue": return [any_to_raw(i) for i in x.get("values", [])]
    if k == "kvlistValue": return {i["key"]: any_to_raw(i.get("value", {})) for i in x.get("values", [])}
    return x

def canon(raw):
    if isinstance(raw, bool): return "true" if raw else "false"
    if isinstance(raw, int): return str(raw)
    if isinstance(raw, float): return gofloat(raw)
    if isinstance(raw, (list, dict)): return json.dumps(raw, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    return raw

def any_canon(v):
    return canon(any_to_raw(v))

def any_type(v):
    return next(iter(v), "stringValue") if v else "stringValue"

def kv_canon(x):
    t, v = x.get("type", "string"), x.get("value")
    if t == "bool": return "true" if v else "false"
    if t == "int64": return str(int(v))
    if t == "float64": return gofloat(v)
    return v if isinstance(v, str) else canon(v)

# ---------- normalização ----------

def norm_legacy(t, types):
    procs = {pid: (p["serviceName"], tuple(sorted((x["key"], kv_canon(x)) for x in p["tags"]))) for pid, p in t["processes"].items()}
    out = {}
    for s in t["spans"]:
        for x in s["tags"]:
            types.append(x.get("type"))
        key = (s["spanID"], procs[s["processID"]][0], s["operationName"])
        out.setdefault(key, []).append(dict(
            op=s["operationName"], start=s["startTime"], dur=s["duration"],
            refs=tuple(sorted((r["refType"], r["traceID"], r["spanID"]) for r in s["references"])),
            tags=tuple(sorted((x["key"], kv_canon(x)) for x in s["tags"])),
            logs=tuple(sorted((l["timestamp"], tuple(sorted((f["key"], kv_canon(f)) for f in l["fields"]))) for l in s["logs"])),
            process=procs[s["processID"]], warnings=tuple(sorted(set(s.get("warnings") or ())))))
    return out

def norm_otlp(result, types):
    out = {}
    for rs in result.get("resourceSpans", []):
        res = tuple(sorted((a["key"], any_canon(a.get("value"))) for a in rs["resource"].get("attributes", [])))
        for ss in rs.get("scopeSpans", []):
            scope = (ss.get("scope", {}).get("name", ""), ss.get("scope", {}).get("version", ""))
            for s in ss["spans"]:
                for a in s.get("attributes", []):
                    types.append(any_type(a.get("value")))
                key = (s["spanId"], dict(res).get("service.name", ""), s["name"])
                out.setdefault(key, []).append(dict(
                    kind=s.get("kind", 0), parent=s.get("parentSpanId", ""), start=s["startTimeUnixNano"], end=s["endTimeUnixNano"],
                    state=s.get("traceState", ""),
                    attrs=tuple(sorted((a["key"], any_canon(a.get("value"))) for a in s.get("attributes", []))),
                    events=tuple(sorted((e.get("timeUnixNano"), e.get("name", ""), tuple(sorted((a["key"], any_canon(a.get("value"))) for a in e.get("attributes", [])))) for e in s.get("events", []))),
                    links=tuple(sorted((l["traceId"], l["spanId"], l.get("traceState", ""), tuple(sorted((a["key"], any_canon(a.get("value"))) for a in l.get("attributes", [])))) for l in s.get("links", []))),
                    status=(s.get("status", {}).get("code", 0), s.get("status", {}).get("message", "")),
                    resource=res, scope=scope))
    return out

def norm_grafana(frames):
    out = {}
    for f in frames:
        for r in frame_rows(f):
            nr = {}
            for k, v in r.items():
                if k in ("tags", "serviceTags") and v:
                    v = tuple(sorted((x["key"], canon(x.get("value"))) for x in v))
                elif k == "logs" and v:
                    v = tuple(sorted((l["timestamp"], tuple(sorted((x["key"], canon(x.get("value"))) for x in l.get("fields", [])))) for l in v))
                elif k == "references" and v:
                    v = tuple(sorted((x.get("traceID"), x.get("spanID")) for x in v))
                elif k == "warnings" and v:
                    v = tuple(sorted(set(v)))
                elif isinstance(v, (list, dict)):
                    v = json.dumps(v, sort_keys=True)
                nr[k] = v
            out.setdefault((r.get("spanID"), r.get("serviceName"), r.get("operationName")), []).append(nr)
    return {k: sorted(v, key=lambda d: json.dumps(d, sort_keys=True, default=str)) for k, v in out.items()}

def first_diff(a, b):
    for k in sorted(set(a) | set(b), key=str):
        if k not in a: return f"span {k} só no Jaeger"
        if k not in b: return f"span {k} só no ClickHouse"
        if a[k] != b[k]:
            if len(a[k]) != len(b[k]): return f"span {k}: {len(a[k])} x {len(b[k])} ocorrências"
            for x, y in zip(a[k], b[k]):
                for f in x:
                    if x[f] != y.get(f):
                        sx, sy = set(x[f]) if isinstance(x[f], tuple) else x[f], set(y[f]) if isinstance(y.get(f), tuple) else y.get(f)
                        if isinstance(sx, set):
                            return f"span {k[2]!r}.{f}: só ClickHouse={sorted(sx - sy, key=str)[:3]} só Jaeger={sorted(sy - sx, key=str)[:3]}"
                        return f"span {k[2]!r}.{f}: ClickHouse={str(x[f])[:120]} Jaeger={str(y.get(f))[:120]}"
    return ""

def iso(t): return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--json"); a = ap.parse_args()
    ensure_reference_datasource()
    run = f"var{int(time.time())}"
    print(f"== gerando casos de borda (run={run}) ...")
    gen = json.loads(subprocess.run([sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen_traces_varied.py"), "--run", run], capture_output=True, text=True, check=True).stdout)
    cases = gen["cases"]
    now = gen["now_ns"] / 1e9
    start, end = now - 75 * 60, now + 120
    s_us, e_us = int(start * 1e6), int(end * 1e6)
    total_spans = sum(c["span_count"] for c in cases.values())
    print(f"   {len(cases)} casos, {total_spans} spans")
    # espera ingestão nos 3 backends (pelo trace grande)
    tid_large = cases["large_1500_spans"]["traceIds"][0]
    for _ in range(40):
        time.sleep(2)
        c1 = jget(OURS, f"/api/traces/{tid_large}")[1]; c2 = jget(REF_LEGACY, f"/api/traces/{tid_large}")[1]; c3 = jget(REF_V3, f"/api/v3/traces/{tid_large}")[1]
        n1 = len(c1["data"][0]["spans"]) if c1.get("data") else 0
        n2 = len(c2["data"][0]["spans"]) if c2.get("data") else 0
        n3 = sum(len(ss["spans"]) for rs in c3.get("result", {}).get("resourceSpans", []) for ss in rs["scopeSpans"]) if isinstance(c3, dict) and "result" in c3 else 0
        if n1 == n2 == n3 == 1500: break
    print(f"   trace de 1500 spans: clickhouse={n1} jaeger2.20={n2} jaeger2.23={n3}\n")

    type_diffs = {}
    print("== 1. Trace por ID, caso a caso (legacy | Grafana | v3)")
    for name, c in cases.items():
        for tid in c["traceIds"]:
            tl, tj, tv = [], [], []
            c1, d1 = jget(OURS, f"/api/traces/{tid}"); c2, d2 = jget(REF_LEGACY, f"/api/traces/{tid}")
            if c1 != 200 or c2 != 200:
                record("1-trace", f"{name} legacy", c1 == c2, f"http ClickHouse={c1} Jaeger={c2} {str(d1)[:150]} | {str(d2)[:150]}")
            else:
                n1, n2 = norm_legacy(d1["data"][0], tl), norm_legacy(d2["data"][0], tj)
                record("1-trace", f"{name} legacy ({len(d1['data'][0]['spans'])} spans)", n1 == n2, first_diff(n1, n2))
                nt = sum(1 for t in tj if t not in ("string",)) - sum(1 for t in tl if t not in ("string",))
                if nt: type_diffs[name] = type_diffs.get(name, 0) + nt
            e1, f1 = gquery(DS_OURS, {"query": tid}, start, end); e2, f2 = gquery(DS_REF, {"query": tid}, start, end)
            g1, g2 = norm_grafana(f1), norm_grafana(f2)
            record("1-trace", f"{name} Grafana", not e1 and not e2 and g1 == g2 and len(g1) > 0, (f"err={e1 or e2}" if e1 or e2 else first_diff(g1, g2)))
            c1, d1 = jget(OURS, f"/api/v3/traces/{tid}"); c2, d2 = jget(REF_V3, f"/api/v3/traces/{tid}")
            if c1 != 200 or c2 != 200:
                record("1-trace", f"{name} v3", c1 == c2, f"http ClickHouse={c1} Jaeger={c2}")
            else:
                v1, v2 = norm_otlp(d1["result"], []), norm_otlp(d2["result"], tv)
                record("1-trace", f"{name} v3/OTLP", v1 == v2, first_diff(v1, v2))

    print("\n== 2. Busca por caso (test.case) — legacy | v3 trace-summaries | Grafana")
    for name, c in cases.items():
        svc = c.get("service") or "missing-service-name"
        tags = {"test.run_id": run, "test.case": name}
        lp = {"tags": json.dumps(tags), "start": s_us, "end": e_us, "limit": 100}
        if svc: lp["service"] = svc
        if not svc:
            record("2-busca", f"{name} (sem service.name)", True, "pulado: API exige serviço", note="ver seção 3")
            continue
        _, d1 = jget(OURS, "/api/traces", lp); _, d2 = jget(REF_LEGACY, "/api/traces", lp)
        i1 = {t["traceID"]: norm_legacy(t, []) for t in d1.get("data") or []}; i2 = {t["traceID"]: norm_legacy(t, []) for t in d2.get("data") or []}
        record("2-busca", f"{name} legacy", set(i1) == set(i2) and i1 == i2 and len(i1) > 0, f"{len(i1)} traces" if i1 == i2 else f"ClickHouse={len(i1)} Jaeger={len(i2)} {next((first_diff(i1[k], i2[k]) for k in i1 if k in i2 and i1[k] != i2[k]), '')}")
        vp = {"query.serviceName": svc, "query.startTimeMin": iso(start), "query.startTimeMax": iso(end), "query.searchDepth": 100, "query.attributes": json.dumps(tags)}
        _, s1 = jget(OURS, "/api/v3/trace-summaries", vp); _, s2 = jget(REF_V3, "/api/v3/trace-summaries", vp)
        m1 = {x["traceId"]: x for x in s1.get("summaries") or []}; m2 = {x["traceId"]: x for x in s2.get("summaries") or []}
        record("2-busca", f"{name} v3 summaries", m1 == m2 and len(m1) > 0, f"{len(m1)} resumos" if m1 == m2 else f"ClickHouse={len(m1)} Jaeger={len(m2)} ex={next(({f: (m1[k].get(f), m2[k].get(f)) for f in set(m1[k]) | set(m2[k]) if m1[k].get(f) != m2[k].get(f)} for k in m1 if k in m2 and m1[k] != m2[k]), '')}")
        q = {"queryType": "search", "service": svc, "tags": " ".join(f"{k}={v}" for k, v in tags.items()), "limit": 100}
        e1, f1 = gquery(DS_OURS, q, start, end); e2, f2 = gquery(DS_REF, q, start, end)
        r1 = {r["traceID"]: r for f in f1 for r in frame_rows(f)}; r2 = {r["traceID"]: r for f in f2 for r in frame_rows(f)}
        record("2-busca", f"{name} Grafana", not e1 and not e2 and r1 == r2 and len(r1) > 0, f"{len(r1)} traces" if r1 == r2 else f"ClickHouse={list(r1.values())[:1]} Jaeger={list(r2.values())[:1]}")

    print("\n== 3. Buscas por recurso (legacy + v3 + Grafana devem bater com o Jaeger)")
    R = {"test.run_id": run}
    feats = [
        ("span.kind=producer", "svc-orders", {"span.kind": "producer"}, {}),
        ("span.kind=consumer", "svc-shipping", {"span.kind": "consumer"}, {}),
        ("span.kind=unspecified", "svc-kinds", {"span.kind": "unspecified"}, {}),
        ("bool a.bool_t=true", "svc-types", {"a.bool_t": "true"}, {}),
        ("int a.int=42", "svc-types", {"a.int": "42"}, {}),
        ("int negativo a.neg=-7", "svc-types", {"a.neg": "-7"}, {}),
        ("double a.double=0.5", "svc-types", {"a.double": "0.5"}, {}),
        ("array a.arr_str", "svc-types", {"a.arr_str": '["x","y"]'}, {}),
        ("map a.map", "svc-types", {"a.map": '{"k":"v","n":1}'}, {}),
        ("string vazia a.empty=''", "svc-types", {"a.empty": ""}, {}),
        ("unicode a.unicode", "svc-types", {"a.unicode": "ação 日本 ✓"}, {}),
        ("chave com espaço", "svc-types", {"chave com espaço": "ok"}, {}),
        ("valor de 10KB", "svc-types", {"a.long": "L" * 10000}, {}),
        ("colisão: valor do span", "svc-collision", {"region": "span-value"}, {}),
        ("colisão: valor do resource", "svc-collision", {"region": "resource-value"}, {}),
        ("campo de evento exception.type", "svc-events", {"exception.type": "ValueError"}, {}),
        ("campo de evento int i=4", "svc-events", {"i": "4"}, {}),
        ("minDuration=30m (span de 1h)", "svc-durations", {}, {"minDuration": "30m"}),
        ("maxDuration=1us (span de 0ns/1ns)", "svc-durations", {}, {"maxDuration": "1us"}),
        ("error=true (status sem mensagem)", "svc-status", {"error": "true"}, {}),
        ("operação vazia", "svc-emptyname", {}, {"operation": ""}),
        ("operação unicode", "serviço-ação 日本", {}, {"operation": "GET /usuários/{id} ✓"}),
        ("host.name de instância", "svc-multi-inst", {"host.name": "node-2"}, {}),
        ("trace antigo (30min)", "svc-old", {}, {}),
    ]
    for name, svc, tags, extra in feats:
        tg = {**R, **tags}
        lp = {"service": svc, "tags": json.dumps(tg), "start": s_us, "end": e_us, "limit": 100, **{k: v for k, v in extra.items() if v}}
        if extra.get("operation"): lp["operation"] = extra["operation"]
        _, d1 = jget(OURS, "/api/traces", lp); _, d2 = jget(REF_LEGACY, "/api/traces", lp)
        i1 = sorted(t["traceID"] for t in d1.get("data") or []); i2 = sorted(t["traceID"] for t in d2.get("data") or [])
        vp = {"query.serviceName": svc, "query.startTimeMin": iso(start), "query.startTimeMax": iso(end), "query.searchDepth": 100, "query.attributes": json.dumps(tg)}
        if extra.get("operation"): vp["query.operationName"] = extra["operation"]
        if extra.get("minDuration"): vp["query.durationMin"] = extra["minDuration"]
        if extra.get("maxDuration"): vp["query.durationMax"] = extra["maxDuration"]
        _, s1 = jget(OURS, "/api/v3/trace-summaries", vp); _, s2 = jget(REF_V3, "/api/v3/trace-summaries", vp)
        j1 = sorted(x["traceId"] for x in s1.get("summaries") or []); j2 = sorted(x["traceId"] for x in s2.get("summaries") or [])
        ok = i1 == i2 and j1 == j2
        record("3-recurso", name, ok, f"legacy={len(i1)} v3={len(j1)} traces" if ok else f"legacy ClickHouse={len(i1)} Jaeger={len(i2)} | v3 ClickHouse={len(j1)} Jaeger={len(j2)}")

    print("\n== 4. Serviços, operações e dependências dos casos")
    _, a1 = jget(OURS, "/api/services"); _, a2 = jget(REF_LEGACY, "/api/services")
    mine = {c["service"] for c in cases.values() if c.get("service")} | {"svc-deep-b", "svc-wide-worker", "svc-shipping", "svc-billing", "svc-skew-child", "svc-dup-server"} | {f"svc-large-{i}" for i in range(5)}
    record("4-meta", "serviços novos listados", mine <= set(a1["data"]) and mine <= set(a2["data"]), f"faltando ClickHouse={sorted(mine - set(a1['data']))} Jaeger={sorted(mine - set(a2['data']))}")
    extra1 = sorted(set(a1["data"]) - set(a2["data"]) - {"demo-app"}); extra2 = sorted(set(a2["data"]) - set(a1["data"]) - {"jaeger", "demo-app"})
    record("4-meta", "serviço do span SEM service.name", extra1 == extra2, f"ClickHouse lista {extra1}, Jaeger lista {extra2}")
    for svc in ["svc-kinds", "serviço-ação 日本", "svc-emptyname", "svc-large-1", "svc-wide-worker"]:
        _, o1 = jget(OURS, "/api/operations", {"service": svc}); _, o2 = jget(REF_LEGACY, "/api/operations", {"service": svc})
        k = lambda d: sorted((x["name"], x["spanKind"]) for x in (d.get("data") or []))
        record("4-meta", f"operações {svc}", k(o1) == k(o2), f"{len(k(o1))} ops" if k(o1) == k(o2) else f"ClickHouse={k(o1)[:6]} Jaeger={k(o2)[:6]}")
        _, o1 = jget(OURS, "/api/v3/operations", {"service": svc}); _, o2 = jget(REF_V3, "/api/v3/operations", {"service": svc})
        k3 = lambda d: sorted((x["name"], x["spanKind"]) for x in d.get("operations") or [])
        record("4-meta", f"operações v3 {svc}", k3(o1) == k3(o2), "" if k3(o1) == k3(o2) else f"ClickHouse={k3(o1)[:6]} Jaeger={k3(o2)[:6]}")
    endTs, lb = int(end * 1000), int((end - start) * 1000)
    _, d1 = jget(OURS, "/api/dependencies", {"endTs": endTs, "lookback": lb}); _, d2 = jget(REF_LEGACY, "/api/dependencies", {"endTs": endTs, "lookback": lb})
    D = lambda d: {(x["parent"], x["child"]): x["callCount"] for x in d["data"] if x["parent"] in mine or x["child"] in mine}
    dd1, dd2 = D(d1), D(d2)
    record("4-meta", f"dependências dos casos ({len(dd2)} arestas no Jaeger)", dd1 == dd2,
           "" if dd1 == dd2 else f"só ClickHouse={ {k: v for k, v in dd1.items() if dd2.get(k) != v} } só Jaeger={ {k: v for k, v in dd2.items() if dd1.get(k) != v} }")

    print()
    for sec in ("1-trace", "2-busca", "3-recurso", "4-meta"):
        rs = [r for r in results if r["section"] == sec]
        print(f"{sec:10} {sum(r['ok'] for r in rs)}/{len(rs)} OK")
    tot = sum(r["ok"] for r in results)
    print(f"TOTAL      {tot}/{len(results)} OK")
    if type_diffs:
        print(f"\nTipos não preservados (Jaeger tipado x ClickHouse string), por caso: {type_diffs}")
    if a.json:
        json.dump(dict(run=run, results=results, type_diffs=type_diffs), open(a.json, "w"), indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
