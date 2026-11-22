#!/usr/bin/env python3
"""Gera traces determinísticos multi-serviço via OTLP/HTTP JSON no collector.

Topologia por trace:  frontend (server) -> checkout (client/server) -> payments (server) -> postgres (client)
Cada trace recebe um "run_id" para isolar a bateria de testes. Uso:
  python3 test/compat/gen_traces.py --run RUN_ID [--count 20] [--endpoint http://localhost:4318]
Imprime JSON com os trace IDs e o "gabarito" de cada um.
"""
import argparse, json, os, random, time, urllib.request

KIND = {"internal": 1, "server": 2, "client": 3, "producer": 4, "consumer": 5}

def hexid(n): return os.urandom(n).hex()

def attr(k, v):
    if isinstance(v, bool): return {"key": k, "value": {"boolValue": v}}
    if isinstance(v, int): return {"key": k, "value": {"intValue": str(v)}}
    if isinstance(v, float): return {"key": k, "value": {"doubleValue": v}}
    return {"key": k, "value": {"stringValue": str(v)}}

def build(run, i, now_ns):
    tid = hexid(16)
    rnd = random.Random(f"{run}-{i}")
    error = (i % 5 == 0)                      # 20% dos traces com erro
    slow = (i % 4 == 0)                        # 25% lentos (>= 1.5s)
    region = ["us-east-1", "sa-east-1"][i % 2]
    user_tier = ["gold", "silver", "bronze"][i % 3]
    base = now_ns - (i * 7 + 30) * 1_000_000_000          # espalha no tempo (mais recente primeiro)
    total = (1_500 + rnd.randint(0, 400)) * 1_000_000 if slow else (80 + rnd.randint(0, 120)) * 1_000_000
    s_front, s_chk_c, s_chk, s_pay, s_db = (hexid(8) for _ in range(5))
    status_err = {"code": 2, "message": "card declined"}
    spans = {
      "frontend": [dict(traceId=tid, spanId=s_front, name="GET /checkout", kind=KIND["server"],
                        startTimeUnixNano=str(base), endTimeUnixNano=str(base + total),
                        attributes=[attr("http.request.method", "GET"), attr("http.route", "/checkout"),
                                    attr("http.response.status_code", 500 if error else 200),
                                    attr("test.run_id", run), attr("user.tier", user_tier), attr("test.index", i)],
                        status=({"code": 2, "message": "upstream error"} if error else {"code": 1}))],
      "checkout": [dict(traceId=tid, spanId=s_chk_c, parentSpanId=s_front, name="POST /pay", kind=KIND["client"],
                        startTimeUnixNano=str(base + 5_000_000), endTimeUnixNano=str(base + total - 5_000_000),
                        attributes=[attr("http.request.method", "POST"), attr("server.address", "payments"), attr("test.run_id", run)],
                        status=(status_err if error else {})),
                   dict(traceId=tid, spanId=s_chk, parentSpanId=s_chk_c, name="checkout.process", kind=KIND["internal"],
                        startTimeUnixNano=str(base + 6_000_000), endTimeUnixNano=str(base + 20_000_000),
                        attributes=[attr("cart.items", 3), attr("cart.total", 99.9), attr("test.run_id", run)],
                        events=[{"timeUnixNano": str(base + 10_000_000), "name": "cart.validated",
                                 "attributes": [attr("items", 3)]}],
                        status={})],
      "payments": [dict(traceId=tid, spanId=s_pay, parentSpanId=s_chk_c, name="POST /pay", kind=KIND["server"],
                        startTimeUnixNano=str(base + 10_000_000), endTimeUnixNano=str(base + total - 10_000_000),
                        attributes=[attr("http.request.method", "POST"), attr("payment.provider", "acme"),
                                    attr("test.run_id", run), attr("http.response.status_code", 402 if error else 200)],
                        events=([{"timeUnixNano": str(base + 15_000_000), "name": "exception",
                                  "attributes": [attr("exception.type", "CardDeclined"), attr("exception.message", "card declined")]}]
                                if error else []),
                        links=[{"traceId": hexid(16), "spanId": hexid(8), "attributes": [attr("link.kind", "batch")]}],
                        status=(status_err if error else {"code": 1}))],
      "postgres": [dict(traceId=tid, spanId=s_db, parentSpanId=s_pay, name="SELECT payments", kind=KIND["client"],
                        startTimeUnixNano=str(base + 20_000_000), endTimeUnixNano=str(base + 60_000_000),
                        attributes=[attr("db.system.name", "postgresql"), attr("db.query.text", "SELECT * FROM payments WHERE id = $1"),
                                    attr("test.run_id", run)],
                        status={})],
    }
    resource_spans = []
    for svc, sp in spans.items():
        resource_spans.append({
            "resource": {"attributes": [attr("service.name", svc), attr("deployment.environment.name", "test"),
                                        attr("cloud.region", region), attr("service.version", "1.2.3")]},
            "scopeSpans": [{"scope": {"name": "gen_traces", "version": "1.0"}, "spans": sp}]})
    truth = dict(traceId=tid, error=error, slow=slow, region=region, tier=user_tier, index=i,
                 duration_ms=total / 1e6, start_ns=base, span_count=5, services=sorted(spans))
    return {"resourceSpans": resource_spans}, truth

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--endpoint", default="http://localhost:4318")
    a = ap.parse_args()
    now = time.time_ns()
    truths = []
    for i in range(a.count):
        payload, truth = build(a.run, i, now)
        req = urllib.request.Request(f"{a.endpoint}/v1/traces", json.dumps(payload).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        truths.append(truth)
    print(json.dumps({"run": a.run, "generated_at_ns": now, "traces": truths}))

if __name__ == "__main__":
    main()
