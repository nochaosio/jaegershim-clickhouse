package api

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/nochaosio/clickhouse-jaeger/internal/chstore"
)

const (
	defaultLimit    = 100 // mesmo default da API legacy do Jaeger
	defaultLookback = 48 * time.Hour
)

// normalizeTraceID valida e normaliza um trace ID hex (aceita IDs curtos/sem zeros à esquerda,
// como o Jaeger) para 32 caracteres minúsculos.
func normalizeTraceID(id string) (string, error) {
	id = strings.ToLower(strings.TrimSpace(id))
	if id == "" || len(id) > 32 {
		return "", errors.New("invalid length for TraceID")
	}
	if _, err := hex.DecodeString(strings.Repeat("0", len(id)%2) + id); err != nil {
		return "", errors.New("invalid length for TraceID") // mesma mensagem do Jaeger 2.x
	}
	return strings.Repeat("0", 32-len(id)) + id, nil
}

// parseTags aceita os formatos que o Jaeger aceita:
//   - tags={"k":"v",...} (JSON; valores não-string são convertidos)
//   - tag=k:v (repetível)
//   - logfmt (k=v k2="v 2"), usado por alguns clientes
func parseTags(jsonOrLogfmt string, tagParams []string) (map[string]string, error) {
	out := map[string]string{}
	if s := strings.TrimSpace(jsonOrLogfmt); s != "" {
		if strings.HasPrefix(s, "{") {
			var raw map[string]any
			if err := json.Unmarshal([]byte(s), &raw); err != nil {
				return nil, fmt.Errorf("malformed 'tags' parameter: %v", err)
			}
			for k, v := range raw {
				switch t := v.(type) {
				case string:
					out[k] = t
				default:
					b, _ := json.Marshal(t)
					out[k] = string(b)
				}
			}
		} else {
			kv, err := parseLogfmt(s)
			if err != nil {
				return nil, err
			}
			for k, v := range kv {
				out[k] = v
			}
		}
	}
	for _, t := range tagParams {
		k, v, ok := strings.Cut(t, ":")
		if !ok || k == "" {
			return nil, fmt.Errorf("malformed 'tag' parameter, expecting key:value, received: %s", t)
		}
		out[k] = v
	}
	return out, nil
}

func parseLogfmt(s string) (map[string]string, error) {
	out := map[string]string{}
	i := 0
	for i < len(s) {
		for i < len(s) && s[i] == ' ' {
			i++
		}
		if i >= len(s) {
			break
		}
		j := i
		for j < len(s) && s[j] != '=' && s[j] != ' ' {
			j++
		}
		key := s[i:j]
		if j >= len(s) || s[j] != '=' {
			return nil, fmt.Errorf("malformed tags (logfmt): missing '=' after %q", key)
		}
		j++
		var val string
		if j < len(s) && s[j] == '"' {
			k := j + 1
			var b strings.Builder
			for k < len(s) && s[k] != '"' {
				if s[k] == '\\' && k+1 < len(s) {
					k++
				}
				b.WriteByte(s[k])
				k++
			}
			if k >= len(s) {
				return nil, errors.New("malformed tags (logfmt): unterminated quote")
			}
			val = b.String()
			j = k + 1
		} else {
			k := j
			for k < len(s) && s[k] != ' ' {
				k++
			}
			val = s[j:k]
			j = k
		}
		out[key] = val
		i = j
	}
	return out, nil
}

func parseDurationParam(v url.Values, name string) (time.Duration, error) {
	s := v.Get(name)
	if s == "" {
		return 0, nil
	}
	d, err := time.ParseDuration(s)
	if err != nil {
		return 0, fmt.Errorf("unable to parse param '%s': %v", name, err)
	}
	return d, nil
}

func parseMicros(v url.Values, name string) (time.Time, bool, error) {
	s := v.Get(name)
	if s == "" {
		return time.Time{}, false, nil
	}
	n, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return time.Time{}, false, fmt.Errorf("unable to parse param '%s': %v", name, err)
	}
	return time.UnixMicro(n), true, nil
}

