// Package chstore lê spans OpenTelemetry gravados pelo exporter `clickhouse`
// do OpenTelemetry Collector (tabelas otel_traces e otel_traces_trace_id_ts).
package chstore

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
)

// Config da conexão e das tabelas.
type Config struct {
	Addr            string
	User            string
	Password        string
	Database        string
	TracesTable     string
	TraceIDTSTable  string
	DialTimeout     time.Duration
	QueryTimeout    time.Duration
	MaxOpenConns    int
	ServiceLookback time.Duration // janela usada para listar serviços/operações
	// ExtendedTagSearch habilita busca pelas tags sintéticas que o Jaeger 2.x exibe
	// mas NÃO indexa para busca (otel.status_code, otel.status_description, otel.scope.*).
	// Desligado = comportamento idêntico ao Jaeger.
	ExtendedTagSearch bool
}

// Event é um evento OTel de um span (vira "log" no modelo Jaeger legacy).
type Event struct {
	Time       time.Time
	Name       string
	Attributes map[string]string
}

// Link é um link OTel (vira referência FOLLOWS_FROM no modelo Jaeger legacy).
type Link struct {
	TraceID    string
	SpanID     string
	TraceState string
	Attributes map[string]string
}

// Span é uma linha de otel_traces.
type Span struct {
	Timestamp          time.Time
	TraceID            string
	SpanID             string
	ParentSpanID       string
	TraceState         string
	Name               string
	Kind               string // normalizado: server, client, producer, consumer, internal, unspecified
	ServiceName        string
	ResourceAttributes map[string]string
	ScopeName          string
	ScopeVersion       string
	Attributes         map[string]string
	Duration           time.Duration
	StatusCode         string // normalizado: OK, ERROR, UNSET
	StatusMessage      string
	Events             []Event
	Links              []Link
	Warnings           []string // preenchido pelos adjusters (camada api)
}

// IsError indica status de erro.
func (s *Span) IsError() bool { return s.StatusCode == "ERROR" }

// Operation = (nome, kind).
type Operation struct {
	Name string
	Kind string
}

// Dependency = arestas pai -> filho entre serviços.
type Dependency struct {
	Parent    string
	Child     string
	CallCount uint64
}

// TraceQuery são os filtros de busca (semântica Jaeger: todos os filtros
// precisam casar no MESMO span; o resultado são traces inteiros).
type TraceQuery struct {
	Service     string
	Operation   string
	Tags        map[string]string
	StartMin    time.Time
	StartMax    time.Time
	DurationMin time.Duration
	DurationMax time.Duration
	Limit       int
}

// Store acessa o ClickHouse.
type Store struct {
	conn driver.Conn
	cfg  Config
}

// New abre a conexão (protocolo nativo).
func New(ctx context.Context, cfg Config) (*Store, error) {
	conn, err := clickhouse.Open(&clickhouse.Options{
		Addr:            []string{cfg.Addr},
		Auth:            clickhouse.Auth{Database: cfg.Database, Username: cfg.User, Password: cfg.Password},
		DialTimeout:     cfg.DialTimeout,
		MaxOpenConns:    cfg.MaxOpenConns,
		ConnMaxLifetime: time.Hour,
		// Sem Settings no cliente: o usuário recomendado é readonly=1 com limites
		// definidos no servidor (SETTINGS PROFILE ... CONST). O timeout é aplicado via context.
		ClientInfo: clickhouse.ClientInfo{Products: []struct{ Name, Version string }{{Name: "clickhouse-jaeger", Version: "0.1.0"}}},
	})
	if err != nil {
		return nil, err
	}
	if err := conn.Ping(ctx); err != nil {
		return nil, fmt.Errorf("ping clickhouse: %w", err)
	}
	return &Store{conn: conn, cfg: cfg}, nil
}

// Ping verifica a conexão.
func (s *Store) Ping(ctx context.Context) error { return s.conn.Ping(ctx) }

