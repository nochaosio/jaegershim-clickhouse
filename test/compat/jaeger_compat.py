#!/usr/bin/env python3
"""De-para: Jaeger original x jaegershim (ClickHouse).

Gera traces determinísticos (test/compat/gen_traces.py) que o OTel Collector envia ao
mesmo tempo para o ClickHouse, para um Jaeger 2.23 (API v3 / Jaeger UI) e para um
Jaeger 2.20 (API legacy / Grafana). Depois executa os MESMOS cenários nos dois
lados e compara os resultados:

  A. via Grafana (datasource Jaeger nativo): "Jaeger (original 2.20)" x "jaegershim"
  B. API legacy direta (/api/*):            Jaeger 2.20               x adaptador
  C. API v3 direta (/api/v3/*, Jaeger UI):  Jaeger 2.23               x adaptador
  D. Jaeger UI servido pelo adaptador (assets + chamadas no formato do UI)

Uso: python3 test/compat/jaeger_compat.py [--count 30] [--json out.json]
"""
import argparse, base64, json, os, subprocess, sys, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
from datetime import datetime, timezone

OURS = "http://localhost:16686"
REF_LEGACY = "http://localhost:16688"   # Jaeger 2.20
REF_V3 = "http://localhost:16687"       # Jaeger 2.23
GRAFANA = "http://localhost:3000"
G_AUTH = "Basic " + base64.b64encode(b"admin:admin").decode()
DS_OURS, DS_REF = "jaegershim", "jaeger-original"
TEST_SERVICES = {"frontend", "checkout", "payments", "postgres"}

results = []

