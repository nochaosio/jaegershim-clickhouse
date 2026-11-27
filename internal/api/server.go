package api

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/nochaosio/jaegershim-clickhouse/internal/chstore"
)

// Store é a interface usada pelos handlers (permite testes sem ClickHouse).
type Store interface {
	Ping(ctx context.Context) error
	Services(ctx context.Context) ([]string, error)
	Operations(ctx context.Context, service, kind string) ([]chstore.Operation, error)
	FindTraceIDs(ctx context.Context, q chstore.TraceQuery) ([]string, error)
	GetTraces(ctx context.Context, ids []string) (map[string][]chstore.Span, error)
	Dependencies(ctx context.Context, start, end time.Time) ([]chstore.Dependency, error)
}

// Server expõe a API do Jaeger Query (legacy /api/* e v3 /api/v3/*) e o Jaeger UI.
type Server struct {
	store   Store
	uiDir   string
	version string
	log     *slog.Logger
	now     func() time.Time
	mux     *http.ServeMux
}

func New(store Store, uiDir, version string, log *slog.Logger) *Server {
	s := &Server{store: store, uiDir: uiDir, version: version, log: log, now: time.Now, mux: http.NewServeMux()}
	// API legacy (datasource Jaeger do Grafana, Jaeger UI < 2.21)
	s.mux.HandleFunc("GET /api/services", s.legacyServices)
	s.mux.HandleFunc("GET /api/services/{service}/operations", s.legacyServiceOperations)
	s.mux.HandleFunc("GET /api/operations", s.legacyOperations)
	s.mux.HandleFunc("GET /api/traces", s.legacySearch)
	s.mux.HandleFunc("GET /api/traces/{traceID}", s.legacyTrace)
	s.mux.HandleFunc("GET /api/dependencies", s.legacyDependencies)
	// API v3 (Jaeger UI 2.x)
	s.mux.HandleFunc("GET /api/v3/services", s.v3Services)
	s.mux.HandleFunc("GET /api/v3/operations", s.v3Operations)
	s.mux.HandleFunc("GET /api/v3/traces", s.v3Search)
	s.mux.HandleFunc("GET /api/v3/traces/{traceID}", s.v3Trace)
	s.mux.HandleFunc("GET /api/v3/trace-summaries", s.v3Summaries)
	// saúde
	s.mux.HandleFunc("GET /health", s.health)
	s.mux.HandleFunc("/api/", func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "404 page not found", http.StatusNotFound)
	})
	// UI
	s.mux.HandleFunc("/", s.ui)
	return s
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	start := time.Now()
	rw := &statusWriter{ResponseWriter: w, code: 200}
	s.mux.ServeHTTP(rw, r)
	if strings.HasPrefix(r.URL.Path, "/api/") {
		s.log.Info("request", "method", r.Method, "path", r.URL.Path, "query", r.URL.RawQuery,
			"status", rw.code, "duration_ms", time.Since(start).Milliseconds(), "ua", r.UserAgent())
	}
}

type statusWriter struct {
	http.ResponseWriter
	code int
}

func (w *statusWriter) WriteHeader(c int) { w.code = c; w.ResponseWriter.WriteHeader(c) }

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

// ---------- legacy ----------

func legacyOK(data any, total int) structuredResponse {
	return structuredResponse{Data: data, Total: total}
}

func (s *Server) legacyErr(w http.ResponseWriter, code int, err error) {
	if code >= 500 {
		s.log.Error("store error", "err", err)
	}
	writeJSON(w, code, structuredResponse{Errors: []structuredError{{Code: code, Msg: err.Error()}}})
}

func (s *Server) legacyServices(w http.ResponseWriter, r *http.Request) {
	svcs, err := s.store.Services(r.Context())
	if err != nil {
		s.legacyErr(w, 500, err)
		return
	}
	writeJSON(w, 200, legacyOK(svcs, len(svcs)))
}

func (s *Server) legacyServiceOperations(w http.ResponseWriter, r *http.Request) {
	ops, err := s.store.Operations(r.Context(), r.PathValue("service"), "")
	if err != nil {
		s.legacyErr(w, 500, err)
		return
	}
	seen := map[string]bool{}
	names := []string{}
	for _, o := range ops {
		if !seen[o.Name] {
			seen[o.Name] = true
			names = append(names, o.Name)
		}
	}
	var data any = names
	if len(names) == 0 {
		data = nil // o Jaeger devolve "data": null quando não há operações
	}
	writeJSON(w, 200, legacyOK(data, len(names)))
}

type jOperation struct {
	Name     string `json:"name"`
	SpanKind string `json:"spanKind"`
}

