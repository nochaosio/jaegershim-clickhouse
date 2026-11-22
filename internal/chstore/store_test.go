package chstore

import (
	"strings"
	"testing"
)

func TestTagConditionJaegerCompatible(t *testing.T) {
	cases := []struct {
		key, value string
		extended   bool
		wantSQL    string
		wantArgs   int
	}{
		{"error", "true", false, "= 'ERROR'", 0},
		{"error", "false", false, "!= 'ERROR'", 0},
		{"span.kind", "client", false, "lower(?)", 1},
		{"service.name", "x", false, "ServiceName = ?", 1},
		// sem extended: tags sintéticas viram busca normal em atributos (igual ao Jaeger 2.x: não casa)
		{"otel.status_code", "ERROR", false, "mapContains(SpanAttributes", 9},
		{"otel.scope.name", "lib", false, "mapContains(SpanAttributes", 9},
		// com extended: viram colunas
		{"otel.status_code", "ERROR", true, "= upper(?)", 1},
		{"otel.status_description", "boom", true, "StatusMessage = ?", 1},
		{"otel.scope.version", "1.0", true, "ScopeVersion = ?", 1},
		{"http.route", "/x", false, "Events.Attributes", 9},
	}
	for _, c := range cases {
		sql, args := tagCondition(c.key, c.value, c.extended)
		if !strings.Contains(sql, c.wantSQL) || len(args) != c.wantArgs {
			t.Errorf("tagCondition(%q,%q,%v) = %q (%d args); want contains %q (%d args)", c.key, c.value, c.extended, sql, len(args), c.wantSQL, c.wantArgs)
		}
	}
}

func TestQuoteIdent(t *testing.T) {
	if got := quoteIdent("a`b"); got != "`a``b`" {
		t.Errorf("quoteIdent = %s", got)
	}
}
