// jaegershim: implementa a API do Jaeger Query (legacy e v3) lendo
// traces OpenTelemetry direto do ClickHouse (schema do exporter `clickhouse` do
// OpenTelemetry Collector). Permite usar o Jaeger UI e o datasource Jaeger do
// Grafana sobre dados que já estão no ClickHouse, sem duplicar o armazenamento.
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/nochaosio/jaegershim-clickhouse/internal/api"
	"github.com/nochaosio/jaegershim-clickhouse/internal/chstore"
)

var version = "0.1.0"

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envDur(k string, def time.Duration) time.Duration {
	if v := os.Getenv(k); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}

func envInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	cfg := chstore.Config{
		Addr:            env("CLICKHOUSE_ADDR", "localhost:9000"),
		User:            env("CLICKHOUSE_USER", "default"),
		Password:        os.Getenv("CLICKHOUSE_PASSWORD"),
		Database:        env("CLICKHOUSE_DATABASE", "default"),
		TracesTable:     env("TRACES_TABLE", "otel_traces"),
		TraceIDTSTable:  env("TRACES_ID_TS_TABLE", "otel_traces_trace_id_ts"),
		DialTimeout:     envDur("CLICKHOUSE_DIAL_TIMEOUT", 10*time.Second),
		QueryTimeout:    envDur("QUERY_TIMEOUT", 25*time.Second), // < max_execution_time do profile (30s)
		MaxOpenConns:    envInt("CLICKHOUSE_MAX_OPEN_CONNS", 10),
		ServiceLookback: envDur("SERVICES_LOOKBACK", 7*24*time.Hour),
		// true habilita busca por otel.status_code/otel.status_description/otel.scope.* (o Jaeger não permite)
		ExtendedTagSearch: os.Getenv("EXTENDED_TAG_SEARCH") == "true",
	}
	listen := env("LISTEN_ADDR", ":16686")

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	var store *chstore.Store
	var err error
	for i := 0; ; i++ { // espera o ClickHouse subir
		store, err = chstore.New(ctx, cfg)
		if err == nil {
			break
		}
		if i >= 30 || ctx.Err() != nil {
			log.Error("cannot connect to clickhouse", "err", err)
			os.Exit(1)
		}
		log.Warn("clickhouse not ready, retrying", "err", err)
		time.Sleep(2 * time.Second)
	}
	defer store.Close()

	srv := &http.Server{
		Addr:              listen,
		Handler:           api.New(store, os.Getenv("UI_DIR"), version, log),
		ReadHeaderTimeout: 10 * time.Second,
		WriteTimeout:      cfg.QueryTimeout + 10*time.Second,
	}
	go func() {
		<-ctx.Done()
		sctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(sctx)
	}()
	log.Info("jaegershim started", "listen", listen, "clickhouse", cfg.Addr,
		"table", cfg.Database+"."+cfg.TracesTable, "ui", os.Getenv("UI_DIR") != "", "version", version)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Error("server error", "err", err)
		os.Exit(1)
	}
}