// toJOps: o Jaeger devolve kind "unspecified" como "" na API legacy e como "internal" na v3.
func toJOps(ops []chstore.Operation, unspecifiedAs string) []jOperation {
	out := make([]jOperation, 0, len(ops))
	for _, o := range ops {
		k := o.Kind
		if k == "unspecified" {
			k = unspecifiedAs
		}
		out = append(out, jOperation{Name: o.Name, SpanKind: k})
	}
	return out
}

func (s *Server) legacyOperations(w http.ResponseWriter, r *http.Request) {
	svc := r.URL.Query().Get("service")
	if svc == "" {
		s.legacyErr(w, 400, errors.New("parameter 'service' is required"))
		return
	}
	ops, err := s.store.Operations(r.Context(), svc, strings.ToLower(r.URL.Query().Get("spanKind")))
	if err != nil {
		s.legacyErr(w, 500, err)
		return
	}
	jo := toJOps(ops, "")
	writeJSON(w, 200, legacyOK(jo, len(jo)))
}

func (s *Server) loadTraces(ctx context.Context, ids []string) ([]string, map[string][]chstore.Span, error) {
	m, err := s.store.GetTraces(ctx, ids)
	if err != nil {
		return ids, nil, err
	}
	for id, sp := range m {
		m[id] = adjustTrace(sp)
	}
	return ids, m, nil
}

func (s *Server) legacySearch(w http.ResponseWriter, r *http.Request) {
	q, ids, err := parseLegacyQuery(r.URL.Query(), s.now())
	if err != nil {
		s.legacyErr(w, 400, err)
		return
	}
	if len(ids) == 0 {
		if ids, err = s.store.FindTraceIDs(r.Context(), q); err != nil {
			s.legacyErr(w, 500, err)
			return
		}
	}
	ids, spans, err := s.loadTraces(r.Context(), ids)
	if err != nil {
		s.legacyErr(w, 500, err)
		return
	}
	traces := []jTrace{}
	var errs []structuredError
	for _, id := range ids {
		if sp, ok := spans[id]; ok {
			traces = append(traces, toLegacyTrace(id, sp))
		} else if r.URL.Query().Has("traceID") {
			errs = append(errs, structuredError{Code: 404, Msg: "trace not found", TraceID: id})
		}
	}
	writeJSON(w, 200, structuredResponse{Data: traces, Errors: errs})
}

func (s *Server) legacyTrace(w http.ResponseWriter, r *http.Request) {
	id, err := normalizeTraceID(r.PathValue("traceID"))
	if err != nil {
		s.legacyErr(w, 400, err)
		return
	}
	_, spans, err := s.loadTraces(r.Context(), []string{id})
	if err != nil {
		s.legacyErr(w, 500, err)
		return
	}
	sp, ok := spans[id]
	if !ok {
		s.legacyErr(w, 404, errors.New("trace not found"))
		return
	}
	writeJSON(w, 200, legacyOK([]jTrace{toLegacyTrace(id, sp)}, 0))
}

type jDependency struct {
	Parent    string `json:"parent"`
	Child     string `json:"child"`
	CallCount uint64 `json:"callCount"`
}

func (s *Server) legacyDependencies(w http.ResponseWriter, r *http.Request) {
	v := r.URL.Query()
	end := s.now()
	if e := v.Get("endTs"); e != "" {
		ms, err := strconv.ParseInt(e, 10, 64)
		if err != nil {
			s.legacyErr(w, 400, errors.New("unable to parse param 'endTs': "+err.Error()))
			return
		}
		end = time.UnixMilli(ms)
	}
	lookback := 24 * time.Hour
	if l := v.Get("lookback"); l != "" {
		ms, err := strconv.ParseInt(l, 10, 64)
		if err != nil {
			s.legacyErr(w, 400, errors.New("unable to parse param 'lookback': "+err.Error()))
			return
		}
		lookback = time.Duration(ms) * time.Millisecond
	}
	deps, err := s.store.Dependencies(r.Context(), end.Add(-lookback), end)
	if err != nil {
		s.legacyErr(w, 500, err)
		return
	}
	out := make([]jDependency, 0, len(deps))
	for _, d := range deps {
		out = append(out, jDependency{Parent: d.Parent, Child: d.Child, CallCount: d.CallCount})
	}
	writeJSON(w, 200, structuredResponse{Data: out})
}

// ---------- v3 ----------

type v3Error struct {
	Error struct {
		HTTPCode int    `json:"httpCode"`
		Message  string `json:"message"`
	} `json:"error"`
}

func (s *Server) v3Err(w http.ResponseWriter, code int, err error) {
	if code >= 500 {
		s.log.Error("store error", "err", err)
	}
	var e v3Error
	e.Error.HTTPCode, e.Error.Message = code, err.Error()
	writeJSON(w, code, e)
}

