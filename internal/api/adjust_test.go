package api

import (
	"strings"
	"testing"
	"time"

	"github.com/nochaosio/clickhouse-jaeger/internal/chstore"
)

func TestAdjustNamesDurationOrphan(t *testing.T) {
	t0 := time.Unix(1_700_000_000, 558_781_000).UTC()
	in := []chstore.Span{
		{TraceID: tid, SpanID: "a", Name: "", ServiceName: "", Timestamp: t0, Duration: 0},
		{TraceID: tid, SpanID: "b", ParentSpanID: "missing", Name: "x", ServiceName: "s", Timestamp: t0, Duration: time.Millisecond},
	}
	out := adjustTrace(in)
	if out[0].Name != emptySpanName || out[0].ServiceName != missingServiceName {
		t.Errorf("names: %+v", out[0])
	}
	if out[0].Duration != time.Nanosecond || len(out[0].Warnings) != 1 ||
		!strings.Contains(out[0].Warnings[0], "Original end timestamp: 2023-11-14 22:13:20.558781 +0000 UTC, adjusted to: 2023-11-14 22:13:20.558781001 +0000 UTC") {
		t.Errorf("duration sanitizer: %v %v", out[0].Duration, out[0].Warnings)
	}
	if len(out[1].Warnings) != 1 || out[1].Warnings[0] != "parent span ID=missing is not in the trace; skipping clock skew adjustment" {
		t.Errorf("orphan warning: %v", out[1].Warnings)
	}
	if in[0].Name != "" {
		t.Error("adjustTrace must not mutate the input")
	}
}

func TestUniquifySharedSpanID(t *testing.T) {
	spans := []chstore.Span{
		{SpanID: "aaaaaaaaaaaaaaaa", Kind: "client", ServiceName: "c"},
		{SpanID: "aaaaaaaaaaaaaaaa", Kind: "server", ServiceName: "s"},
		{SpanID: "bbbbbbbbbbbbbbbb", ParentSpanID: "aaaaaaaaaaaaaaaa", Kind: "internal", ServiceName: "s"},
	}
	uniquifySpanIDs(spans)
	if spans[1].SpanID != "0000000000000001" || spans[1].ParentSpanID != "aaaaaaaaaaaaaaaa" {
		t.Errorf("server span: %+v", spans[1])
	}
	if spans[2].ParentSpanID != "0000000000000001" {
		t.Errorf("child of shared span must move to the server span: %+v", spans[2])
	}
	if spans[0].SpanID != "aaaaaaaaaaaaaaaa" {
		t.Errorf("client span must keep its ID: %+v", spans[0])
	}
}

func TestLegacyParentLinkAndShortTraceID(t *testing.T) {
	short := "0000000000000000af7651916cd43dd8"
	spans := []chstore.Span{{TraceID: short, SpanID: "c", ParentSpanID: "p", ServiceName: "s",
		Links: []chstore.Link{{TraceID: "11111111111111111111111111111111", SpanID: "x"}, {TraceID: short, SpanID: "p"}}}}
	jt := toLegacyTrace(short, spans)
	if jt.TraceID != "af7651916cd43dd8" || jt.Spans[0].TraceID != "af7651916cd43dd8" {
		t.Errorf("short trace id: %s / %s", jt.TraceID, jt.Spans[0].TraceID)
	}
	refs := jt.Spans[0].References
	if len(refs) != 2 || refs[0].RefType != "FOLLOWS_FROM" || refs[0].SpanID != "p" || refs[1].SpanID != "x" {
		t.Errorf("parent link refs: %+v", refs)
	}
}

func TestSummaryOrphans(t *testing.T) {
	t0 := time.Unix(1_700_000_000, 0)
	ts := summarize(tid, []chstore.Span{
		{SpanID: "a", ParentSpanID: "zz", ServiceName: "s", Timestamp: t0, Duration: time.Millisecond},
		{SpanID: "b", ParentSpanID: "zz", ServiceName: "s", Timestamp: t0, Duration: time.Millisecond},
	})
	if ts.RootServiceName != "" || ts.RootOperationName != "" || ts.OrphanSpanCount != 2 {
		t.Errorf("orphan summary: %+v", ts)
	}
}
