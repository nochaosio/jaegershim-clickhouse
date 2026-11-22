package api

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"

	"github.com/nochaosio/clickhouse-jaeger/internal/chstore"
)

// ---------- fake store ----------

type fakeStore struct {
	lastQuery chstore.TraceQuery
	traces    map[string][]chstore.Span
}

func (f *fakeStore) Ping(context.Context) error { return nil }
func (f *fakeStore) Services(context.Context) ([]string, error) {
	return []string{"checkout", "frontend"}, nil
}
func (f *fakeStore) Operations(_ context.Context, svc, kind string) ([]chstore.Operation, error) {
	all := []chstore.Operation{{Name: "POST /pay", Kind: "client"}, {Name: "checkout.process", Kind: "internal"}}
	if svc != "checkout" {
		return []chstore.Operation{}, nil
	}
	out := []chstore.Operation{}
	for _, o := range all {
		if kind == "" || o.Kind == kind {
			out = append(out, o)
		}
	}
	return out, nil
}
func (f *fakeStore) FindTraceIDs(_ context.Context, q chstore.TraceQuery) ([]string, error) {
	f.lastQuery = q
	ids := []string{}
	for id := range f.traces {
		ids = append(ids, id)
	}
	return ids, nil
}
func (f *fakeStore) GetTraces(_ context.Context, ids []string) (map[string][]chstore.Span, error) {
	out := map[string][]chstore.Span{}
	for _, id := range ids {
		if t, ok := f.traces[id]; ok {
			out[id] = t
		}
	}
	return out, nil
}
func (f *fakeStore) Dependencies(context.Context, time.Time, time.Time) ([]chstore.Dependency, error) {
	return []chstore.Dependency{{Parent: "frontend", Child: "checkout", CallCount: 3}}, nil
}

const tid = "0af7651916cd43dd8448eb211c80319c"

func sampleTrace() []chstore.Span {
	t0 := time.Unix(1_700_000_000, 123_456_789)
	res := map[string]string{"service.name": "frontend", "cloud.region": "sa-east-1"}
	return []chstore.Span{
		{Timestamp: t0, TraceID: tid, SpanID: "b7ad6b7169203331", Name: "GET /checkout", Kind: "server",
			ServiceName: "frontend", ResourceAttributes: res, ScopeName: "lib", ScopeVersion: "1.0",
			Attributes: map[string]string{"http.route": "/checkout"}, Duration: 1500 * time.Millisecond,
			StatusCode: "ERROR", StatusMessage: "boom"},
		{Timestamp: t0.Add(10 * time.Millisecond), TraceID: tid, SpanID: "00f067aa0ba902b7", ParentSpanID: "b7ad6b7169203331",
			Name: "POST /pay", Kind: "client", ServiceName: "checkout",
			ResourceAttributes: map[string]string{"service.name": "checkout"}, Attributes: map[string]string{},
			Duration: 20 * time.Millisecond, StatusCode: "UNSET",
			Events: []chstore.Event{{Time: t0.Add(15 * time.Millisecond), Name: "exception", Attributes: map[string]string{"exception.type": "X"}}},
			Links:  []chstore.Link{{TraceID: "11111111111111111111111111111111", SpanID: "2222222222222222"}}},
	}
}

func newTestServer() (*Server, *fakeStore) {
	fs := &fakeStore{traces: map[string][]chstore.Span{tid: sampleTrace()}}
	s := New(fs, "", "test", slog.New(slog.NewTextHandler(io.Discard, nil)))
	s.now = func() time.Time { return time.Unix(1_700_000_100, 0) }
	return s, fs
}

func get(t *testing.T, s *Server, path string) (int, map[string]any) {
	t.Helper()
	rec := httptest.NewRecorder()
	s.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("%s: invalid json %q: %v", path, rec.Body.String(), err)
	}
	return rec.Code, body
}

// ---------- params ----------

func TestNormalizeTraceID(t *testing.T) {
	cases := map[string]string{
		tid:                tid,
		"ABCDEF":           "00000000000000000000000000abcdef",
		"af7651916cd43dd8": "0000000000000000af7651916cd43dd8",
	}
	for in, want := range cases {
		got, err := normalizeTraceID(in)
		if err != nil || got != want {
			t.Errorf("normalizeTraceID(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	for _, bad := range []string{"", "zzz", "0af7651916cd43dd8448eb211c80319c0"} {
		if _, err := normalizeTraceID(bad); err == nil {
			t.Errorf("normalizeTraceID(%q) expected error", bad)
		}
	}
}

func TestParseTags(t *testing.T) {
	got, err := parseTags(`{"error":"true","http.status_code":500,"ok":true}`, []string{"a:b:c"})
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"error": "true", "http.status_code": "500", "ok": "true", "a": "b:c"}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("tag %s = %q want %q", k, got[k], v)
		}
	}
	lf, err := parseTags(`error=true msg="hello world" x=1`, nil)
	if err != nil || lf["msg"] != "hello world" || lf["error"] != "true" || lf["x"] != "1" {
		t.Errorf("logfmt parse: %v %v", lf, err)
	}
	if _, err := parseTags(`{bad`, nil); err == nil {
		t.Error("expected error for malformed json")
	}
	if _, err := parseTags("", []string{"nocolon"}); err == nil {
		t.Error("expected error for malformed tag")
	}
}

