# Relatório do laboratório — ClickHouse Observability (pt-BR)

Stack de observabilidade **OpenTelemetry → ClickHouse**: logs, métricas e traces num único banco,
consultáveis por SQL, **PromQL** (métricas) e pela **API do Jaeger** (traces), com HyperDX, Grafana,
Jaeger UI e um servidor MCP.

> Ambiente de laboratório (Docker Compose, nó único). Versões verificadas em 2026-11-22.
> Veredito de produção no fim deste documento.

```
                         ┌──────────────────────────── ClickHouse 26.10 ───────────────────────────┐
apps / SDKs ──OTLP──▶ OTel Collector ──exporter clickhouse──▶ otel_logs, otel_traces, otel_metrics_* │
 (telemetrygen)     Contrib 0.165 ──prometheus_remote_write──▶ prometheus.metrics (TimeSeries)        │
                         └──────┬──────────────┬───────────────┬──────────────┬───────────────────────┘
                                │SQL           │PromQL API     │SQL           │SQL
                        HyperDX/ClickStack   Grafana 13.2   ClickHouse      mcp-clickhouse
                        (UI 8180)            (3000)         Jaeger (16686)  (8765, IA/Claude)
                                             plugin CH +    API Jaeger legacy+v3
                                             Prometheus +   + Jaeger UI 2.23
                                             Jaeger
```

