# LLVM Reviewer Patterns

Observed from real review on `lijinpei-amd`'s PRs (reviewers: nikic, arsenm,
RKSimon, joker-eph, efriedma-quic, dtcxzyw, AaronBallman/cor3ntin/shafik, nhaehnle,
ssahasra, teresajohnson, jhuber6, …). Use when preparing PR text, responding to
review, or judging whether a patch is reviewer-ready. PR numbers cite where the
pattern was actually enforced.

## Motivation and scope

- Reviewers ask for motivation when a PR only describes mechanics — include both
  "why this matters" and "what it changes". Show where the pattern comes from in
  real code when possible (188731: *"context on where this shows up in the wild?"*;
  205053: *"being produced for real code?"*).
- State architecture boundaries for target changes (e.g. gfx generation); constrain
  the patch or explain why all targets share the property (180908: *"add an arch
  check for when this changed?"*).
- Separate measured performance from theory; don't assume a microarch argument is
  universal across VMEM/LDS/SMEM/gfx generations.
- Keep PRs tightly scoped. Split large infra/rename/constification/TableGen cleanup
  into a separate **NFC pre-patch** PR and stack the semantic change on top
  (201285 NFC pre-patch; 197638: infra change *"needs to be reviewed and submitted
  separately"*). For a stacked PR, say what's in this PR vs. the follow-up.

## Correctness bar

- **Correct-by-construction, not opt-in.** Mandatory ordering/dependencies must live
  in the IR/MIR/instruction model so every pass sees them — not in an optional DAG
  mutation a scheduler must remember to install (197638: *"Requiring a DAG mutation
  for functional correctness is a very big NO."* / *"correct by construction for all
  passes."*). Prefer modeling via implicit def/use of a fake physical register.
- **A crash on dubious IR is never "correct"** — emit a clean error and bail
  (205035, MLIR: *"A crash is never 'correct', we should error and bail out
  cleanly."*). In MLIR, drop discardable `dialect.attr` that collide with inherent
  attrs during lowering.
- **Poison/undef/flags rigor:** to fold, prove the value is genuinely non-poison
  (not merely non-zero/non-null); freeze *all nested* uses when de-duplicating
  (200120); mask poison-generating flags by reusing `andIRFlags` rather than a new
  flag list (201545, dtcxzyw); preserve fast-math/poison flags when rewriting nodes
  (201426). Multi-use undef is a known minefield — nikic declined to spend time on
  pre-existing multi-use undef fixes (202217); only touch with strong motivation.
- **Don't destroy information:** guard transforms behind a real `Changed` flag and
  only bump stats / return `true` when something changed; don't clobber `llvm.assume`
  range info — use `replaceUsesWithIf` + `isa<AssumeInst>` (183688).
- **Don't expand a UDiv with a possibly-poison divisor** (202378). `-0.0` is not
  subnormal and must not be flushed under denormal modes (202268).
- **Pass-manager hygiene:** use the DTU-accepting overload; `DTU.get()` flushes the
  dominator tree (204859).
- **API design:** decide caller-responsibility vs. defensive behavior before
  changing an interface; don't weaken an API to accept null/malformed values — add a
  separate overload/optional wrapper around the stricter helper (201426: *"I'd
  rather not weaken the API to allow null"*; 203367, joker-eph: push responsibility
  to the caller). Ask when uncertain.

## Test expectations

- Use UTC scripts for generated checks; hand-written checks get rejected
  (188731, 201047, 200120, 205052). `update_test_checks.py` (IR transforms),
  `update_llc_test_checks.py` (codegen), `update_cc_test_checks.py` (Clang codegen).
- Follow **InstCombine precommit-test style**: add tests first so the fix diff shows
  which checks change (188731).
- Build the dataflow/control-flow shape that makes the information genuinely
  unavailable where needed; don't rely on filler instructions that exploit an
  optimization cutoff (188731).
- Avoid a target triple used only to bias a cost model — force the path with a pass
  flag and move the test out of a target dir if possible; justify any required
  triple (202378: *"Do you know why this test requires an X86 triple?"*; 200647:
  prefer `-run-pass=none` / a subtarget).
- For crash fixes, test the **original reproducer** (when valid) *and* a distilled
  regression — edge cases hide in the original (200873 → follow-up 202276). Add a
  one-line comment + issue reference explaining what each test exercises (201431).
- Add negative/partial-coverage cases (183688: *"a case where only one use … to make
  sure other uses are still folded"*).
- Don't add explicit `verify` passes or `-verify-machineinstrs` (204859, 201285).
  Prefer `poison` over new `undef`. Don't weaken a test to compile-only unless output
  is inherently unstable and you say so.

## Code shape

- Take reviewer suggestions toward existing idioms: `isa<AssumeInst>`,
  `replaceUsesWithIf`, direct operand flags, `SaveAndRestore`, generated ODS
  accessors, `any_of`, `else if` for mutually-exclusive classifications (197638),
  setting flags directly vs. wrappers (199533: *"directly set the dead flag … instead
  of addRegisterDead?"*).
- If a new helper is warranted, make it a general helper parallel to existing style
  (201431: *"add something like CreateExactBinOp similar to CreateNoWrapBinOp"*);
  reuse existing entities/registers rather than inventing new ones.
- Prefer static/free functions over members when no state is needed.
- Names must be accurate and consistent — one term everywhere; don't name a modeling
  concept after a hardware counter that may diverge (197638, RyanRio/ssahasra).
- Trim AI-looking/verbose/inaccurate comments (199963, jhuber6: *"Trim up AI
  generated comments please."*). Reference issues by name (`GH192264`), not pasted
  URLs in comments (202276).
- Split unrelated NFC churn out of the bug-fix patch.

## PR etiquette

- `Fixes: https://github.com/llvm/llvm-project/issues/<n>` in the commit message.
- Add a `clang/docs/ReleaseNotes.rst` entry for user-visible Clang changes,
  especially diagnostics/frontend crash fixes (200873, cor3ntin).
- **Search for duplicate/competing PRs and coordinate before investing** (205143
  closed as duplicate; 201285: *"decide between you who is fixing what"*).
- Wait for the right **domain** reviewer's approval, not just any LGTM (205053:
  *"please wait for @teresajohnson"*).
- If a buildbot/premerge failure looks unrelated, inspect enough to say why; don't
  ignore it if it plausibly touches your area. clang-format/premerge are hard gates.
- State a concrete reason when closing a PR (184664, arsenm: *"Reasoning should be
  given when closing PRs."*). Ping at a courteous cadence (~weekly); for blocked
  main/premerge, explain urgency. After approval + green CI, ask a reviewer to merge
  if you lack commit access.
- Never `Co-Authored-By: Claude`; use `Assisted-by: <model/tool>` for substantial AI
  content, omit for trivial.

## Responding to review

- Answer the precise concern with evidence: local reproduction, pass output,
  generated IR/MIR, target docs, or a reduced example. Prefer "I tested X and
  observed Y" over speculation.
- If the reviewer points to a better abstraction/name, usually take it.
- When correcting a reviewer assumption, quote the relevant IR/MIR/pass output and
  keep the tone direct.
- If a request would broaden scope, propose a follow-up and keep the current patch
  focused (unless the patch is incomplete without it).
- After fixing one site, check sibling sites with the same pattern and add a test
  for each (the user routinely generalizes a fix across all call/construction sites).
- When unsure about intended semantics, ask one concrete question listing 2–3 viable
  behaviors instead of guessing.
