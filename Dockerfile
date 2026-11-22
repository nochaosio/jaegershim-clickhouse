# syntax=docker/dockerfile:1
# ---- build (cross-compile: roda na arquitetura do builder, gera binário para TARGETARCH) ----
FROM --platform=$BUILDPLATFORM golang:1.27.4-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY main.go ./
COPY internal ./internal
ARG TARGETOS TARGETARCH VERSION=dev
RUN go vet ./... && go test ./... && \
    CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH \
    go build -trimpath -ldflags="-s -w -X main.version=${VERSION}" -o /out/clickhouse-jaeger .

# ---- Jaeger UI (assets oficiais, Apache-2.0) ----
FROM --platform=$BUILDPLATFORM alpine:3.22 AS ui
ARG JAEGER_UI_VERSION=v2.23.0
RUN apk add --no-cache curl tar && mkdir -p /ui && \
    curl -fsSL -o /tmp/assets.tar.gz https://github.com/jaegertracing/jaeger-ui/releases/download/${JAEGER_UI_VERSION}/assets.tar.gz && \
    tar -xzf /tmp/assets.tar.gz -C /tmp && cp -r /tmp/packages/jaeger-ui/build/. /ui/

# ---- runtime ----
FROM gcr.io/distroless/static-debian12:nonroot
LABEL org.opencontainers.image.source="https://github.com/nochaosio/clickhouse-jaeger" \
      org.opencontainers.image.description="Jaeger Query API (legacy + v3) and Jaeger UI over OpenTelemetry traces stored in ClickHouse" \
      org.opencontainers.image.licenses="Apache-2.0"
COPY --from=build /out/clickhouse-jaeger /clickhouse-jaeger
COPY --from=ui /ui /ui
ENV UI_DIR=/ui LISTEN_ADDR=:16686
EXPOSE 16686
USER nonroot
ENTRYPOINT ["/clickhouse-jaeger"]
