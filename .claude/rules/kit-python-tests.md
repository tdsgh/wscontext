<!-- kit:python/rules/tests v1 -->
# Python tests

- Tests run offline: no database, network or external service. Mock the client
  or engine at its boundary, or record what a fake receives and assert on that.
- Where the protected rule is visible in the output — the compiled SQL, the
  payload, the bound parameters — assert on the output, not on a mock's call
  arguments.
- A test asserting an absence is load-bearing. Reversing one is a deliberate
  deletion named in the report. Never weaken it to pass.
- Never edit a test to make a gate pass.
- Adding a test dependency, such as a pytest plugin, is a decision. Propose it.
- Run each project's suite in its own pytest invocation. One invocation over two
  project trees raises `ImportPathMismatchError`.
- Report the test count and every new warning. A run that adds a warning is not
  green.
