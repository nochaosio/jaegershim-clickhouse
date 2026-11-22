#!/usr/bin/env python3
"""Compara PromQL do ClickHouse (TimeSeries) contra um Prometheus de referência
alimentado com os mesmos dados (remote write duplo no OTel Collector).

Uso: python3 test/promql/promql_compat.py [--at UNIX_TS] [--json out.json]
"""
import argparse, base64, json, math, time, urllib.parse, urllib.request

CH = "http://localhost:8123/prometheus/api/v1"
PROM = "http://localhost:9090/api/v1"
CH_AUTH = "Basic " + base64.b64encode(b"default:clickhouse").decode()

C = 'avalanche_counter_metric_mmmmm_0_0_total'   # counter
G = 'avalanche_gauge_metric_mmmmm_0_0'           # gauge
H = 'avalanche_histogram_metric_mmmmm_0_0'       # classic histogram (_bucket/_sum/_count)

QUERIES = {
  # --- seletores e matchers
  "selector": f'{G}',
  "matcher_eq": f'{G}{{series_id="3"}}',
  "matcher_neq": f'{G}{{series_id!="3"}}',
  "matcher_re": f'{G}{{series_id=~"1.*"}}',
  "matcher_nre": f'{G}{{series_id!~"1.*"}}',
  "name_regex": '{__name__=~"avalanche_gauge_metric_mmmmm_0_[0-2]", series_id="0"}',
  "offset": f'{G}{{series_id="1"}} offset 2m',
  "at_modifier": f'{G}{{series_id="1"}} @ end()',
  # --- funções de range
  "rate": f'rate({C}[2m])',
  "irate": f'irate({C}[2m])',
  "increase": f'increase({C}[3m])',
  "delta": f'delta({G}[3m])',
  "idelta": f'idelta({G}[3m])',
  "deriv": f'deriv({G}[3m])',
  "resets": f'resets({C}[5m])',
  "changes": f'changes({G}[5m])',
  "avg_over_time": f'avg_over_time({G}[3m])',
  "min_over_time": f'min_over_time({G}[3m])',
  "max_over_time": f'max_over_time({G}[3m])',
  "sum_over_time": f'sum_over_time({G}[3m])',
  "count_over_time": f'count_over_time({G}[3m])',
  "last_over_time": f'last_over_time({G}[3m])',
  "present_over_time": f'present_over_time({G}[3m])',
  "stddev_over_time": f'stddev_over_time({G}[3m])',
  "stdvar_over_time": f'stdvar_over_time({G}[3m])',
  "quantile_over_time": f'quantile_over_time(0.9, {G}[3m])',
  "mad_over_time": f'mad_over_time({G}[3m])',
  "predict_linear": f'predict_linear({G}[3m], 600)',
  "double_exp_smoothing": f'double_exponential_smoothing({G}[3m], 0.5, 0.5)',
  "absent_over_time": 'absent_over_time(nao_existe[3m])',
  # --- agregações
  "sum": f'sum(rate({C}[2m]))',
  "sum_by": f'sum by (label_key_kkkkk_0) (rate({C}[2m]))',
  "sum_without": f'sum without (series_id) (rate({C}[2m]))',
  "avg": f'avg({G})', "min": f'min({G})', "max": f'max({G})',
  "count": f'count({G})', "group": f'group by (job) ({G})',
  "stddev": f'stddev({G})', "stdvar": f'stdvar({G})',
  "topk": f'topk(3, {G})', "bottomk": f'bottomk(3, {G})',
  "quantile": f'quantile(0.95, {G})',
  "count_values": f'count_values("v", round({G}))',
  "limitk": f'limitk(2, {G})',
  "limit_ratio": f'limit_ratio(0.5, {G})',
  # --- histogramas clássicos
  "histogram_quantile": f'histogram_quantile(0.95, sum by (le) (rate({H}_bucket[3m])))',
  "histogram_quantile_by": f'histogram_quantile(0.5, sum by (le, series_id) (rate({H}_bucket[3m])))',
  "histogram_avg_classic": f'sum(rate({H}_sum[3m])) / sum(rate({H}_count[3m]))',
  "histogram_fraction": f'histogram_fraction(0, 0.5, sum by (le) (rate({H}_bucket[3m])))',
  # --- operadores binários e vector matching
  "arith_scalar": f'{G} * 2 + 1',
  "arith_vectors": f'{G} - {G}',
  "comparison_filter": f'{G} > 50',
  "comparison_bool": f'{G} > bool 50',
  "and": f'{G} and {G}{{series_id="2"}}',
  "or": f'{G}{{series_id="1"}} or {G}{{series_id="2"}}',
  "unless": f'{G} unless {G}{{series_id="2"}}',
  "on": f'{G} / on(series_id, instance, job) {G}',
  "ignoring": f'{G} / ignoring(label_key_kkkkk_0) {G}',
  "group_left": f'{G} * on(job) group_left() count by (job) ({G})',
  "pow_mod": f'({G} % 7) ^ 2',
  "atan2": f'{G} atan2 {G}',
  # --- funções de vetor/label
  "abs": f'abs(-{G})', "ceil": f'ceil({G})', "floor": f'floor({G})', "round": f'round({G}, 5)',
  "sqrt": f'sqrt({G})', "exp": f'exp({G} / 1000)', "ln": f'ln({G} + 1)', "log2": f'log2({G} + 1)', "log10": f'log10({G} + 1)',
  "clamp": f'clamp({G}, 10, 50)', "clamp_min": f'clamp_min({G}, 30)', "clamp_max": f'clamp_max({G}, 30)',
  "sgn": f'sgn({G} - 50)', "trig": f'sin({G}) + cos({G})', "deg_rad": f'deg(rad({G}))',
  "label_replace": f'label_replace({G}, "sid", "s-$1", "series_id", "(.*)")',
  "label_join": f'label_join({G}, "j", "-", "job", "series_id")',
  "sort": f'sort({G})', "sort_desc": f'sort_desc({G})',
  "sort_by_label": f'sort_by_label({G}, "series_id")',
  "absent": 'absent(nao_existe)', "absent_present": f'absent({G})',
  "scalar_vector": f'vector(scalar(count({G})))',
  "time": 'time()', "timestamp": f'timestamp({G}{{series_id="1"}})',
  "date_funcs": f'hour(timestamp({G}{{series_id="1"}}))',
  "minute": 'minute()', "day_of_week": 'day_of_week()',
  "pi_number": 'pi()',
  # --- subqueries
  "subquery": f'max_over_time(rate({C}[1m])[5m:30s])',
  "subquery_nested": f'avg_over_time(sum(rate({C}[1m]))[4m:1m])',
  # --- string/edge cases
  "empty_result": f'{G}{{series_id="nao_existe"}}',
  "number_literal": '42', "string_literal": '"hello"',
  "info_func": f'info(rate({C}[2m]))',
  # --- métricas reais (collector / clickhouse)
  "real_rate_spans": 'sum(rate(otelcol_receiver_accepted_spans_total[2m]))',
  "real_hq_batch": 'histogram_quantile(0.99, sum by (le) (rate(otelcol_processor_batch_batch_send_size_bucket[5m])))',
}