## Sumário
1. [Componentes e versões](#1-componentes-e-versões)
2. [Como subir](#2-como-subir)
3. [Endpoints](#3-endpoints)
4. [Ingestão (OTel Collector)](#4-ingestão-otel-collector)
5. [PromQL no ClickHouse](#5-promql-no-clickhouse)
6. [Dashboards Grafana (PromQL)](#6-dashboards-grafana-promql)
7. [jaegershim (traces com a API do Jaeger)](#7-jaegershim)
8. [MCP do ClickHouse](#8-mcp-do-clickhouse)
9. [Segurança aplicada](#9-segurança-aplicada)
10. [Testes executados e resultados](#10-testes-executados-e-resultados)
11. [Limitações conhecidas](#11-limitações-conhecidas)
12. [Dá para usar em produção?](#12-dá-para-usar-em-produção)
13. [Estrutura do repositório](#13-estrutura-do-repositório)

---

## 1. Componentes e versões

| Componente | Imagem / versão | Papel |
|---|---|---|
| ClickHouse | `clickhouse/clickhouse-server:26.10.9.12` | armazenamento de logs/métricas/traces; API PromQL |
| OTel Collector Contrib | `otel/opentelemetry-collector-contrib:0.165.0` | recebe OTLP, grava no ClickHouse |
| HyperDX (ClickStack) | `clickhouse/clickstack:2.46.0` + `mongo:5.0.32` | UI oficial de observabilidade da ClickHouse |
| Grafana | `grafana/grafana:13.2.6` + plugin `grafana-clickhouse-datasource@4.23.5` | dashboards, Explore |
| **jaegershim** | `ghcr.io/nochaosio/jaegershim-clickhouse` (construído deste repo, Go 1.27.4 + clickhouse-go v2.51.0) | API do Jaeger + Jaeger UI 2.23 sobre o ClickHouse |
| mcp-clickhouse | `ghcr.io/clickhouse/mcp-clickhouse:0.7.0` | servidor MCP (somente leitura) para IA |
| telemetrygen | `telemetrygen:v0.165.0` (3 serviços) | gera logs, métricas e traces contínuos (`demo-app`) |

Todas as versões ficam em `examples/stack/.env` (copie de `.env.example`).

## 2. Como subir

```bash
cd examples/stack && cp .env.example .env
docker compose up -d                       # stack principal
docker compose ps                          # tudo "Up"/"healthy"
```

Stacks de teste (opcionais, usadas no de-para):
```bash
# Prometheus de referência + avalanche (teste de PromQL)
docker compose -f docker-compose.yml -f compat/docker-compose.promql-test.yml up -d
# Jaeger 2.20 (API legacy) e 2.23 (API v3) recebendo os mesmos traces (teste do jaegershim)
docker compose -f docker-compose.yml -f compat/docker-compose.jaeger-test.yml up -d
```
Para voltar ao normal: remova os serviços de teste e `docker compose up -d --force-recreate otel-collector`.

## 3. Endpoints

| Serviço | URL | Credenciais |
|---|---|---|
| HyperDX (UI ClickStack) | http://localhost:8180 | criar conta no 1º acesso |
| Grafana | http://localhost:3000 | `admin` / `$GRAFANA_ADMIN_PASSWORD` |
| **jaegershim** (Jaeger UI + API) | http://localhost:16686 | — |
| ClickHouse Play (SQL) | http://localhost:8123/play | `default` / `$CLICKHOUSE_PASSWORD` |
| ClickHouse Dashboard | http://localhost:8123/dashboard | `default` / `$CLICKHOUSE_PASSWORD` |
| ClickHouse nativo | `localhost:9009` (9000 estava ocupada no host) | |
| **API PromQL** | http://localhost:8123/prometheus/api/v1/query, `/query_range`, `/series`, `/labels` … | `default` / `$CLICKHOUSE_PASSWORD` |
| OTLP gRPC / HTTP | `localhost:4317` / http://localhost:4318 | — |
| Collector health / métricas | http://localhost:13133 / http://localhost:8888/metrics | — |
| MCP ClickHouse | http://localhost:8765/mcp (health em `/health`) | `Authorization: Bearer $CLICKHOUSE_MCP_AUTH_TOKEN` |

Portas fora do padrão por conflito no host: HyperDX 8180 (8080/8000 ocupadas), ClickHouse nativo 9009.

**Datasources do Grafana (provisionados):** `ClickHouse` (plugin oficial, modo OTel para logs/traces),
`ClickHouse PromQL` (tipo Prometheus apontando para o ClickHouse) e `jaegershim` (tipo Jaeger
apontando para o jaegershim).

## 4. Ingestão (OTel Collector)

`otel-collector/config.yaml`:
- **Receivers:** `otlp` (gRPC 4317 / HTTP 4318) e `prometheus` (scrape do próprio collector `:8888` e do ClickHouse `:9363`, para ter métricas reais).
- **Processors:** `memory_limiter`, `resource_detection`, `batch`.
- **Exporters:**
  - `clickhouse` → tabelas `otel_logs`, `otel_traces` (+ `otel_traces_trace_id_ts`), `otel_metrics_{gauge,sum,histogram,summary,exponential_histogram}`; `create_schema: true`, TTL 72h.
  - `prometheus_remote_write` → `http://clickhouse:8123/prometheus/api/v1/write` (tabela TimeSeries `prometheus.metrics`), com `basicauth`.
- Pipeline de métricas grava nos **dois** formatos (OTel e TimeSeries).

Geração contínua: `telemetrygen-traces`, `-logs` e `-metrics` enviam ~5/s cada como `demo-app`.

## 5. PromQL no ClickHouse

Recurso nativo do ClickHouse (engine `TimeSeries`, *private preview*), habilitado assim:
- `clickhouse/config.d/prometheus.xml`: handler `prometheus_api_v1` em `/prometheus/api/v1` (remote write/read + API de query) fixado em `prometheus.metrics`; endpoint de métricas do próprio ClickHouse em `:9363`.
- `clickhouse/users.xml`: `enable_time_series_table=1`, `async_insert=1`.
- `clickhouse/initdb/01-prometheus.sql`: cria `prometheus.metrics ENGINE = TimeSeries` **com TTL de 15 dias** na tabela de samples (sem isso ela cresce para sempre — achado dos testes).

Consultas:
```bash
curl -u default:$CLICKHOUSE_PASSWORD http://localhost:8123/prometheus/api/v1/query \
  --data-urlencode 'query=sum by (job) (rate(otelcol_receiver_accepted_spans_total[1m]))'
# direto no SQL
SELECT * FROM prometheusQuery(prometheus.metrics, 'sum(rate(demo_requests_total[1m]))', now())
```
Só métricas que chegam via **remote write** são consultáveis por PromQL (as tabelas `otel_metrics_*` não).

## 6. Dashboards Grafana (PromQL)

Pasta **"ClickHouse PromQL"** (provisionada de `grafana/dashboards/`), todos com o datasource `ClickHouse PromQL`:

| Dashboard | URL | Conteúdo |
|---|---|---|
| OTel Collector (PromQL) | /d/promql-otelcol | throughput por receiver/exporter, filas, batch p50/p95/p99, CPU/memória, recusados |
| ClickHouse Server (PromQL) | /d/promql-clickhouse | queries/s por tipo, conexões, inserts/leituras, merges/parts, memória, load |
| Demo App & Targets (PromQL) | /d/promql-demo-app | `rate(demo_requests_total)`, status dos scrape targets, cardinalidade, variável `$job` |

As 57 queries dos painéis foram validadas via API do Grafana (todas retornam dados).
Dica: `sort()`/`sort_desc()` não existem no ClickHouse — ordene pela legenda/tabela do Grafana.

## 7. jaegershim

Serviço Go na raiz deste repositório que **implementa a API do Jaeger Query** lendo `otel_traces` direto
do ClickHouse — o equivalente, para traces, do que o PromQL é para métricas. Sem duplicar dados.
Documentação detalhada: [README](../README.md).

- **API legacy** `/api/*` — usada pelo **datasource Jaeger nativo do Grafana**. Importante: o Jaeger
  2.22+ removeu essa API, então o Grafana 13 **não funciona com o Jaeger atual**; o jaegershim atende.
- **API v3** `/api/v3/*` (services, operations, trace-summaries, traces, traces/{id} em OTLP/JSON) — usada pelo **Jaeger UI 2.23**, que vem embutido no container.
- **Semântica idêntica ao Jaeger:** filtros casam no mesmo span; tags buscam em atributos do span, do resource e de eventos; tags sintéticas `error` e `span.kind`; opcionalmente `otel.status_code`/`otel.scope.*` com `EXTENDED_TAG_SEARCH=true`.
- **Adjusters do Jaeger reproduzidos:** `empty-span-name`, `missing-service-name`, SpanIDUniquifier (spans client/server com mesmo ID, estilo Zipkin), sanitizer de duração zero, warning de pai ausente (`@jaeger@warnings` na v3), link para o próprio pai, trace IDs de 64 bits encurtados na API legacy, `orphanSpanCount` nos resumos, aresta de dependência para spans compartilhados.
- **Usuário ClickHouse dedicado** `jaeger_reader` (`clickhouse/initdb/02-jaeger-reader.sql`): só `SELECT` em `otel_traces*`, `readonly=2`, limites com teto (`MAX`).
- Grafana: datasource **`jaegershim`** (Explore → busca, trace por ID em cascata, grafo de dependências).

## 8. MCP do ClickHouse

`mcp-clickhouse` 0.7.0 em transporte HTTP (`/mcp`), autenticado por token (sem token → 401), somente
leitura (padrão). Registrado no Claude Code deste projeto como `clickhouse`
(`claude mcp add --scope local --transport http clickhouse http://localhost:8765/mcp --header "Authorization: Bearer …"`).

## 9. Segurança aplicada

| Item | Estado |
|---|---|
| Usuário `promql_reader` (somente leitura PromQL) | `readonly=2` + `GRANT CREATE TEMPORARY TABLE` (o motor PromQL usa tabelas temporárias); perfil `promql_reader_limits` com `CONST` |
| Usuário `promql_writer` (remote write) | `INSERT, SELECT` só em `prometheus.*` |
| Usuário `jaeger_reader` (jaegershim) | `SELECT` só em `otel_traces*`, `readonly=2`, `MAX` em tempo/memória/linhas |
| MCP | token Bearer, somente leitura |
| **Pendente** | Grafana/collector ainda usam o usuário `default`; senhas de laboratório no `.env`; sem TLS; sem fila persistente no collector |

Achado dos testes: **sem constraints, qualquer cliente HTTP pode alterar settings do ClickHouse via URL**
(ex.: `?max_execution_time=…`). Use `SETTINGS PROFILE` com `CONST`/`MAX` para todo usuário de consulta.

## 10. Testes executados e resultados

### 10.1 PromQL — ClickHouse x Prometheus v3.16 (mesmos dados via remote write duplo)
Script: `test/promql/promql_compat.py` (99 queries), `test/promql/bench.py` (latência).

| Teste | Resultado |
|---|---|
| Compatibilidade (instant / range) | **91/99 e 89/98 idênticos**; tudo que está implementado deu resultado idêntico |
| Não implementado | `sort`, `sort_desc`, `sort_by_label`, `histogram_fraction`, `double_exponential_smoothing`, `limit_ratio`, `info` |
| API | ok: query, query_range, series, labels, label values, format_query, write; 404: rules, alerts, targets, status/*, exemplars, federate |
| Latência | ~10–15 ms (Prometheus 1–3 ms); 40–250 ms em 100k séries; **1,1 s sob ingestão pesada** |
| Ingestão | ~73k amostras/s usando ~5–6 cores (~12–15k/s por core) num nó Docker Desktop |
| Resiliência | ClickHouse fora 90 s: **zero perda**; collector reiniciado durante a queda: **perda** (fila em memória) |
| Riscos | sem limite de pontos (11k do Prometheus), parâmetro `timeout` quebra a query, settings alteráveis via URL, samples sem TTL por padrão |

### 10.2 jaegershim — de-para com o Jaeger original
Os mesmos traces vão do collector para o ClickHouse, para um Jaeger 2.20 (legacy) e um Jaeger 2.23 (v3).

| Bateria | Script | O que cobre | Resultado |
|---|---|---|---|
| Básica | `test/compat/jaeger_compat.py` | 30 traces multi-serviço; 15 buscas (serviço, operação, durações, error, span.kind, resource, int, eventos, multi-tag, limit…); trace por ID; dependências; erros; **via Grafana** (plugin Jaeger: original x jaegershim), API legacy, API v3, Jaeger UI | **83/83** |
| Casos de borda | `test/compat/jaeger_compat_varied.py` | 24 tipos de trace: span único, todos os kinds, cadeia de 30 níveis, fan-out de 300, **1500 spans**, órfãos, múltiplas raízes, mensageria assíncrona com links entre traces, 3 links, 52 eventos, variações de status, atributos int/double/bool/array/map/bytes/unicode/10 KB/chave com espaço, colisão span x resource, 2 instâncias, sem `service.name`, múltiplos escopos, tracestate, duração 0 e 1 h, clock skew, span ID compartilhado (Zipkin), trace ID de 64 bits, nomes unicode, nome vazio, trace antigo | **184/184** |
| Unitários Go | `go test ./...` (também no `docker build`) | parâmetros, conversões, adjusters, contratos HTTP | ok |
| Navegador real | Playwright/Chromium headless | busca, cascata e dependências renderizam no Jaeger UI, sem erros JS | ok |

Achados durante os testes (todos corrigidos para ficar idêntico ao Jaeger): tags sintéticas não
pesquisáveis no Jaeger, mensagens de erro, adjusters (nomes vazios, uniquifier, duração zero, órfãos,
link ao pai, trace ID curto, orphanSpanCount). **Bug encontrado no Jaeger 2.20:** o warning de pai
ausente se acumula a cada request (6, 7, 8…) porque o adjuster altera o trace guardado em memória; o
Jaeger 2.23 e o jaegershim devolvem uma vez só.

Latência (Jaeger em memória x ClickHouse): ~1 ms x ~10 ms por busca — a referência guarda tudo em RAM,
não é comparável a um Jaeger de produção com Elasticsearch/Cassandra.

## 11. Limitações conhecidas

- **Tipos de atributos de traces:** o exporter do ClickHouse grava `Map(String,String)`; números/booleanos/arrays aparecem como string (o Jaeger mostra `int64`/`bool`). Valores e buscas são idênticos. Nos testes: 1.806 atributos com tipo não preservado.
- **PromQL** é *private preview* (pode mudar de forma incompatível), sem regras/alertas no estilo Prometheus, sem histogramas nativos, 7 funções ausentes, custo de CPU alto na ingestão.
- **jaegershim:** sem archive, deep-dependencies e Monitor (SPM); dependências calculadas na hora (JOIN), caro em janelas longas com muito volume; `flags`/`schemaUrl` não são gravados pelo exporter.
- **Infra:** nó único, sem réplicas/backup, sem TLS, credenciais de laboratório, fila do collector em memória.

## 12. Dá para usar em produção?

**Depende da camada:**

| Camada | Veredito |
|---|---|
| **OTLP → ClickHouse** (collector, tabelas `otel_*`, HyperDX, plugin ClickHouse do Grafana, MCP) | **Sim**, com o hardening abaixo. É o caminho oficial da ClickHouse (ClickStack) e não depende de nada experimental. |
| **jaegershim** (este projeto) | **Pronto para piloto / uso interno, ainda não para produção crítica.** Compatibilidade comprovada (267/267 cenários idênticos ao Jaeger), segurança de leitura correta e baixa latência — mas é código novo (v0.1.0), sem histórico em produção, sem testes de carga e concorrência em volume real, sem métricas/tracing próprios, sem HA e com dependências calculadas na hora. |
| **PromQL no ClickHouse** | **Não para produção crítica.** *Private preview*, sem alertas, funções ausentes, ingestão cara e defaults perigosos. Ok para dashboards não críticos; mantenha Prometheus/Mimir/VictoriaMetrics para alertas. |

**Para levar o jaegershim a produção, faltaria:**
1. Teste de carga com volume real (milhões de spans/dia, buscas concorrentes) e ajuste de índices/projeções em `otel_traces` (ex.: projeção por `TraceId`, materialized view para dependências).
2. Observabilidade do próprio serviço (métricas Prometheus, tracing), limites de concorrência e cache curto para `services`/`operations`.
3. 2+ réplicas atrás de load balancer, TLS, autenticação na frente (o Jaeger UI não tem login).
4. Pipeline de CI com os testes de de-para rodando contra novas versões do Jaeger UI, Grafana e do exporter.

**Hardening geral da stack (antes de qualquer produção):** usuários por função (Grafana e collector
fora do `default`), senhas fortes/secret manager, TLS, fila persistente no collector
(`file_storage`), ClickHouse replicado com backup, TTLs revisados e limites de consulta com `CONST`/`MAX`.

## 13. Estrutura do repositório

```
main.go, internal/        serviço Go (api: handlers, modelos, adjusters; chstore: queries no ClickHouse)
Dockerfile                imagem multi-arch com os assets oficiais do Jaeger UI
examples/stack/           laboratório completo: ClickHouse, OTel Collector, Grafana, HyperDX, MCP
examples/stack/compat/    overlays de referência (Jaeger / Prometheus) usados nas suítes
test/compat/              suítes de comparação Jaeger x jaegershim e geradores de traces
test/promql/              comparação ClickHouse PromQL x Prometheus (estudo do laboratório)
docs/                     compatibilidade, roadmap, este relatório
```
