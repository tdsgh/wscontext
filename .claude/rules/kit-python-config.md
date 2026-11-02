<!-- kit:python/rules/config v1 -->
# Configuration and secrets

- All configuration flows through `.localAIharness/config.toml` via `TODO(kit): the function the harness calls to load settings`.
  Never `os.environ` or `os.getenv` in application code. *(non-negotiable)*
- A new setting is a field on a config model, with its default in
  `.localAIharness/config.toml`.
- Secrets never go in code or committed files. Local development reads them from
  gitignored env files. *(non-negotiable)*
- Secret-bearing config fields are `SecretStr`.
- A connector or client takes its config model as an argument and is a context
  manager that closes its resources on exit.
- An import reads no config and opens no connection. Build those in a factory or
  at the entry point.