// parseLegacyQuery traduz os parâmetros de GET /api/traces (API legacy do Jaeger).
func parseLegacyQuery(v url.Values, now time.Time) (chstore.TraceQuery, []string, error) {
	var q chstore.TraceQuery
	var ids []string
	for _, raw := range v["traceID"] {
		id, err := normalizeTraceID(raw)
		if err != nil {
			return q, nil, err
		}
		ids = append(ids, id)
	}
	q.Service = v.Get("service")
	q.Operation = v.Get("operation")
	if len(ids) == 0 && q.Service == "" {
		return q, nil, errors.New("parameter 'service' is required")
	}
	end, ok, err := parseMicros(v, "end")
	if err != nil {
		return q, nil, err
	}
	if !ok {
		end = now
	}
	start, ok, err := parseMicros(v, "start")
	if err != nil {
		return q, nil, err
	}
	if !ok {
		lb := defaultLookback
		if s := v.Get("lookback"); s != "" && s != "custom" {
			if d, err := time.ParseDuration(s); err == nil {
				lb = d
			}
		}
		start = end.Add(-lb)
	}
	q.StartMin, q.StartMax = start, end
	if q.DurationMin, err = parseDurationParam(v, "minDuration"); err != nil {
		return q, nil, err
	}
	if q.DurationMax, err = parseDurationParam(v, "maxDuration"); err != nil {
		return q, nil, err
	}
	if q.DurationMin > 0 && q.DurationMax > 0 && q.DurationMax < q.DurationMin {
		return q, nil, errors.New("'maxDuration' should be greater than 'minDuration'")
	}
	q.Limit = defaultLimit
	if s := v.Get("limit"); s != "" {
		n, err := strconv.Atoi(s)
		if err != nil || n < 0 {
			return q, nil, fmt.Errorf("unable to parse param 'limit': %s", s)
		}
		if n > 0 {
			q.Limit = n
		}
	}
	if q.Tags, err = parseTags(v.Get("tags"), v["tag"]); err != nil {
		return q, nil, err
	}
	return q, ids, nil
}

// first devolve o primeiro valor não vazio entre os nomes (camelCase e snake_case).
func first(v url.Values, names ...string) string {
	for _, n := range names {
		if s := v.Get(n); s != "" {
			return s
		}
	}
	return ""
}

// parseV3Query traduz os parâmetros query.* da API v3 (trace-summaries e traces).
func parseV3Query(v url.Values) (chstore.TraceQuery, error) {
	var q chstore.TraceQuery
	q.Service = first(v, "query.serviceName", "query.service_name")
	q.Operation = first(v, "query.operationName", "query.operation_name")
	smin, smax := first(v, "query.startTimeMin", "query.start_time_min"), first(v, "query.startTimeMax", "query.start_time_max")
	if smin == "" || smax == "" {
		return q, errors.New("invalid query: min and max start time are required")
	}
	var err error
	if q.StartMin, err = time.Parse(time.RFC3339Nano, smin); err != nil {
		return q, fmt.Errorf("failed to parse query.startTimeMin: %v", err)
	}
	if q.StartMax, err = time.Parse(time.RFC3339Nano, smax); err != nil {
		return q, fmt.Errorf("failed to parse query.startTimeMax: %v", err)
	}
	for _, p := range []struct {
		names []string
		dst   *time.Duration
	}{{[]string{"query.durationMin", "query.duration_min"}, &q.DurationMin}, {[]string{"query.durationMax", "query.duration_max"}, &q.DurationMax}} {
		if s := first(v, p.names...); s != "" {
			if *p.dst, err = time.ParseDuration(s); err != nil {
				return q, fmt.Errorf("failed to parse %s: %v", p.names[0], err)
			}
		}
	}
	q.Limit = defaultLimit
	if s := first(v, "query.searchDepth", "query.search_depth", "query.numTraces", "query.num_traces"); s != "" {
		n, err := strconv.Atoi(s)
		if err != nil || n < 0 {
			return q, fmt.Errorf("failed to parse query.searchDepth: %s", s)
		}
		if n > 0 {
			q.Limit = n
		}
	}
	if q.Tags, err = parseTags(first(v, "query.attributes"), nil); err != nil {
		return q, err
	}
	return q, nil
}
