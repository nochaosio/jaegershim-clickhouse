#!/usr/bin/env python3
"""Latência de queries PromQL: ClickHouse vs Prometheus (mediana de N execuções)."""
import base64, json, statistics, sys, time, urllib.parse, urllib.request
CH="http://localhost:8123/prometheus/api/v1"; PROM="http://localhost:9090/api/v1"
AUTH="Basic "+base64.b64encode(b"default:clickhouse").decode()
N=int(sys.argv[1]) if len(sys.argv)>1 else 5
RANGE=int(sys.argv[2]) if len(sys.argv)>2 else 600
Q={
 "1 série instant": 'avalanche_gauge_metric_mmmmm_0_0{series_id="1"}',
 "sum(rate) 1 métrica": 'sum(rate(avalanche_counter_metric_mmmmm_0_0_total[1m]))',
 "sum by regex 50 gauges": 'sum by (series_id) ({__name__=~"avalanche_gauge.+"})',
 "rate 1000 séries": 'rate({__name__=~"avalanche_counter_metric_mmmmm_[0-4].+"}[1m]) > -1',
 "count todas séries": 'count({__name__=~".+"})',
 "topk regex gauges": 'topk(5, {__name__=~"avalanche_gauge.+"})',
 "histogram_quantile 5 hists": 'histogram_quantile(0.99, sum by (le, series_id) (rate(avalanche_histogram_metric_mmmmm_0_0_bucket[2m])))',
 "subquery": 'max_over_time(sum(rate(avalanche_counter_metric_mmmmm_0_0_total[1m]))[5m:30s])',
}
def call(base,path,p,auth=None):
    r=urllib.request.Request(f"{base}/{path}",urllib.parse.urlencode(p).encode());
    if auth: r.add_header("Authorization",auth)
    t=time.perf_counter()
    try: d=json.load(urllib.request.urlopen(r,timeout=120)); ok=d["status"]=="success"; n=len(d["data"]["result"]) if ok else d["error"][:80]
    except Exception as e: ok=False; n=str(e)[:80]
    return (time.perf_counter()-t)*1000, ok, n
end=int(time.time()//15)*15-30
print(f"range={RANGE}s step=15s N={N}")
print(f"{'query':32} {'CH inst':>8} {'PR inst':>8} {'CH range':>9} {'PR range':>9}  resultado")
for name,q in Q.items():
    row=[]
    for path,p in (("query",{"query":q,"time":end}),("query_range",{"query":q,"start":end-RANGE,"end":end,"step":15})):
        for base,a in ((CH,AUTH),(PROM,None)):
            ts=[];info=None
            for _ in range(N):
                t,ok,n=call(base,path,p,a); ts.append(t); info=(ok,n)
            row.append((statistics.median(ts),info))
    print(f"{name:32} {row[0][0]:8.0f} {row[1][0]:8.0f} {row[2][0]:9.0f} {row[3][0]:9.0f}  ch={row[2][1][1]} prom={row[3][1][1]}")
