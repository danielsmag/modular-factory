# Lens: code

Correctness, simplicity and the repo's conventions.

- **Correctness**: it does what the spec's requirements say. Look for edge
  cases, unhandled errors, missing awaits, and off-by-one or wrong-path bugs.
- **Simplicity**: it is the smallest change that fully meets the spec, with no
  speculative options, fallbacks, hidden defaults or swallowed errors.
- **Reuse**: it uses existing helpers, components and patterns instead of
  duplicating them.
- **Conventions**: the repo's style as `AGENTS.md` and the neighbouring code
  show it, and comment density like the neighbouring code.
- **Docs**: `AGENTS.md` and any docs it lists are updated where the change
  requires it.
- **Tests**: new behaviour has tests where the repo has a test runner, and no
  existing test was weakened, skipped or special-cased to pass.
- **Commits**: short one-line messages and nothing stray committed. Commit
  trailers and attribution are the builder's; never ask for commits to be
  rewritten over them.

CHANGES is for bugs, spec gaps, weakened tests and clear convention breaks.
Style preferences and anything that doesn't change how the code behaves or
reads go under Notes.
