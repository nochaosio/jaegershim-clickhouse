package api

import (
	"sort"
	"strings"

	"github.com/nochaosio/jaegershim-clickhouse/internal/chstore"
)

// Modelo JSON da API legacy do Jaeger (/api/*), consumido pelo datasource Jaeger do Grafana.

type kv struct {
	Key   string `json:"key"`
	Type  string `json:"type"`
	Value any    `json:"value"`
}

type reference struct {
	RefType string `json:"refType"`
	TraceID string `json:"traceID"`
	SpanID  string `json:"spanID"`
}

type logEntry struct {
	Timestamp int64 `json:"timestamp"`
	Fields    []kv  `json:"fields"`
}

type jSpan struct {
	TraceID       string      `json:"traceID"`
	SpanID        string      `json:"spanID"`
	OperationName string      `json:"operationName"`
	References    []reference `json:"references"`
	StartTime     int64       `json:"startTime"`
	Duration      int64       `json:"duration"`
	Tags          []kv        `json:"tags"`
	Logs          []logEntry  `json:"logs"`
	ProcessID     string      `json:"processID"`
	Warnings      []string    `json:"warnings"`
}

type process struct {
	ServiceName string `json:"serviceName"`
	Tags        []kv   `json:"tags"`
}

type jTrace struct {
	TraceID   string             `json:"traceID"`
	Spans     []jSpan            `json:"spans"`
	Processes map[string]process `json:"processes"`
	Warnings  []string           `json:"warnings"`
}

type structuredError struct {
	Code    int    `json:"code,omitempty"`
	Msg     string `json:"msg"`
	TraceID string `json:"traceID,omitempty"`
}

type structuredResponse struct {
	Data   any               `json:"data"`
	Total  int               `json:"total"`
	Limit  int               `json:"limit"`
	Offset int               `json:"offset"`
	Errors []structuredError `json:"errors"`
}

// Os atributos no ClickHouse são Map(String,String): o tipo original (int/bool/double)
// não é preservado pelo exporter, então todos os valores saem como "string".
func strKV(k, v string) kv { return kv{Key: k, Type: "string", Value: v} }

func sortedKVs(m map[string]string, skip ...string) []kv {
	keys := make([]string, 0, len(m))
outer:
	for k := range m {
		for _, s := range skip {
			if k == s {
				continue outer
			}
		}
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := make([]kv, 0, len(keys))
	for _, k := range keys {
		out = append(out, strKV(k, m[k]))
	}
	return out
}

// toLegacyTrace converte spans OTel (ClickHouse) no modelo Jaeger, seguindo as
// mesmas regras do tradutor OTLP->Jaeger do próprio Jaeger.
// jaegerTraceID imita model.TraceID.String() do Jaeger: IDs com os 64 bits altos
// zerados são impressos com 16 caracteres.
func jaegerTraceID(id string) string {
	if len(id) == 32 && id[:16] == "0000000000000000" {
		return id[16:]
	}
	return id
}

func toLegacyTrace(traceID string, spans []chstore.Span) jTrace {
	t := jTrace{TraceID: jaegerTraceID(traceID), Spans: make([]jSpan, 0, len(spans)), Processes: map[string]process{}}
	procIdx := map[string]string{}
	for i := range spans {
		s := &spans[i]
		ptags := sortedKVs(s.ResourceAttributes, "service.name")
		var pk strings.Builder
		pk.WriteString(s.ServiceName)
		for _, x := range ptags {
			pk.WriteString("\x00" + x.Key + "=" + x.Value.(string))
		}
		pid, ok := procIdx[pk.String()]
		if !ok {
			pid = "p" + itoa(len(procIdx)+1)
			procIdx[pk.String()] = pid
			t.Processes[pid] = process{ServiceName: s.ServiceName, Tags: ptags}
		}
		js := jSpan{
			TraceID:       jaegerTraceID(s.TraceID),
			SpanID:        s.SpanID,
			OperationName: s.Name,
			References:    []reference{},
			StartTime:     s.Timestamp.UnixMicro(),
			Duration:      s.Duration.Microseconds(),
			Logs:          []logEntry{},
			ProcessID:     pid,
			Warnings:      s.Warnings,
		}
		// Como no Jaeger: se um link aponta para o próprio pai, ele vira a primeira
		// referência (FOLLOWS_FROM) e o CHILD_OF não é emitido.
		parentLinked := false
		for _, l := range s.Links {
			if s.ParentSpanID != "" && l.TraceID == s.TraceID && l.SpanID == s.ParentSpanID {
				parentLinked = true
			}
		}
		if s.ParentSpanID != "" && !parentLinked {
			js.References = append(js.References, reference{RefType: "CHILD_OF", TraceID: jaegerTraceID(s.TraceID), SpanID: s.ParentSpanID})
		}
		if parentLinked {
			js.References = append(js.References, reference{RefType: "FOLLOWS_FROM", TraceID: jaegerTraceID(s.TraceID), SpanID: s.ParentSpanID})
		}
		for _, l := range s.Links {
			if parentLinked && l.TraceID == s.TraceID && l.SpanID == s.ParentSpanID {
				continue
			}
			js.References = append(js.References, reference{RefType: "FOLLOWS_FROM", TraceID: jaegerTraceID(l.TraceID), SpanID: l.SpanID})
		}
		// tags: escopo, atributos do span, depois os sintéticos (mesma ordem do Jaeger)
		if s.ScopeName != "" {
			js.Tags = append(js.Tags, strKV("otel.scope.name", s.ScopeName))
		}
		if s.ScopeVersion != "" {
			js.Tags = append(js.Tags, strKV("otel.scope.version", s.ScopeVersion))
		}
		js.Tags = append(js.Tags, sortedKVs(s.Attributes)...)
		if s.Kind != "" && s.Kind != "unspecified" {
			js.Tags = append(js.Tags, strKV("span.kind", s.Kind))
		}
		switch s.StatusCode {
		case "OK":
			js.Tags = append(js.Tags, strKV("otel.status_code", "OK"))
		case "ERROR":
			js.Tags = append(js.Tags, strKV("otel.status_code", "ERROR"), kv{Key: "error", Type: "bool", Value: true})
		}
		if s.StatusMessage != "" {
			js.Tags = append(js.Tags, strKV("otel.status_description", s.StatusMessage))
		}
		if s.TraceState != "" {
			js.Tags = append(js.Tags, strKV("w3c.tracestate", s.TraceState))
		}
		if js.Tags == nil {
			js.Tags = []kv{}
		}
		for _, e := range s.Events {
			fields := []kv{}
			if e.Name != "" {
				fields = append(fields, strKV("event", e.Name))
			}
			fields = append(fields, sortedKVs(e.Attributes)...)
			js.Logs = append(js.Logs, logEntry{Timestamp: e.Time.UnixMicro(), Fields: fields})
		}
		t.Spans = append(t.Spans, js)
	}
	return t
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