def get(base, path, params, auth=None):
    url = f"{base}/{path}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if auth: req.add_header("Authorization", auth)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        try: body = json.load(e)
        except Exception: body = {"status": "error", "error": f"HTTP {e.code}"}
    except Exception as e:
        body = {"status": "error", "error": str(e)}
    return body, (time.perf_counter() - t0) * 1000

DROP = {"otel_scope_name", "otel_scope_version"}

def norm(res):
    """-> (resultType, {frozenset(labels): [values...]})"""
    d = res.get("data", {})
    rt = d.get("resultType")
    r = d.get("result")
    if rt in ("scalar", "string"):
        return rt, {frozenset(): [r[1]]}
    out = {}
    for s in r or []:
        lbl = frozenset((k, v) for k, v in s.get("metric", {}).items() if k not in DROP)
        vals = [s["value"][1]] if "value" in s else [v[1] for v in s.get("values", [])]
        out.setdefault(lbl, []).extend(vals)
    return rt, out

def close(a, b):
    try: fa, fb = float(a), float(b)
    except ValueError: return a == b
    if math.isnan(fa) and math.isnan(fb): return True
    if math.isinf(fa) or math.isinf(fb): return fa == fb
    return abs(fa - fb) <= 1e-6 * max(1.0, abs(fa), abs(fb))

