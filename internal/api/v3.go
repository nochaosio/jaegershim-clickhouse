package api

import (
	"sort"
	"strconv"
	"strings"

	"github.com/nochaosio/jaegershim-clickhouse/internal/chstore"
)

// Modelo da API v3 do Jaeger (OTLP/JSON), consumido pelo Jaeger UI 2.x.

type anyValue struct {
	StringValue *string     `json:"stringValue,omitempty"`
	ArrayValue  *arrayValue `json:"arrayValue,omitempty"`
}

type arrayValue struct {
	Values []anyValue `json:"values"`
}

func strVal(s string) anyValue { return anyValue{StringValue: &s} }

type otlpKV struct {
	Key   string   `json:"key"`
	Value anyValue `json:"value"`
}

type otlpEvent struct {
	TimeUnixNano string   `json:"timeUnixNano"`
	Name         string   `json:"name"`
	Attributes   []otlpKV `json:"attributes,omitempty"`
}

type otlpLink struct {
	TraceID    string   `json:"traceId"`
	SpanID     string   `json:"spanId"`
	TraceState string   `json:"traceState,omitempty"`
	Attributes []otlpKV `json:"attributes,omitempty"`
}

type otlpStatus struct {
	Message string `json:"message,omitempty"`
	Code    int    `json:"code,omitempty"`
}

type otlpSpan struct {
	TraceID           string      `json:"traceId"`
	SpanID            string      `json:"spanId"`
	TraceState        string      `json:"traceState,omitempty"`
	ParentSpanID      string      `json:"parentSpanId,omitempty"`
	Name              string      `json:"name"`
	Kind              int         `json:"kind"`
	StartTimeUnixNano string      `json:"startTimeUnixNano"`
	EndTimeUnixNano   string      `json:"endTimeUnixNano"`
	Attributes        []otlpKV    `json:"attributes,omitempty"`
	Events            []otlpEvent `json:"events,omitempty"`
	Links             []otlpLink  `json:"links,omitempty"`
	Status            otlpStatus  `json:"status"`
}

type otlpScope struct {
	Name    string `json:"name,omitempty"`
	Version string `json:"version,omitempty"`
}

type scopeSpans struct {
	Scope otlpScope  `json:"scope"`
	Spans []otlpSpan `json:"spans"`
}

type resource struct {
	Attributes []otlpKV `json:"attributes"`
}

type resourceSpans struct {
	Resource   resource     `json:"resource"`
	ScopeSpans []scopeSpans `json:"scopeSpans"`
}

type tracesData struct {
	ResourceSpans []resourceSpans `json:"resourceSpans"`
}

var kindNum = map[string]int{"unspecified": 0, "internal": 1, "server": 2, "client": 3, "producer": 4, "consumer": 5}

func otlpKVs(m map[string]string) []otlpKV {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := make([]otlpKV, 0, len(keys))
	for _, k := range keys {
		out = append(out, otlpKV{Key: k, Value: strVal(m[k])})
	}
	return out
}

func toOTLPSpan(s *chstore.Span) otlpSpan {
	o := otlpSpan{
		TraceID: s.TraceID, SpanID: s.SpanID, ParentSpanID: s.ParentSpanID, TraceState: s.TraceState,
		Name: s.Name, Kind: kindNum[s.Kind],
		StartTimeUnixNano: strconv.FormatInt(s.Timestamp.UnixNano(), 10),
		EndTimeUnixNano:   strconv.FormatInt(s.Timestamp.Add(s.Duration).UnixNano(), 10),
		Attributes:        otlpKVs(s.Attributes),
	}
	if len(s.Warnings) > 0 { // mesmo atributo que o Jaeger usa na API v3
		vals := make([]anyValue, 0, len(s.Warnings))
		for _, w := range s.Warnings {
			vals = append(vals, strVal(w))
		}
		o.Attributes = append(o.Attributes, otlpKV{Key: "@jaeger@warnings", Value: anyValue{ArrayValue: &arrayValue{Values: vals}}})
	}
	for _, e := range s.Events {
		o.Events = append(o.Events, otlpEvent{TimeUnixNano: strconv.FormatInt(e.Time.UnixNano(), 10), Name: e.Name, Attributes: otlpKVs(e.Attributes)})
	}
	for _, l := range s.Links {
		o.Links = append(o.Links, otlpLink{TraceID: l.TraceID, SpanID: l.SpanID, TraceState: l.TraceState, Attributes: otlpKVs(l.Attributes)})
	}
	switch s.StatusCode {
	case "OK":
		o.Status.Code = 1
	case "ERROR":
		o.Status.Code = 2
	}
	o.Status.Message = s.StatusMessage
	return o
}

