package api

import (
	"fmt"
	"time"

	"github.com/nochaosio/jaegershim-clickhouse/internal/chstore"
)

// Reproduz os "adjusters" que o Jaeger Query aplica antes de devolver um trace
// (mesmo resultado nas APIs legacy e v3):
//   - nomes vazios: operação "" -> "empty-span-name", serviço "" -> "missing-service-name"
//   - SpanIDUniquifier: spans server que compartilham o span ID com um span client
//     (estilo Zipkin) ganham um ID novo e viram filhos do span client
//   - sanitizer de duração: duração 0 -> fim = início + 1ns, com warning
//   - warning para spans cujo pai não está no trace (clock skew adjuster)

const (
	emptySpanName      = "empty-span-name"
	missingServiceName = "missing-service-name"
)

func adjustTrace(in []chstore.Span) []chstore.Span {
	spans := make([]chstore.Span, len(in))
	copy(spans, in)
	for i := range spans {
		s := &spans[i]
		s.Warnings = nil
		if s.Name == "" {
			s.Name = emptySpanName
		}
		if s.ServiceName == "" {
			s.ServiceName = missingServiceName
		}
	}
	uniquifySpanIDs(spans)
	ids := make(map[string]bool, len(spans))
	for i := range spans {
		ids[spans[i].SpanID] = true
	}
	for i := range spans {
		s := &spans[i]
		if s.Duration <= 0 {
			orig := s.Timestamp
			s.Duration = time.Nanosecond
			s.Warnings = append(s.Warnings, fmt.Sprintf(
				"Negative duration detected, sanitizing end timestamp. Original end timestamp: %s, adjusted to: %s",
				orig.UTC().String(), orig.Add(time.Nanosecond).UTC().String()))
		}
		if s.ParentSpanID != "" && !ids[s.ParentSpanID] {
			s.Warnings = append(s.Warnings, fmt.Sprintf("parent span ID=%s is not in the trace; skipping clock skew adjustment", s.ParentSpanID))
		}
	}
	return spans
}

// uniquifySpanIDs segue o SpanIDUniquifier do Jaeger.
func uniquifySpanIDs(spans []chstore.Span) {
	byID := map[string][]int{}
	for i := range spans {
		byID[spans[i].SpanID] = append(byID[spans[i].SpanID], i)
	}
	sharedWithClient := func(id string) bool {
		for _, i := range byID[id] {
			if spans[i].Kind == "client" {
				return true
			}
		}
		return false
	}
	used := map[string]bool{}
	for id := range byID {
		used[id] = true
	}
	var next uint64
	newID := func() string {
		for {
			next++
			id := fmt.Sprintf("%016x", next)
			if !used[id] {
				used[id] = true
				return id
			}
		}
	}
	oldToNew := map[string]string{}
	for i := range spans {
		s := &spans[i]
		if s.Kind == "server" && len(byID[s.SpanID]) > 1 && sharedWithClient(s.SpanID) {
			n := newID()
			oldToNew[s.SpanID] = n
			s.ParentSpanID = s.SpanID
			s.SpanID = n
		}
	}
	if len(oldToNew) == 0 {
		return
	}
	for i := range spans {
		s := &spans[i]
		if n, ok := oldToNew[s.ParentSpanID]; ok && s.SpanID != n {
			s.ParentSpanID = n
		}
	}
}
