SET enable_time_series_table = 1;
CREATE DATABASE IF NOT EXISTS prometheus;
-- Retenção: sem o TTL abaixo a tabela de samples cresce para sempre (só recentsamples expira, em 4 dias)
CREATE TABLE IF NOT EXISTS prometheus.metrics ENGINE = TimeSeries
    SAMPLES INNER ENGINE = MergeTree ORDER BY (id, timestamp) TTL toDateTime(timestamp) + INTERVAL 15 DAY;
