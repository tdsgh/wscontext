<!-- kit:core/rules/abstractions v1 -->
# Abstractions

- Add a function, class, dataclass, type alias or module only if its name and
  signature are simpler to read than its body, or it is a contract — raised and
  caught, registered, or imported by another module. Otherwise inline it.
- Deduplicate only code that encodes one decision. Leave look-alike code as is.
- Put shared logic in an existing place on every path before adding a new one.
- Don't hide I/O or heavy work behind a helper called in a loop.
- Write code that reads like the surrounding code: match its comment density,
  naming and idiom.