def compare(ch, pr, ordered=False):
    if ch.get("status") != "success": return "CH_ERROR", ch.get("error", "")[:220]
    if pr.get("status") != "success": return "PROM_ERROR", pr.get("error", "")[:220]
    rt1, a = norm(ch); rt2, b = norm(pr)
    if rt1 != rt2: return "DIFF", f"resultType ch={rt1} prom={rt2}"
    if set(a) != set(b):
        only_ch = len(set(a) - set(b)); only_pr = len(set(b) - set(a))
        ex = next(iter(set(b) - set(a)), None) or next(iter(set(a) - set(b)), None)
        return "DIFF", f"séries: ch={len(a)} prom={len(b)} (só ch={only_ch}, só prom={only_pr}) ex={dict(ex) if ex else ''}"
    bad = 0; tot = 0; ex = ""
    for k in a:
        va, vb = a[k], b[k]
        if len(va) != len(vb):
            bad += 1; ex = ex or f"nº pontos ch={len(va)} prom={len(vb)}"; continue
        for x, y in zip(va, vb):
            tot += 1
            if not close(x, y):
                bad += 1; ex = ex or f"{dict(k)} ch={x} prom={y}"
    if ordered:
        ka = [frozenset((kk, vv) for kk, vv in s["metric"].items() if kk not in DROP) for s in ch["data"]["result"]]
        kb = [frozenset((kk, vv) for kk, vv in s["metric"].items() if kk not in DROP) for s in pr["data"]["result"]]
        if ka != kb: return "DIFF", "ordem diferente"
    return ("OK", f"{len(a)} séries") if bad == 0 else ("DIFF", f"{bad} valores diferentes de {tot}; {ex}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", type=float, default=None)
    ap.add_argument("--json")
    a = ap.parse_args()
    at = a.at or (int(time.time() // 15) * 15 - 60)
    start, end, step = at - 600, at, 30
    results = []
    print(f"avaliação em {time.strftime('%H:%M:%S', time.gmtime(at))} UTC  | range: 10m step 30s\n")
    print(f"{'query':24} {'instant':10} {'range':10} {'ch ms':>7} {'prom ms':>7}  detalhe")
    for name, q in QUERIES.items():
        ordered = name.startswith("sort")
        ch_i, t_ch = get(CH, "query", {"query": q, "time": at}, CH_AUTH)
        pr_i, t_pr = get(PROM, "query", {"query": q, "time": at})
        s_i, d_i = compare(ch_i, pr_i, ordered)
        if pr_i.get("data", {}).get("resultType") in ("vector", "matrix", "scalar") and not name.startswith(("string",)):
            ch_r, _ = get(CH, "query_range", {"query": q, "start": start, "end": end, "step": step}, CH_AUTH)
            pr_r, _ = get(PROM, "query_range", {"query": q, "start": start, "end": end, "step": step})
            s_r, d_r = compare(ch_r, pr_r) if pr_i["data"]["resultType"] != "matrix" else ("n/a", "")
        else:
            s_r, d_r = "n/a", ""
        det = d_i if s_i != "OK" else (d_r if s_r not in ("OK", "n/a") else d_i)
        print(f"{name:24} {s_i:10} {s_r:10} {t_ch:7.0f} {t_pr:7.0f}  {det}")
        results.append(dict(name=name, query=q, instant=s_i, range=s_r, detail=det, ch_ms=round(t_ch), prom_ms=round(t_pr)))
    ok_i = sum(r["instant"] == "OK" for r in results)
    ok_r = sum(r["range"] == "OK" for r in results); n_r = sum(r["range"] != "n/a" for r in results)
    print(f"\nINSTANT: {ok_i}/{len(results)} idênticos | RANGE: {ok_r}/{n_r} idênticos")
    if a.json: json.dump(results, open(a.json, "w"), indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