func (s *Server) v3Services(w http.ResponseWriter, r *http.Request) {
	svcs, err := s.store.Services(r.Context())
	if err != nil {
		s.v3Err(w, 500, err)
		return
	}
	writeJSON(w, 200, map[string]any{"services": svcs})
}

func (s *Server) v3Operations(w http.ResponseWriter, r *http.Request) {
	v := r.URL.Query()
	svc := v.Get("service")
	if svc == "" {
		s.v3Err(w, 400, errors.New("service name is required"))
		return
	}
	ops, err := s.store.Operations(r.Context(), svc, strings.ToLower(first(v, "span_kind", "spanKind")))
	if err != nil {
		s.v3Err(w, 500, err)
		return
	}
	writeJSON(w, 200, map[string]any{"operations": toJOps(ops, "internal")})
}

func (s *Server) v3Find(w http.ResponseWriter, r *http.Request) ([]string, map[string][]chstore.Span, bool) {
	q, err := parseV3Query(r.URL.Query())
	if err != nil {
		s.v3Err(w, 400, err)
		return nil, nil, false
	}
	ids, err := s.store.FindTraceIDs(r.Context(), q)
	if err != nil {
		s.v3Err(w, 500, err)
		return nil, nil, false
	}
	ids, spans, err := s.loadTraces(r.Context(), ids)
	if err != nil {
		s.v3Err(w, 500, err)
		return nil, nil, false
	}
	return ids, spans, true
}

func (s *Server) v3Search(w http.ResponseWriter, r *http.Request) {
	ids, spans, ok := s.v3Find(w, r)
	if !ok {
		return
	}
	td := tracesData{ResourceSpans: []resourceSpans{}}
	for _, id := range ids {
		appendOTLP(&td, spans[id])
	}
	if len(td.ResourceSpans) == 0 {
		s.v3Err(w, 404, errors.New("No traces found"))
		return
	}
	writeJSON(w, 200, map[string]any{"result": td})
}

func (s *Server) v3Summaries(w http.ResponseWriter, r *http.Request) {
	ids, spans, ok := s.v3Find(w, r)
	if !ok {
		return
	}
	out := []traceSummary{}
	for _, id := range ids {
		if sp, ok := spans[id]; ok {
			out = append(out, summarize(id, sp))
		}
	}
	writeJSON(w, 200, map[string]any{"summaries": out})
}

func (s *Server) v3Trace(w http.ResponseWriter, r *http.Request) {
	id, err := normalizeTraceID(r.PathValue("traceID"))
	if err != nil {
		s.v3Err(w, 400, err)
		return
	}
	_, spans, err := s.loadTraces(r.Context(), []string{id})
	if err != nil {
		s.v3Err(w, 500, err)
		return
	}
	sp, ok := spans[id]
	if !ok {
		s.v3Err(w, 404, errors.New("No traces found"))
		return
	}
	td := tracesData{}
	appendOTLP(&td, sp)
	writeJSON(w, 200, map[string]any{"result": td})
}

// ---------- saúde e UI ----------

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	if err := s.store.Ping(ctx); err != nil {
		writeJSON(w, 503, map[string]string{"status": "unavailable", "error": err.Error()})
		return
	}
	writeJSON(w, 200, map[string]string{"status": "ok"})
}

func (s *Server) ui(w http.ResponseWriter, r *http.Request) {
	if s.uiDir == "" {
		writeJSON(w, 200, map[string]string{"service": "jaegershim", "version": s.version})
		return
	}
	clean := filepath.Clean("/" + r.URL.Path)
	p := filepath.Join(s.uiDir, clean)
	if st, err := os.Stat(p); err == nil && !st.IsDir() && clean != "/index.html" {
		if strings.HasPrefix(clean, "/static/") {
			w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
		}
		http.ServeFile(w, r, p)
		return
	}
	// SPA: qualquer outra rota (/search, /trace/..., /dependencies) devolve o index.html
	b, err := os.ReadFile(filepath.Join(s.uiDir, "index.html"))
	if err != nil {
		http.Error(w, "UI not available", http.StatusNotFound)
		return
	}
	html := strings.Replace(string(b), "const JAEGER_VERSION = DEFAULT_VERSION;",
		"const JAEGER_VERSION = "+mustJSON(map[string]string{"gitVersion": "jaegershim " + s.version, "gitCommit": "", "buildDate": ""})+";", 1)
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache")
	_, _ = w.Write([]byte(html))
}

func mustJSON(v any) string { b, _ := json.Marshal(v); return string(b) }

// helper para testes
func withQuery(path string, v url.Values) string { return path + "?" + v.Encode() }
