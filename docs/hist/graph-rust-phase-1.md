# Rust in the code graph — feature description

Requirements for a person deciding whether these are the right ones. Technical
design is a later document, `build-plan-graph-rust-phase-1.md`, kept local and
not in the repository.

## 1. Purpose

The code graph reads Python, TypeScript and JavaScript. Rust files today take
part only in "files that change together". With this task, `ctx code`, `ctx impact` and `ctx why`
on a name answer for Rust code as they do for the other languages.

## 2. What we are building

A Rust part for the code graph. It finds the functions, methods and types in
each Rust file, the files each file uses, and the calls each function makes,
matched by name. It follows Rust's own rules for which file holds which
module, and its own conventions for where tests live. It is added the same way
the three existing languages were, and the rest of the graph does not change,
except where § 7 says otherwise.

## 3. Scope

**In.**

- Definitions, imports, calls and tests for Rust files (§ 4 to § 7).
- Switching Rust on and off like the other languages (§ 8).
- Links from sessions to the Rust functions they changed, through their
  commits, as for the other languages.

**Out.**

- Exact references from the Rust compiler or a language server.
- Crates from outside the repository: the standard library and dependencies
  from the package registry. Their imports stay unresolved, as for packages in
  the other languages.
- Code produced at build time: build scripts, procedural macros, and the
  contents of `include!`.
- Conditional compilation. Every item is read, whatever `#[cfg(...)]` says.
- Other crates in the same repository. A `use` of one stays unresolved, like a
  registry crate.

**When.** After Alpha's sync test and benchmark, so the plugin version does not
change during Alpha's reliability count. Until then, Rust files take part only
in "files that change together".

## 4. Where Rust code is defined

**The operator can see:**

- Functions, methods and types in `ctx code` and in a file's outline:
  - A free function is a `function`.
  - A function inside `impl Foo` or `impl Trait for Foo` is a `method` named
    `Foo.name`. Type arguments are left out: `impl<T> Foo<T>` gives `Foo`.
  - A function inside `trait T`, with or without a body, is a `method` named
    `T.name`.
  - `struct`, `enum`, `union`, `trait` and `type` aliases are a `type`, as
    TypeScript interfaces and enums are.
  - Items inside an inline module `mod x { ... }` are named `x.name`. The
    module itself is not listed as a definition.
  - A macro defined with `macro_rules!` is a `function`.
- A definition starts at its first attribute or doc comment (`#[...]`, `///`)
  and ends at its closing brace. In Rust, doc comments are attributes, so a
  commit that changes only the doc comment counts as a change to that function.
- The signature: the header up to the body, on one line, with type parameters
  and `where` clauses.

**The operator can change:** nothing beyond the other languages' settings.

**Deliberately cannot:**

- Tell apart two methods of the same name on one type that come from different
  traits, such as `fmt` from `Display` and from `Debug`. Both are `Foo.fmt`.

## 5. What a Rust file uses

**The operator can see:**

- In `ctx impact` on a file, the files that use it, and the reverse:
  - `mod foo;` uses the file that holds module `foo`: `foo.rs` or `foo/mod.rs`,
    next to the declaring file or in its own folder, by Rust's rules.
  - `use crate::...`, `use super::...` and `use self::...` lead to the file
    that holds the named module. When the last part names an item rather than
    a module, the file of the module that holds it is used.
  - A grouped `use a::{b, c::{d, e as f}, *}` counts as one use per path.
- The crate root is found from the nearest `Cargo.toml` above the file:
  `src/lib.rs` or `src/main.rs`.

**Deliberately cannot:**

- Follow `#[path = "..."]` attributes or `pub use` re-exports. A use leads to
  the file that declares the module, not to where its items come from.
- Resolve other crates in the same repository (§ 3).

## 6. What Rust code calls

**The operator can see:**

- In `ctx impact` on a name, the functions that call it, matched by name:
  `f()`, `module::f()`, `Foo::new()`, `x.f()` and `f::<T>()` all call `f`
  (`new` for `Foo::new()`).
- Calls inside macro arguments, such as `assert_eq!(f(x), 1)` or
  `vec![make(1)]`. A name followed directly by parentheses counts as a call.
  Most test checks sit inside such macros.
- Calls inside closures belong to the function the closure is in.
- A macro invocation such as `square!(...)` is a call to `square`. Calls to
  standard macros such as `println!` match no definition and change no answer.

**Deliberately cannot:**

- Tell which `new`, `from`, `clone` or `fmt` is meant. Rust uses these names
  very often, so `ctx impact` on them lists many callers. It says how many
  definitions share the name, as for the other languages.

## 7. Tests

**The operator can see:**

- In `ctx impact`, tests listed apart from other callers and importers:
  - Every file under a crate's `tests/` folder is a test file.
  - A function marked `#[test]`, and everything inside a `#[cfg(test)]`
    module, is a test, even when it sits in the same file as the code it
    tests. Most Rust unit tests live there.

**Deliberately cannot:**

- Treat `benches/` or `examples/` as tests.

## 8. Switching Rust on

**The operator can see:**

- Rust in the health check and the statistics, with its file and definition
  counts, as for the other languages.

**The operator can change:**

- Switch Rust on or off in the committed configuration. A newly set-up
  repository has it on. A repository set up earlier adds it to its list.

**Deliberately cannot:**

- Lose the other languages if the Rust grammar fails to install. Rust is then
  reported as unavailable.

## 9. Cross-cutting requirements

- Switching Rust on or off leaves every other language's answers as they were.
- The Rust grammar installs on every platform the plugin supports, with no
  compiler.
- Rust files follow the existing limits: excluded paths, the size cap, and the
  time budget of a refresh.
- After an update that adds Rust, a repository's past commits that changed
  Rust files link their sessions to the Rust functions they changed.

## 10. Feasibility

- **Already proven.** A throwaway test loaded the Rust grammar with the parser
  wscontext already uses. It read every construct in § 4 to § 6. A syntax
  error stayed inside the broken item, and the rest of the file was read. The
  grammar ships for all five supported platforms.
- **New but straightforward.** Splitting grouped `use` paths. Finding a
  module's file from the crate root and the declaring file. Attributes and doc
  comments that come before an item.
- **Known costs.** Macro arguments are not parsed as code. Calls inside them
  are found by the "name followed by parentheses" rule, which also counts
  constructors such as `Some(x)`. Calls outside macros already count those.
  In-file tests (§ 7) need the graph to accept that one file holds both code
  and tests. That is the one change outside the Rust part.
- **Blocked.** Nothing.

## 11. Open questions

None.

## 12. Beyond this task

- Exact references from the Rust compiler or a language server, if matching
  by name gives wrong answers too often.
- Other crates in the same repository, resolved by matching the crate name to
  its folder name.
- Following `pub use` re-exports to where items are defined.

## 13. Notes for implementation planning

The existing language contract fits Rust without change, apart from in-file
tests. The throwaway test confirmed the grammar's shapes: an `impl` names its
type and, if present, its trait. A module declaration and an inline module are
the same kind of item, with or without a body. Attributes and doc comments
come before their item rather than inside it. Macro arguments arrive as a flat
list of tokens.

In-file tests reach beyond the Rust part. Today a test is decided per file,
and `ctx impact` sorts callers by their file. Planning has to decide how a
part marks single definitions as tests, while the other languages keep their
per-file rule unchanged.

Two things to check before the design is fixed. First, whether the graph
accepts two definitions with the same name in one file (§ 4, `Foo.fmt`).
Second, whether switching a language on re-links the past commits that
changed its files (§ 9).

A first sample set can come from the throwaway test.
