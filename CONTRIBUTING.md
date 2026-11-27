# Contributing

Thanks for helping. Bug reports, compatibility gaps and PRs are all welcome.

## Before opening a PR

1. `gofmt -l .` prints nothing, and `go vet ./... && go test -race ./...` passes.
2. Anything visible through the Jaeger APIs must match real Jaeger. If you change that behaviour, run the
   compatibility suites and paste the summary into the PR:

   ```bash
   cd examples/stack && cp .env.example .env
   docker compose -f docker-compose.yml -f compat/docker-compose.jaeger-test.yml up -d
   cd ../.. && python3 test/compat/jaeger_compat.py && python3 test/compat/jaeger_compat_varied.py
   ```

3. Keep each PR focused, and add any user-visible change under `Unreleased` in `CHANGELOG.md`.

## Reporting a compatibility gap

Open an issue that includes the request, jaegershim's response, what real Jaeger returns for the same
traces, and the Jaeger, Grafana and exporter versions you use.

Security issues go through [SECURITY.md](SECURITY.md), not public issues.