func TestParseLegacyQuery(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	v := url.Values{"service": {"a"}, "minDuration": {"1.5s"}, "maxDuration": {"2s"}, "limit": {"5"},
		"start": {"1699999000000000"}, "end": {"1700000000000000"}, "tags": {`{"k":"v"}`}}
	q, ids, err := parseLegacyQuery(v, now)
	if err != nil || len(ids) != 0 {
		t.Fatal(err)
	}
	if q.DurationMin != 1500*time.Millisecond || q.DurationMax != 2*time.Second || q.Limit != 5 ||
		q.StartMin.Unix() != 1_699_999_000 || q.StartMax.Unix() != 1_700_000_000 || q.Tags["k"] != "v" {
		t.Errorf("unexpected query: %+v", q)
	}
	q, _, _ = parseLegacyQuery(url.Values{"service": {"a"}, "lookback": {"1h"}}, now)
	if q.StartMax != now || q.StartMin != now.Add(-time.Hour) || q.Limit != defaultLimit {
		t.Errorf("lookback/defaults: %+v", q)
	}
	for _, bad := range []url.Values{
		{},
		{"service": {"a"}, "minDuration": {"abc"}},
		{"service": {"a"}, "minDuration": {"2s"}, "maxDuration": {"1s"}},
		{"service": {"a"}, "limit": {"x"}},
		{"traceID": {"zz"}},
	} {
		if _, _, err := parseLegacyQuery(bad, now); err == nil {
			t.Errorf("expected error for %v", bad)
		}
	}
}

func TestParseV3Query(t *testing.T) {
	v := url.Values{"query.serviceName": {"a"}, "query.startTimeMin": {"2026-01-01T00:00:00Z"},
		"query.startTimeMax": {"2026-01-01T01:00:00.5Z"}, "query.searchDepth": {"7"},
		"query.durationMin": {"10ms"}, "query.attributes": {`{"error":"true"}`}}
	q, err := parseV3Query(v)
	if err != nil {
		t.Fatal(err)
	}
	if q.Service != "a" || q.Limit != 7 || q.DurationMin != 10*time.Millisecond || q.Tags["error"] != "true" {
		t.Errorf("unexpected: %+v", q)
	}
	// snake_case também é aceito
	q, err = parseV3Query(url.Values{"query.service_name": {"b"}, "query.start_time_min": {"2026-01-01T00:00:00Z"}, "query.start_time_max": {"2026-01-01T00:00:01Z"}})
	if err != nil || q.Service != "b" {
		t.Errorf("snake_case: %+v %v", q, err)
	}
	if _, err := parseV3Query(url.Values{"query.serviceName": {"a"}}); err == nil {
		t.Error("expected error without time range")
	}
}

// ---------- conversão ----------

func TestToLegacyTrace(t *testing.T) {
	jt := toLegacyTrace(tid, sampleTrace())
	if len(jt.Spans) != 2 || len(jt.Processes) != 2 {
		t.Fatalf("spans=%d processes=%d", len(jt.Spans), len(jt.Processes))
	}
	root, child := jt.Spans[0], jt.Spans[1]
	if root.StartTime != 1_700_000_000_123_456 || root.Duration != 1_500_000 {
		t.Errorf("µs conversion: start=%d dur=%d", root.StartTime, root.Duration)
	}
	tags := map[string]any{}
	for _, k := range root.Tags {
		tags[k.Key] = k.Value
	}
	for k, want := range map[string]any{"otel.scope.name": "lib", "span.kind": "server", "otel.status_code": "ERROR",
		"error": true, "otel.status_description": "boom", "http.route": "/checkout"} {
		if tags[k] != want {
			t.Errorf("root tag %s = %v want %v", k, tags[k], want)
		}
	}
	if len(root.References) != 0 {
		t.Error("root must not have references")
	}
	if len(child.References) != 2 || child.References[0].RefType != "CHILD_OF" || child.References[1].RefType != "FOLLOWS_FROM" {
		t.Errorf("child references: %+v", child.References)
	}
	if len(child.Logs) != 1 || child.Logs[0].Fields[0].Value != "exception" {
		t.Errorf("logs: %+v", child.Logs)
	}
	p := jt.Processes[root.ProcessID]
	if p.ServiceName != "frontend" || len(p.Tags) != 1 || p.Tags[0].Key != "cloud.region" {
		t.Errorf("process: %+v", p)
	}
}