// appendOTLP agrupa spans por resource (atributos) e escopo, como no OTLP.
func appendOTLP(td *tracesData, spans []chstore.Span) {
	resIdx := map[string]int{}
	scopeIdx := map[string]int{}
	for i := range spans {
		s := &spans[i]
		ra := s.ResourceAttributes
		if _, ok := ra["service.name"]; !ok && s.ServiceName != "" {
			ra = make(map[string]string, len(s.ResourceAttributes)+1)
			for k, v := range s.ResourceAttributes {
				ra[k] = v
			}
			ra["service.name"] = s.ServiceName
		}
		kvs := otlpKVs(ra)
		var rk strings.Builder
		for _, x := range kvs {
			rk.WriteString(x.Key + "=" + *x.Value.StringValue + "\x00")
		}
		ri, ok := resIdx[rk.String()]
		if !ok {
			ri = len(td.ResourceSpans)
			resIdx[rk.String()] = ri
			td.ResourceSpans = append(td.ResourceSpans, resourceSpans{Resource: resource{Attributes: kvs}})
		}
		sk := rk.String() + "|" + s.ScopeName + "|" + s.ScopeVersion
		si, ok := scopeIdx[sk]
		if !ok {
			si = len(td.ResourceSpans[ri].ScopeSpans)
			scopeIdx[sk] = si
			td.ResourceSpans[ri].ScopeSpans = append(td.ResourceSpans[ri].ScopeSpans, scopeSpans{Scope: otlpScope{Name: s.ScopeName, Version: s.ScopeVersion}})
		}
		td.ResourceSpans[ri].ScopeSpans[si].Spans = append(td.ResourceSpans[ri].ScopeSpans[si].Spans, toOTLPSpan(s))
	}
}

type serviceSummary struct {
	Name           string `json:"name"`
	SpanCount      int    `json:"spanCount"`
	ErrorSpanCount int    `json:"errorSpanCount,omitempty"`
}

type traceSummary struct {
	TraceID              string           `json:"traceId"`
	RootServiceName      string           `json:"rootServiceName,omitempty"`
	RootOperationName    string           `json:"rootOperationName,omitempty"`
	MinStartTimeUnixNano string           `json:"minStartTimeUnixNano"`
	MaxEndTimeUnixNano   string           `json:"maxEndTimeUnixNano"`
	SpanCount            int              `json:"spanCount"`
	ErrorSpanCount       int              `json:"errorSpanCount,omitempty"`
	OrphanSpanCount      int              `json:"orphanSpanCount,omitempty"`
	Services             []serviceSummary `json:"services"`
}

// summarize monta o resumo usado na lista de resultados do Jaeger UI.
// Raiz = span sem pai mais antigo (sem raiz: campos omitidos e orphanSpanCount, como o Jaeger).
func summarize(traceID string, spans []chstore.Span) traceSummary {
	ts := traceSummary{TraceID: traceID, SpanCount: len(spans)}
	svc := map[string]*serviceSummary{}
	var minStart, maxEnd int64
	var root *chstore.Span
	ids := make(map[string]bool, len(spans))
	for i := range spans {
		ids[spans[i].SpanID] = true
	}
	for i := range spans {
		s := &spans[i]
		st, en := s.Timestamp.UnixNano(), s.Timestamp.Add(s.Duration).UnixNano()
		if i == 0 || st < minStart {
			minStart = st
		}
		if en > maxEnd {
			maxEnd = en
		}
		ss := svc[s.ServiceName]
		if ss == nil {
			ss = &serviceSummary{Name: s.ServiceName}
			svc[s.ServiceName] = ss
		}
		ss.SpanCount++
		if s.IsError() {
			ss.ErrorSpanCount++
			ts.ErrorSpanCount++
		}
		if s.ParentSpanID == "" {
			if root == nil || s.Timestamp.Before(root.Timestamp) {
				root = s
			}
		} else if !ids[s.ParentSpanID] {
			ts.OrphanSpanCount++
		}
	}
	if root != nil {
		ts.RootServiceName, ts.RootOperationName = root.ServiceName, root.Name
	}
	ts.MinStartTimeUnixNano = strconv.FormatInt(minStart, 10)
	ts.MaxEndTimeUnixNano = strconv.FormatInt(maxEnd, 10)
	names := make([]string, 0, len(svc))
	for n := range svc {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		ts.Services = append(ts.Services, *svc[n])
	}
	return ts
}