def http(method, url, body=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw

def jget(base, path, params=None):
    q = ("?" + urllib.parse.urlencode(params, doseq=True)) if params else ""
    return http("GET", base + path + q)

def record(section, name, ok, detail=""):
    results.append(dict(section=section, name=name, ok=bool(ok), detail=detail))
    mark = "OK  " if ok else "DIFF"
    print(f"  [{mark}] {name}" + (f"  -> {detail}" if detail and not ok else (f"  ({detail})" if detail else "")))

def sv(v):
    if isinstance(v, bool):
        return str(v).lower()
    return str(v)

# ---------------- cenários de busca ----------------

def scenarios(run):
    R = {"test.run_id": run}
    return [
        ("service=frontend", dict(service="frontend", tags=R)),
        ("service=payments + error=true", dict(service="payments", tags={**R, "error": "true"})),
        ("minDuration=1s", dict(service="frontend", tags=R, minDuration="1s")),
        ("maxDuration=500ms", dict(service="frontend", tags=R, maxDuration="500ms")),
        ("min+maxDuration (100ms..1.8s)", dict(service="frontend", tags=R, minDuration="100ms", maxDuration="1.8s")),
        ("operation='POST /pay' (checkout)", dict(service="checkout", operation="POST /pay", tags=R)),
        ("span.kind=client", dict(service="checkout", tags={**R, "span.kind": "client"})),
        ("tag de resource cloud.region", dict(service="payments", tags={**R, "cloud.region": "sa-east-1"})),
        ("atributo int http.response.status_code=402", dict(service="payments", tags={**R, "http.response.status_code": "402"})),
        ("campo de evento exception.type", dict(service="payments", tags={**R, "exception.type": "CardDeclined"})),
        ("multi-tag mesmo span (user.tier+error)", dict(service="frontend", tags={**R, "user.tier": "gold", "error": "true"})),
        ("otel.status_code=ERROR (Jaeger não indexa: 0)", dict(service="checkout", tags={**R, "otel.status_code": "ERROR"})),
        ("error=false", dict(service="payments", tags={**R, "error": "false"})),
        ("tags em spans diferentes (não casa)", dict(service="frontend", tags={**R, "db.system.name": "postgresql"})),
        ("sem resultado", dict(service="frontend", tags={**R, "user.tier": "platinum"})),
    ]

def legacy_params(sc, start_us, end_us, limit=1000):
    p = {"service": sc["service"], "start": start_us, "end": end_us, "limit": sc.get("limit", limit)}
    if sc.get("operation"): p["operation"] = sc["operation"]
    if sc.get("tags"): p["tags"] = json.dumps(sc["tags"])
    for k in ("minDuration", "maxDuration"):
        if sc.get(k): p[k] = sc[k]
    return p

def v3_params(sc, start, end, limit=1000):
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    p = {"query.serviceName": sc["service"], "query.startTimeMin": iso(start), "query.startTimeMax": iso(end),
         "query.searchDepth": sc.get("limit", limit)}
    if sc.get("operation"): p["query.operationName"] = sc["operation"]
    if sc.get("tags"): p["query.attributes"] = json.dumps(sc["tags"])
    if sc.get("minDuration"): p["query.durationMin"] = sc["minDuration"]
    if sc.get("maxDuration"): p["query.durationMax"] = sc["maxDuration"]
    return p

def logfmt(tags):
    return " ".join(f'{k}="{v}"' if " " in v else f"{k}={v}" for k, v in tags.items())

def expected_ids(truth, sc):
    """Gabarito independente (calculado do gerador), para validar os DOIS lados."""
    out = set()
    for t in truth:
        tags = sc.get("tags", {})
        svc = sc["service"]
        # spans do gerador: (serviço, operação, kind, dur_ms, atributos/resource/evento, erro?)
        d = t["duration_ms"]
        err = t["error"]
        spans = [
            ("frontend", "GET /checkout", "server", d, {"user.tier": t["tier"], "http.response.status_code": "500" if err else "200", }, err),
            ("checkout", "POST /pay", "client", d - 10, {}, err),
            ("checkout", "checkout.process", "internal", 14, {"items": "3"}, False),
            ("payments", "POST /pay", "server", d - 20, {"http.response.status_code": "402" if err else "200", **({"exception.type": "CardDeclined"} if err else {})}, err),
            ("postgres", "SELECT payments", "client", 40, {"db.system.name": "postgresql"}, False),
        ]
        for s_svc, op, kind, dur, attrs, is_err in spans:
            if s_svc != svc: continue
            if sc.get("operation") and op != sc["operation"]: continue
            if sc.get("minDuration") and dur < parse_ms(sc["minDuration"]): continue
            if sc.get("maxDuration") and dur > parse_ms(sc["maxDuration"]): continue
            ok = True
            allattrs = {**attrs, "cloud.region": t["region"], "span.kind": kind, "test.run_id": tags.get("test.run_id")}
            for k, v in tags.items():
                if k == "error":
                    ok &= (is_err == (v == "true"))
                elif allattrs.get(k) != v:
                    ok = False
            if ok:
                out.add(t["traceId"]); break
    return out

def parse_ms(s):
    if s.endswith("ms"): return float(s[:-2])
    if s.endswith("s"): return float(s[:-1]) * 1000
    raise ValueError(s)

# ---------------- normalização ----------------

def norm_legacy_trace(t):
    procs = {pid: (p["serviceName"], tuple(sorted((x["key"], sv(x["value"])) for x in p["tags"]))) for pid, p in t["processes"].items()}
    spans = {}
    for s in t["spans"]:
        spans[s["spanID"]] = dict(
            op=s["operationName"], start=s["startTime"], dur=s["duration"],
            refs=tuple(sorted((r["refType"], r["traceID"], r["spanID"]) for r in s["references"])),
            tags=tuple(sorted((x["key"], sv(x["value"])) for x in s["tags"])),
            logs=tuple(sorted((l["timestamp"], tuple(sorted((f["key"], sv(f["value"])) for f in l["fields"]))) for l in s["logs"])),
            process=procs[s["processID"]])
    return spans

def norm_otlp(result):
    spans = {}
    for rs in result["resourceSpans"]:
        res = tuple(sorted((a["key"], sv(list(a["value"].values())[0] if a["value"] else "")) for a in rs["resource"]["attributes"]))
        for ss in rs["scopeSpans"]:
            scope = (ss.get("scope", {}).get("name", ""), ss.get("scope", {}).get("version", ""))
            for s in ss["spans"]:
                spans[s["spanId"]] = dict(
                    name=s["name"], kind=s.get("kind", 0), parent=s.get("parentSpanId", ""),
                    start=s["startTimeUnixNano"], end=s["endTimeUnixNano"],
                    attrs=tuple(sorted((a["key"], sv(list(a["value"].values())[0])) for a in s.get("attributes", []))),
                    events=tuple(sorted((e["timeUnixNano"], e["name"], tuple(sorted((a["key"], sv(list(a["value"].values())[0])) for a in e.get("attributes", [])))) for e in s.get("events", []))),
                    links=tuple(sorted((l["traceId"], l["spanId"]) for l in s.get("links", []))),
                    status=(s.get("status", {}).get("code", 0), s.get("status", {}).get("message", "")),
                    resource=res, scope=scope)
    return spans

def diff_dicts(a, b, limit=2):
    out = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            if k not in a or k not in b:
                out.append(f"span {k} só em {'ours' if k in a else 'ref'}")
            else:
                for f in a[k]:
                    if a[k][f] != b[k][f]:
                        out.append(f"span {k}.{f}: ours={str(a[k][f])[:160]} ref={str(b[k][f])[:160]}")
        if len(out) >= limit: break
    return "; ".join(out)

def frame_rows(frame):
    names = [f["name"] for f in frame["schema"]["fields"]]
    vals = frame["data"]["values"]
    n = len(vals[0]) if vals else 0
    return [{names[i]: vals[i][r] for i in range(len(names))} for r in range(n)]

def norm_grafana_trace(frames):
    rows = []
    for f in frames:
        rows += frame_rows(f)
    out = {}
    for r in rows:
        nr = {}
        for k, v in r.items():
            if k in ("tags", "serviceTags") and v:
                v = tuple(sorted((x["key"], sv(x.get("value"))) for x in v))
            elif k == "logs" and v:
                v = tuple(sorted((l["timestamp"], tuple(sorted((x["key"], sv(x.get("value"))) for x in l.get("fields", [])))) for l in v))
            elif k == "references" and v:
                v = tuple(sorted((x.get("traceID"), x.get("spanID"), json.dumps(x.get("tags"), sort_keys=True)) for x in v))
            elif isinstance(v, list):
                v = json.dumps(v, sort_keys=True)
            nr[k] = v
        out[r.get("spanID")] = nr
    return out

def ensure_reference_datasource():
    """Cria no Grafana o datasource Jaeger apontando para o Jaeger 2.20 de referência, se não existir."""
    c, _ = http("GET", f"{GRAFANA}/api/datasources/uid/{DS_REF}", headers={"Authorization": G_AUTH})
    if c == 404:
        http("POST", f"{GRAFANA}/api/datasources", {"name": "Jaeger (original 2.20)", "type": "jaeger", "uid": DS_REF,
             "access": "proxy", "url": "http://jaeger-reference-legacy:16686", "jsonData": {"nodeGraph": {"enabled": True}}},
             {"Authorization": G_AUTH})

def gquery(ds, q, frm, to):
    body = {"from": str(int(frm * 1000)), "to": str(int(to * 1000)), "queries": [{"refId": "A", "datasource": {"uid": ds}, **q}]}
    code, res = http("POST", GRAFANA + "/api/ds/query", body, {"Authorization": G_AUTH})
    r = res.get("results", {}).get("A", {}) if isinstance(res, dict) else {}
    return r.get("error"), r.get("frames", [])

# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--json")
    a = ap.parse_args()
    ensure_reference_datasource()
    run = f"cmp{int(time.time())}"
    print(f"== gerando {a.count} traces determinísticos (run={run}) ...")
    out = subprocess.run([sys.executable, "-I", os.path.join(HERE, "gen_traces.py"), "--run", run, "--count", str(a.count)],
                         capture_output=True, text=True, check=True).stdout
    truth = json.loads(out)["traces"]
    t_gen = time.time()
    oldest = min(t["start_ns"] for t in truth) / 1e9
    start, end = oldest - 60, t_gen + 120
    start_us, end_us = int(start * 1e6), int(end * 1e6)
    # espera os 3 backends terem tudo
    for _ in range(30):
        time.sleep(2)
        c1 = len((jget(OURS, "/api/traces", legacy_params({"service": "postgres", "tags": {"test.run_id": run}}, start_us, end_us))[1] or {}).get("data") or [])
        c2 = len((jget(REF_LEGACY, "/api/traces", legacy_params({"service": "postgres", "tags": {"test.run_id": run}}, start_us, end_us))[1] or {}).get("data") or [])
        c3 = len((jget(REF_V3, "/api/v3/trace-summaries", v3_params({"service": "postgres", "tags": {"test.run_id": run}}, start, end))[1] or {}).get("summaries") or [])
        if c1 == c2 == c3 == a.count:
            break
    print(f"   ingestão: clickhouse={c1} jaeger2.20={c2} jaeger2.23={c3} (esperado {a.count})\n")
    SCN = scenarios(run)

    # ======== A. Grafana ========
    print("== A. Grafana: datasource Jaeger -> Jaeger original 2.20  x  jaegershim")
    for ds in (DS_REF, DS_OURS):
        c, h = http("GET", f"{GRAFANA}/api/datasources/uid/{ds}/health", headers={"Authorization": G_AUTH})
        record("A-grafana", f"health {ds}", c == 200 and h.get("status") == "OK", h.get("message", ""))
    _, s_ours = http("GET", f"{GRAFANA}/api/datasources/uid/{DS_OURS}/resources/services", headers={"Authorization": G_AUTH})
    _, s_ref = http("GET", f"{GRAFANA}/api/datasources/uid/{DS_REF}/resources/services", headers={"Authorization": G_AUTH})
    record("A-grafana", "dropdown de serviços (serviços de teste)", TEST_SERVICES <= set(s_ours) and TEST_SERVICES <= set(s_ref),
           f"ours={sorted(set(s_ours) & TEST_SERVICES)} ref={sorted(set(s_ref) & TEST_SERVICES)}")
    for svc in sorted(TEST_SERVICES):
        _, o1 = http("GET", f"{GRAFANA}/api/datasources/uid/{DS_OURS}/resources/services/{urllib.parse.quote(svc)}/operations", headers={"Authorization": G_AUTH})
        _, o2 = http("GET", f"{GRAFANA}/api/datasources/uid/{DS_REF}/resources/services/{urllib.parse.quote(svc)}/operations", headers={"Authorization": G_AUTH})
        record("A-grafana", f"dropdown de operações: {svc}", sorted(o1 or []) == sorted(o2 or []), f"ours={sorted(o1 or [])} ref={sorted(o2 or [])}")
    for name, sc in SCN:
        q = {"queryType": "search", "service": sc["service"], "limit": 1000}
        if sc.get("operation"): q["operation"] = sc["operation"]
        if sc.get("tags"): q["tags"] = logfmt(sc["tags"])
        for k in ("minDuration", "maxDuration"):
            if sc.get(k): q[k] = sc[k]
        e1, f1 = gquery(DS_OURS, q, start, end)
        e2, f2 = gquery(DS_REF, q, start, end)
        r1 = {r["traceID"]: r for f in f1 for r in frame_rows(f)}
        r2 = {r["traceID"]: r for f in f2 for r in frame_rows(f)}
        exp = expected_ids(truth, sc)
        same = set(r1) == set(r2) and all(r1[k] == r2[k] for k in r1)
        det = f"{len(r1)} traces" if same else f"ours={len(r1)} ref={len(r2)} só_ours={len(set(r1)-set(r2))} só_ref={len(set(r2)-set(r1))} campos_diff={[ (k, {f: (r1[k][f], r2[k][f]) for f in r1[k] if r1[k][f] != r2[k][f]}) for k in set(r1)&set(r2) if r1[k]!=r2[k]][:1]}"
        record("A-grafana", f"busca: {name}", not e1 and not e2 and same and set(r1) == exp, (det if same else det) + ("" if set(r1) == exp else f" | gabarito={len(exp)}") + (f" err={e1 or e2}" if e1 or e2 else ""))
    # limite
    q = {"queryType": "search", "service": "frontend", "tags": logfmt({"test.run_id": run}), "limit": 5}
    _, f1 = gquery(DS_OURS, q, start, end); _, f2 = gquery(DS_REF, q, start, end)
    n1 = sum(len(frame_rows(f)) for f in f1); n2 = sum(len(frame_rows(f)) for f in f2)
    record("A-grafana", "busca: limit=5", n1 == n2 == 5, f"ours={n1} ref={n2}")
    # trace por ID (visão cascata) para TODOS os traces gerados
    bad = []
    for t in truth:
        e1, f1 = gquery(DS_OURS, {"query": t["traceId"]}, start, end)
        e2, f2 = gquery(DS_REF, {"query": t["traceId"]}, start, end)
        n1, n2 = norm_grafana_trace(f1), norm_grafana_trace(f2)
        if e1 or e2 or n1 != n2 or len(n1) != 5:
            bad.append((t["traceId"], e1 or e2 or diff_dicts(n1, n2)))
    record("A-grafana", f"trace por ID (cascata) — {len(truth)} traces", not bad, f"{len(truth)-len(bad)}/{len(truth)} idênticos" + (f"; ex: {bad[0]}" if bad else ""))
    # grafo de dependências
    e1, f1 = gquery(DS_OURS, {"queryType": "dependencyGraph"}, start, end)
    e2, f2 = gquery(DS_REF, {"queryType": "dependencyGraph"}, start, end)
    def edges(frames):
        ed = {}
        for f in frames:
            rows = frame_rows(f)
            if rows and "source" in rows[0]:
                for r in rows:
                    ed[(r["source"], r["target"])] = r.get("mainstat") or r.get("mainStat")
        return ed
    def tset(ed): return {k: v for k, v in ed.items() if k[0] in TEST_SERVICES and k[1] in TEST_SERVICES}
    E1, E2 = tset(edges(f1)), tset(edges(f2))
    record("A-grafana", "grafo de dependências (arestas entre serviços de teste)", not e1 and not e2 and set(E1) == set(E2) and len(E1) >= 3,
           f"ours={sorted(E1)} ref={sorted(E2)}" + (f" err={e1 or e2}" if e1 or e2 else ""))

    # ======== B. API legacy ========
    print("\n== B. API legacy direta: Jaeger 2.20  x  adaptador")
    _, a1 = jget(OURS, "/api/services"); _, a2 = jget(REF_LEGACY, "/api/services")
    record("B-legacy", "/api/services", TEST_SERVICES <= set(a1["data"]) and TEST_SERVICES <= set(a2["data"]))
    for svc in sorted(TEST_SERVICES):
        _, o1 = jget(OURS, "/api/operations", {"service": svc}); _, o2 = jget(REF_LEGACY, "/api/operations", {"service": svc})
        k = lambda d: sorted((x["name"], x["spanKind"]) for x in (d["data"] or []))
        record("B-legacy", f"/api/operations?service={svc}", k(o1) == k(o2), f"ours={k(o1)} ref={k(o2)}")
    for kind in ("client", "internal", "server"):
        _, o1 = jget(OURS, "/api/operations", {"service": "checkout", "spanKind": kind}); _, o2 = jget(REF_LEGACY, "/api/operations", {"service": "checkout", "spanKind": kind})
        k = lambda d: sorted((x["name"], x["spanKind"]) for x in (d["data"] or []))
        record("B-legacy", f"/api/operations spanKind={kind}", k(o1) == k(o2), f"ours={k(o1)} ref={k(o2)}")
    for name, sc in SCN:
        p = legacy_params(sc, start_us, end_us)
        _, d1 = jget(OURS, "/api/traces", p); _, d2 = jget(REF_LEGACY, "/api/traces", p)
        i1 = {t["traceID"]: norm_legacy_trace(t) for t in d1.get("data") or []}
        i2 = {t["traceID"]: norm_legacy_trace(t) for t in d2.get("data") or []}
        exp = expected_ids(truth, sc)
        same = set(i1) == set(i2) and all(i1[k] == i2[k] for k in i1)
        record("B-legacy", f"/api/traces {name}", same and set(i1) == exp,
               f"{len(i1)} traces completos idênticos" if same else f"ours={len(i1)} ref={len(i2)} {next((diff_dicts(i1[k], i2[k]) for k in i1 if k in i2 and i1[k]!=i2[k]), '')}")
    # trace por id + multi traceID + erros
    t0 = truth[0]["traceId"]
    _, d1 = jget(OURS, f"/api/traces/{t0}"); _, d2 = jget(REF_LEGACY, f"/api/traces/{t0}")
    n1, n2 = norm_legacy_trace(d1["data"][0]), norm_legacy_trace(d2["data"][0])
    record("B-legacy", "/api/traces/{id} (modelo completo: tags, logs, refs, processes)", n1 == n2, diff_dicts(n1, n2))
    p = {"traceID": [truth[1]["traceId"], truth[2]["traceId"]]}
    _, d1 = jget(OURS, "/api/traces", p); _, d2 = jget(REF_LEGACY, "/api/traces", p)
    record("B-legacy", "/api/traces?traceID=a&traceID=b", len(d1["data"]) == len(d2["data"]) == 2)
    for path, params in [("/api/traces/zzz", None), ("/api/traces/00000000000000000000000000000001", None),
                         ("/api/traces", None), ("/api/traces", {"service": "x", "minDuration": "abc"})]:
        c1, e1 = jget(OURS, path, params); c2, e2 = jget(REF_LEGACY, path, params)
        m1 = (e1.get("errors") or [{}])[0].get("msg") if isinstance(e1, dict) else e1
        m2 = (e2.get("errors") or [{}])[0].get("msg") if isinstance(e2, dict) else e2
        record("B-legacy", f"erro {path} {params or ''}", c1 == c2 and m1 == m2, f"ours={c1} {m1!r} ref={c2} {m2!r}")
    endTs = int(end * 1000); lookback = int((end - start) * 1000)
    _, d1 = jget(OURS, "/api/dependencies", {"endTs": endTs, "lookback": lookback})
    _, d2 = jget(REF_LEGACY, "/api/dependencies", {"endTs": endTs, "lookback": lookback})
    D = lambda d: {(x["parent"], x["child"]): x["callCount"] for x in d["data"] if x["parent"] in TEST_SERVICES and x["child"] in TEST_SERVICES}
    record("B-legacy", "/api/dependencies (arestas + contagem, serviços de teste)", D(d1) == D(d2), f"ours={D(d1)} ref={D(d2)}")

    # ======== C. API v3 ========
    print("\n== C. API v3 direta (usada pelo Jaeger UI 2.x): Jaeger 2.23  x  adaptador")
    _, a1 = jget(OURS, "/api/v3/services"); _, a2 = jget(REF_V3, "/api/v3/services")
    record("C-v3", "/api/v3/services", TEST_SERVICES <= set(a1["services"]) and TEST_SERVICES <= set(a2["services"]))
    for svc in sorted(TEST_SERVICES):
        _, o1 = jget(OURS, "/api/v3/operations", {"service": svc}); _, o2 = jget(REF_V3, "/api/v3/operations", {"service": svc})
        k = lambda d: sorted((x["name"], x["spanKind"]) for x in d["operations"])
        record("C-v3", f"/api/v3/operations?service={svc}", k(o1) == k(o2), f"ours={k(o1)} ref={k(o2)}")
    for name, sc in SCN:
        p = v3_params(sc, start, end)
        _, d1 = jget(OURS, "/api/v3/trace-summaries", p); _, d2 = jget(REF_V3, "/api/v3/trace-summaries", p)
        s1 = {s["traceId"]: s for s in d1.get("summaries") or []}
        s2 = {s["traceId"]: s for s in d2.get("summaries") or []}
        exp = expected_ids(truth, sc)
        same = set(s1) == set(s2) and all(s1[k] == s2[k] for k in s1)
        record("C-v3", f"trace-summaries {name}", same and set(s1) == exp,
               f"{len(s1)} resumos idênticos" if same else f"ours={len(s1)} ref={len(s2)} ex={next(({f: (s1[k].get(f), s2[k].get(f)) for f in set(s1[k])|set(s2[k]) if s1[k].get(f) != s2[k].get(f)} for k in s1 if k in s2 and s1[k]!=s2[k]), '')}")
    bad = []
    for t in truth:
        _, d1 = jget(OURS, f"/api/v3/traces/{t['traceId']}"); _, d2 = jget(REF_V3, f"/api/v3/traces/{t['traceId']}")
        n1, n2 = norm_otlp(d1["result"]), norm_otlp(d2["result"])
        if n1 != n2:
            bad.append((t["traceId"], diff_dicts(n1, n2)))
    record("C-v3", f"/api/v3/traces/{{id}} OTLP — {len(truth)} traces", not bad, f"{len(truth)-len(bad)}/{len(truth)} idênticos" + (f"; ex: {bad[0]}" if bad else ""))
    p = v3_params({"service": "payments", "tags": {"test.run_id": run, "error": "true"}}, start, end)
    _, d1 = jget(OURS, "/api/v3/traces", p); _, d2 = jget(REF_V3, "/api/v3/traces", p)
    n1, n2 = norm_otlp(d1["result"]), norm_otlp(d2["result"])
    record("C-v3", "/api/v3/traces (busca, OTLP)", n1 == n2 and len(n1) > 0, f"{len(n1)} spans" if n1 == n2 else diff_dicts(n1, n2))
    for path, params in [("/api/v3/traces/00000000000000000000000000000001", None), ("/api/v3/trace-summaries", {"query.serviceName": "x"})]:
        c1, e1 = jget(OURS, path, params); c2, e2 = jget(REF_V3, path, params)
        record("C-v3", f"erro {path}", c1 == c2 and e1 == e2, f"ours={c1} {e1} ref={c2} {e2}")

    # ======== D. Jaeger UI ========
    print("\n== D. Jaeger UI servido pelo adaptador")
    c, html = jget(OURS, "/search")
    html = html.decode() if isinstance(html, bytes) else str(html)
    record("D-ui", "GET /search devolve o Jaeger UI (SPA)", c == 200 and "jaeger-ui-root" in html)
    import re
    assets = re.findall(r'(?:src|href)="\.?/?(static/[^"]+)"', html)
    st = [http("GET", f"{OURS}/{x}")[0] for x in assets]
    record("D-ui", f"assets do UI ({len(assets)} arquivos)", assets and all(x == 200 for x in st), f"{st}")
    c, _ = jget(OURS, f"/trace/{truth[0]['traceId']}")
    record("D-ui", "deep link /trace/{id}", c == 200)
    # chamadas exatamente como o UI 2.23 monta (fetchTraceSummaries usa ISO e query.attributes)
    ui = {"query.serviceName": "payments", "query.startTimeMin": datetime.fromtimestamp(start, timezone.utc).isoformat().replace("+00:00", "Z"),
          "query.startTimeMax": datetime.fromtimestamp(end, timezone.utc).isoformat().replace("+00:00", "Z"), "query.searchDepth": "20",
          "query.attributes": json.dumps({"test.run_id": run})}
    c, d = jget(OURS, "/api/v3/trace-summaries", ui)
    record("D-ui", "busca no formato do UI (trace-summaries)", c == 200 and len(d["summaries"]) == min(20, a.count), f"{len(d.get('summaries', []))} resumos")

    # ======== resumo ========
    print()
    for sec in ("A-grafana", "B-legacy", "C-v3", "D-ui"):
        rs = [r for r in results if r["section"] == sec]
        print(f"{sec:10} {sum(r['ok'] for r in rs)}/{len(rs)} OK")
    tot = sum(r["ok"] for r in results)
    print(f"TOTAL      {tot}/{len(results)} OK")
    if a.json:
        json.dump(dict(run=run, results=results), open(a.json, "w"), indent=2, ensure_ascii=False)
    sys.exit(0 if tot == len(results) else 1)

if __name__ == "__main__":
    main()
