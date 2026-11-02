<!-- kit:core/rules/precedence v1 -->
# Rule files and precedence

Root `CLAUDE.md` owns the rules that hold everywhere in this repo. A subtree
`CLAUDE.md` adds rules for that subtree and does not repeat root rules.
`.claude/rules/kit-*.md` files are kit-managed rules.

1. When working inside a subtree, load both root `CLAUDE.md` and that subtree's
   `CLAUDE.md`.
2. Subtree rules win over root rules inside that subtree's directory, only for
   what the subtree file explicitly addresses. Silence there means the root rule
   still applies.
3. Rules tagged *(non-negotiable)* — in root `CLAUDE.md` or in a
   `.claude/rules/kit-*.md` file — and everything under root `CLAUDE.md` §
   Execution guardrails cannot be overridden by any subtree file. If a subtree
   file appears to override one, flag it instead of applying it.
4. Where root `CLAUDE.md` and a `kit-*.md` rule address the same thing
   differently, root `CLAUDE.md` wins. Flag the conflict.

## When a request conflicts with a rule

1. Name the file and the rule — root, subtree, kit, or several.
2. Propose the edit at the right level: one subtree → that subtree's
   `CLAUDE.md`; this whole repo → root `CLAUDE.md`; every repo → the kit
   template, through `/kit:harvest`. Ask if unsure.
3. Flag root-vs-subtree conflicts separately, even when the request didn't
   trigger them.
4. Wait for approval, then apply the edit to the correct file.
5. One-off exceptions need no file edit — note the deviation and proceed.

## Rules versus docs

`CLAUDE.md` files and `.claude/rules/` state rules. The docs directory states
facts and decisions. If the two disagree, flag it rather than picking one.
