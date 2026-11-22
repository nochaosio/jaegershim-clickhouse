# Contributing

1. `go vet ./... && go test -race ./...` must pass; code must be `gofmt`-clean.
2. Behaviour visible through the Jaeger APIs must match real Jaeger. If you change it, run the
   compatibility suites (see README → Compatibility) and include the result in the PR.
3. Keep PRs focused; describe the user-visible change in `CHANGELOG.md`.