func TestSummarize(t *testing.T) {
	ts := summarize(tid, sampleTrace())
	if ts.RootServiceName != "frontend" || ts.RootOperationName != "GET /checkout" || ts.SpanCount != 2 || ts.ErrorSpanCount != 1 {
		t.Errorf("summary: %+v", ts)
	}
	if len(ts.Services) != 2 || ts.Services[0].Name != "checkout" || ts.Services[1].ErrorSpanCount != 1 {
		t.Errorf("services: %+v", ts.Services)
	}
	if ts.MinStartTimeUnixNano != "1700000000123456789" || ts.MaxEndTimeUnixNano != "1700000001623456789" {
		t.Errorf("times: %s %s", ts.MinStartTimeUnixNano, ts.MaxEndTimeUnixNano)
	}
}

// ---------- HTTP ----------

func TestLegacyEndpoints(t *testing.T) {
	s, fs := newTestServer()
	if code, b := get(t, s, "/api/services"); code != 200 || len(b["data"].([]any)) != 2 || b["total"].(float64) != 2 {
		t.Errorf("services: %d %v", code, b)
	}
	if _, b := get(t, s, "/api/services/checkout/operations"); len(b["data"].([]any)) != 2 {
		t.Errorf("service operations: %v", b)
	}
	if _, b := get(t, s, "/api/services/nope/operations"); b["data"] != nil {
		t.Errorf("empty operations must be null: %v", b)
	}
	if _, b := get(t, s, "/api/operations?service=checkout&spanKind=client"); len(b["data"].([]any)) != 1 {
		t.Errorf("operations by kind: %v", b)
	}
	code, b := get(t, s, `/api/traces?service=frontend&tags=%7B%22error%22%3A%22true%22%7D&minDuration=1s&limit=3`)
	if code != 200 || len(b["data"].([]any)) != 1 || b["errors"] != nil {
		t.Errorf("search: %d %v", code, b)
	}
	if fs.lastQuery.Tags["error"] != "true" || fs.lastQuery.DurationMin != time.Second || fs.lastQuery.Limit != 3 {
		t.Errorf("query passed to store: %+v", fs.lastQuery)
	}
	if code, b := get(t, s, "/api/traces/"+tid); code != 200 || len(b["data"].([]any)) != 1 {
		t.Errorf("trace: %d %v", code, b)
	}
	if code, b := get(t, s, "/api/traces/00000000000000000000000000000001"); code != 404 || b["errors"] == nil {
		t.Errorf("not found: %d %v", code, b)
	}
	if code, _ := get(t, s, "/api/traces/zzz"); code != 400 {
		t.Errorf("bad id: %d", code)
	}
	if code, b := get(t, s, "/api/traces"); code != 400 || b["errors"].([]any)[0].(map[string]any)["msg"] != "parameter 'service' is required" {
		t.Errorf("missing service: %d %v", code, b)
	}
	if code, b := get(t, s, "/api/traces?traceID="+tid+"&traceID=1"); code != 200 || len(b["data"].([]any)) != 1 || len(b["errors"].([]any)) != 1 {
		t.Errorf("multi traceID: %d %v", code, b)
	}
	if _, b := get(t, s, "/api/dependencies?endTs=1700000000000&lookback=3600000"); b["data"].([]any)[0].(map[string]any)["callCount"].(float64) != 3 {
		t.Errorf("deps: %v", b)
	}
}

func TestV3Endpoints(t *testing.T) {
	s, _ := newTestServer()
	if _, b := get(t, s, "/api/v3/services"); len(b["services"].([]any)) != 2 {
		t.Errorf("v3 services: %v", b)
	}
	if _, b := get(t, s, "/api/v3/operations?service=checkout&span_kind=internal"); len(b["operations"].([]any)) != 1 {
		t.Errorf("v3 operations: %v", b)
	}
	q := url.Values{"query.serviceName": {"frontend"}, "query.startTimeMin": {"2023-11-14T00:00:00Z"}, "query.startTimeMax": {"2023-11-15T00:00:00Z"}}
	code, b := get(t, s, withQuery("/api/v3/trace-summaries", q))
	if code != 200 || len(b["summaries"].([]any)) != 1 {
		t.Errorf("summaries: %d %v", code, b)
	}
	if code, b := get(t, s, "/api/v3/trace-summaries?query.serviceName=x"); code != 400 || b["error"] == nil {
		t.Errorf("summaries without time: %d %v", code, b)
	}
	code, b = get(t, s, "/api/v3/traces/"+tid)
	rs := b["result"].(map[string]any)["resourceSpans"].([]any)
	if code != 200 || len(rs) != 2 {
		t.Fatalf("v3 trace: %d %v", code, b)
	}
	if code, b := get(t, s, "/api/v3/traces/00000000000000000000000000000001"); code != 404 || b["error"].(map[string]any)["message"] != "No traces found" {
		t.Errorf("v3 not found: %d %v", code, b)
	}
	if code, b := get(t, s, withQuery("/api/v3/traces", q)); code != 200 || b["result"] == nil {
		t.Errorf("v3 search: %d %v", code, b)
	}
}

func TestUnknownAPI404(t *testing.T) {
	s, _ := newTestServer()
	rec := httptest.NewRecorder()
	s.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/nope", nil))
	if rec.Code != 404 {
		t.Errorf("unknown api: %d", rec.Code)
	}
}