// Close fecha a conexão.
func (s *Store) Close() error { return s.conn.Close() }

func (s *Store) traces() string {
	return quoteIdent(s.cfg.Database) + "." + quoteIdent(s.cfg.TracesTable)
}
func (s *Store) traceTS() string {
	return quoteIdent(s.cfg.Database) + "." + quoteIdent(s.cfg.TraceIDTSTable)
}

func quoteIdent(s string) string { return "`" + strings.ReplaceAll(s, "`", "``") + "`" }

// Nomes que o Jaeger usa para serviço/operação vazios.
const (
	MissingServiceName = "missing-service-name"
	EmptySpanName      = "empty-span-name"
)

// Condições que tratam os nomes substitutos do Jaeger (2 args cada: valor, valor).
const (
	serviceCond   = "(ServiceName = ? OR (? = '" + MissingServiceName + "' AND ServiceName = ''))"
	operationCond = "(SpanName = ? OR (? = '" + EmptySpanName + "' AND SpanName = ''))"
)

// Expressões SQL que normalizam variações de versões do exporter
// ("Server" vs "SPAN_KIND_SERVER", "Error" vs "STATUS_CODE_ERROR").
const (
	kindExpr   = "lower(replaceOne(toString(SpanKind), 'SPAN_KIND_', ''))"
	statusExpr = "upper(replaceOne(toString(StatusCode), 'STATUS_CODE_', ''))"
)

func (s *Store) ctx(parent context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(parent, s.cfg.QueryTimeout)
}

