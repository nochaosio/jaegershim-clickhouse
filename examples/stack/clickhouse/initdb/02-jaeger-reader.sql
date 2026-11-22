-- Usuário somente leitura do clickhouse-jaeger.
-- readonly=2: bloqueia escrita/DDL mas permite ajustar settings (o driver envia
-- max_execution_time derivado do timeout); MAX impede subir os limites.
CREATE SETTINGS PROFILE IF NOT EXISTS jaeger_reader_limits SETTINGS readonly = 2 CONST, max_execution_time = 30 MAX 30, max_memory_usage = 4000000000 MAX 4000000000, max_result_rows = 2000000 MAX 2000000;
CREATE USER IF NOT EXISTS jaeger_reader IDENTIFIED BY 'jaeger_reader' SETTINGS PROFILE 'jaeger_reader_limits';
GRANT SELECT ON default.otel_traces TO jaeger_reader;
GRANT SELECT ON default.otel_traces_trace_id_ts TO jaeger_reader;
