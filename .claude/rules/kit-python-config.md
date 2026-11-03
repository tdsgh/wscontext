<!-- kit:python/rules/config v1 -->
# Configuration and secrets

- All configuration flows through `.wscontext/config.toml` via `wscontext.config.load_config()`.
  Never `os.environ` or `os.getenv` in application code. *(non-negotiable)*
- A new setting is a field on a config model, with its default in
  `.wscontext/config.toml`.
- Secrets never go in code or committed files. Local development reads them from
  gitignored env files. *(non-negotiable)*
- Secret-bearing config fields are `SecretStr`.
- A connector or client takes its config model as an argument and is a context
  manager that closes its resources on exit.
- An import reads no config and opens no connection. Build those in a factory or
  at the entry point.