// Services lista os serviços vistos na janela ServiceLookback.
func (s *Store) Services(ctx context.Context) ([]string, error) {
	ctx, cancel := s.ctx(ctx)
	defer cancel()
	q := fmt.Sprintf(`SELECT DISTINCT if(ServiceName = '', '%s', ServiceName) AS svc FROM %s WHERE Timestamp >= now() - toIntervalSecond(?) ORDER BY svc`, MissingServiceName, s.traces())
	rows, err := s.conn.Query(ctx, q, int64(s.cfg.ServiceLookback.Seconds()))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []string{}
	for rows.Next() {
		var v string
		if err := rows.Scan(&v); err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// Operations lista (nome, kind) de um serviço; kind vazio = todos.
func (s *Store) Operations(ctx context.Context, service, kind string) ([]Operation, error) {
	ctx, cancel := s.ctx(ctx)
	defer cancel()
	q := fmt.Sprintf(`SELECT if(SpanName = '', '%s', SpanName) AS op, %s AS kind FROM %s
		WHERE %s AND Timestamp >= now() - toIntervalSecond(?) AND (? = '' OR %s = ?)
		GROUP BY op, kind ORDER BY op, kind`, EmptySpanName, kindExpr, s.traces(), serviceCond, kindExpr)
	rows, err := s.conn.Query(ctx, q, service, service, int64(s.cfg.ServiceLookback.Seconds()), kind, kind)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Operation{}
	for rows.Next() {
		var o Operation
		if err := rows.Scan(&o.Name, &o.Kind); err != nil {
			return nil, err
		}
		out = append(out, o)
	}
	return out, rows.Err()
}

// tagCondition traduz uma tag de busca Jaeger para SQL (casa no span,
// no resource ou em atributos de eventos, como o Jaeger faz com process tags e logs).
func tagCondition(key, value string, extended bool) (string, []any) {
	switch key {
	case "error":
		if strings.EqualFold(value, "true") {
			return statusExpr + " = 'ERROR'", nil
		}
		return statusExpr + " != 'ERROR'", nil
	case "span.kind":
		return kindExpr + " = lower(?)", []any{value}
	case "service.name":
		return "ServiceName = ?", []any{value}
	}
	if extended {
		switch key {
		case "otel.status_code":
			return statusExpr + " = upper(?)", []any{value}
		case "otel.status_description":
			return "StatusMessage = ?", []any{value}
		case "otel.scope.name":
			return "ScopeName = ?", []any{value}
		case "otel.scope.version":
			return "ScopeVersion = ?", []any{value}
		}
	}
	return `((mapContains(SpanAttributes, ?) AND SpanAttributes[?] = ?)
		OR (mapContains(ResourceAttributes, ?) AND ResourceAttributes[?] = ?)
		OR arrayExists(m -> mapContains(m, ?) AND m[?] = ?, Events.Attributes))`,
		[]any{key, key, value, key, key, value, key, key, value}
}

// FindTraceIDs devolve os IDs dos traces mais recentes que casam com a query.
func (s *Store) FindTraceIDs(ctx context.Context, tq TraceQuery) ([]string, error) {
	ctx, cancel := s.ctx(ctx)
	defer cancel()
	where := []string{"Timestamp >= fromUnixTimestamp64Nano(?)", "Timestamp <= fromUnixTimestamp64Nano(?)"}
	args := []any{tq.StartMin.UnixNano(), tq.StartMax.UnixNano()}
	if tq.Service != "" {
		where = append(where, serviceCond)
		args = append(args, tq.Service, tq.Service)
	}
	if tq.Operation != "" {
		where = append(where, operationCond)
		args = append(args, tq.Operation, tq.Operation)
	}
	if tq.DurationMin > 0 {
		where = append(where, "Duration >= ?")
		args = append(args, uint64(tq.DurationMin.Nanoseconds()))
	}
	if tq.DurationMax > 0 {
		where = append(where, "Duration <= ?")
		args = append(args, uint64(tq.DurationMax.Nanoseconds()))
	}
	keys := make([]string, 0, len(tq.Tags))
	for k := range tq.Tags {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		c, a := tagCondition(k, tq.Tags[k], s.cfg.ExtendedTagSearch)
		where = append(where, c)
		args = append(args, a...)
	}
	limit := tq.Limit
	if limit <= 0 {
		limit = 20
	}
	q := fmt.Sprintf(`SELECT TraceId FROM %s WHERE %s GROUP BY TraceId ORDER BY max(Timestamp) DESC LIMIT %d`,
		s.traces(), strings.Join(where, " AND "), limit)
	rows, err := s.conn.Query(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	ids := []string{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// GetTraces busca todos os spans dos traces informados. A janela de tempo vem
// da tabela otel_traces_trace_id_ts (permite poda de partições em otel_traces).
// Retorna map traceID -> spans ordenados de forma determinística: início, raiz antes dos filhos
// em caso de empate (o Grafana usa o primeiro span para nomear o trace), maior duração, span ID.
func (s *Store) GetTraces(ctx context.Context, ids []string) (map[string][]Span, error) {
	out := map[string][]Span{}
	if len(ids) == 0 {
		return out, nil
	}
	ctx, cancel := s.ctx(ctx)
	defer cancel()
	var tmin, tmax time.Time
	var n uint64
	bq := fmt.Sprintf(`SELECT min(Start), max(End), count() FROM %s WHERE TraceId IN (?)`, s.traceTS())
	if err := s.conn.QueryRow(ctx, bq, ids).Scan(&tmin, &tmax, &n); err != nil {
		return nil, fmt.Errorf("trace bounds: %w", err)
	}
	where := "TraceId IN (?)"
	args := []any{ids}
	if n > 0 {
		// Start/End têm resolução de segundos: folga de 1s em cada lado.
		where += " AND Timestamp >= fromUnixTimestamp64Nano(?) AND Timestamp <= fromUnixTimestamp64Nano(?)"
		args = append(args, tmin.Add(-time.Second).UnixNano(), tmax.Add(time.Second).UnixNano())
	}
	q := fmt.Sprintf(`SELECT Timestamp, TraceId, SpanId, ParentSpanId, TraceState, SpanName, %s,
		ServiceName, ResourceAttributes, ScopeName, ScopeVersion, SpanAttributes, Duration, %s, StatusMessage,
		Events.Timestamp, Events.Name, Events.Attributes, Links.TraceId, Links.SpanId, Links.TraceState, Links.Attributes
		FROM %s WHERE %s ORDER BY TraceId, Timestamp, ParentSpanId != '', Duration DESC, SpanId`, kindExpr, statusExpr, s.traces(), where)
	rows, err := s.conn.Query(ctx, q, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var sp Span
		var dur uint64
		var evTS []time.Time
		var evName []string
		var evAttr []map[string]string
		var lTID, lSID, lTS []string
		var lAttr []map[string]string
		if err := rows.Scan(&sp.Timestamp, &sp.TraceID, &sp.SpanID, &sp.ParentSpanID, &sp.TraceState, &sp.Name, &sp.Kind,
			&sp.ServiceName, &sp.ResourceAttributes, &sp.ScopeName, &sp.ScopeVersion, &sp.Attributes, &dur, &sp.StatusCode,
			&sp.StatusMessage, &evTS, &evName, &evAttr, &lTID, &lSID, &lTS, &lAttr); err != nil {
			return nil, err
		}
		sp.Duration = time.Duration(dur)
		for i := range evName {
			e := Event{Name: evName[i]}
			if i < len(evTS) {
				e.Time = evTS[i]
			}
			if i < len(evAttr) {
				e.Attributes = evAttr[i]
			}
			sp.Events = append(sp.Events, e)
		}
		for i := range lTID {
			l := Link{TraceID: lTID[i]}
			if i < len(lSID) {
				l.SpanID = lSID[i]
			}
			if i < len(lTS) {
				l.TraceState = lTS[i]
			}
			if i < len(lAttr) {
				l.Attributes = lAttr[i]
			}
			sp.Links = append(sp.Links, l)
		}
		out[sp.TraceID] = append(out[sp.TraceID], sp)
	}
	return out, rows.Err()
}

// Dependencies calcula arestas pai->filho (por serviço) na janela [start,end].
// Assim como o Jaeger, inclui chamadas dentro do mesmo serviço.
func (s *Store) Dependencies(ctx context.Context, start, end time.Time) ([]Dependency, error) {
	ctx, cancel := s.ctx(ctx)
	defer cancel()
	svc := "if(ServiceName = '', '" + MissingServiceName + "', ServiceName)"
	win := "Timestamp >= fromUnixTimestamp64Nano(?) AND Timestamp <= fromUnixTimestamp64Nano(?)"
	q := fmt.Sprintf(`SELECT parent, child, count() AS calls FROM (
		SELECT p.svc AS parent, c.svc AS child
		FROM (SELECT TraceId, SpanId, %[2]s AS svc FROM %[1]s WHERE %[3]s) AS p
		INNER JOIN (SELECT TraceId, ParentSpanId, %[2]s AS svc FROM %[1]s WHERE %[3]s AND ParentSpanId != '') AS c
		ON p.TraceId = c.TraceId AND p.SpanId = c.ParentSpanId
		UNION ALL
		-- spans compartilhados estilo Zipkin (o Jaeger torna o server filho do client)
		SELECT cl.svc AS parent, sv.svc AS child
		FROM (SELECT TraceId, SpanId, %[2]s AS svc FROM %[1]s WHERE %[3]s AND %[4]s = 'client') AS cl
		INNER JOIN (SELECT TraceId, SpanId, %[2]s AS svc FROM %[1]s WHERE %[3]s AND %[4]s = 'server') AS sv
		ON cl.TraceId = sv.TraceId AND cl.SpanId = sv.SpanId
	) GROUP BY parent, child ORDER BY parent, child`, s.traces(), svc, win, kindExpr)
	a, b := start.UnixNano(), end.UnixNano()
	rows, err := s.conn.Query(ctx, q, a, b, a, b, a, b, a, b)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Dependency{}
	for rows.Next() {
		var d Dependency
		if err := rows.Scan(&d.Parent, &d.Child, &d.CallCount); err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}
