---
name: working-on-llvm
description: >-
  Workflow and conventions for fixing upstream llvm/llvm-project (LLVM/MLIR/Clang)
  issues and getting PRs merged. Use when building/testing LLVM, writing or
  minimizing .ll/.mir/MLIR test cases, debugging passes (InstCombine, Attributor,
  LSR, JumpThreading, SelectionDAG/ISel, SCEV, AMDGPU, MLIR dialects), running
  alive2/llubi, using llvm-reduce, or preparing commits/PRs and addressing
  reviewer feedback (lijinpei-amd's setup).
metadata:
  type: reference
---

# Working on LLVM

Operate like an upstream LLVM contributor: prove the failure first, make the
smallest defensible change, preserve existing style, and leave a focused
regression test plus a PR-ready explanation. Conventions below are distilled from
the user's (`lijinpei-amd`, AMD) own work and from real reviewer feedback on their
PRs — follow them by default; they override generic instincts.

## Non-negotiables (the most-repeated corrections)

1. **Verify empirically, never by reasoning alone.** Prove the *pre-fix* binary
   reproduces (crash / assert / miscompile / missed optimization) and the
   *post-fix* binary doesn't. Answer behavior questions by running `opt`/`llc`,
   not by argument. Restore the tree and rebuild before finishing any A/B test.
2. **No verbose comments.** Don't add explanatory comments to `.cpp`/`.h` or to
   `.ll`/`.mir` tests. If a comment is truly needed, make it one line. Put
   rationale in the **commit message**, not the code.
3. **Never `Co-Authored-By: Claude`.** For substantial AI-generated content use an
   `Assisted-by: <model/tool>` trailer; omit any trailer for trivial assistance.
4. **One logical change per commit.** Split fix vs. test, and split independent
   changes. Don't fold unrelated edits into a commit. Amend into the *correct*
   commit when iterating.
5. **Minimal, surgical fixes.** Don't gold-plate, don't refactor uninvited, don't
   weaken/expand existing public APIs for a corner case (add an overload), prefer
   reusing existing helpers/entities over inventing new ones.

## First, orient (don't corrupt a parallel session)

```bash
git status --short --branch     # unrelated dirty changes belong to another session
git worktree list
```
- **Never `git stash` here** — the stash stack is shared across all worktrees on
  the common `.git`, so stash/pop can pull another session's changes into yours.
  Use a private `mktemp` patch or a throwaway WIP commit instead (see reference).
- If the task names a GitHub issue/PR, fetch it live first — comments included,
  they usually carry the reproducer and the disagreement:
  ```bash
  gh issue view N -R llvm/llvm-project --comments
  gh pr view  N -R llvm/llvm-project --comments
  gh pr diff  N -R llvm/llvm-project
  ```
- Dig for history before guessing intent: `git log -S'<symbol>' -- <paths>`,
  `git log -G'<regex>' -- <paths>`, `git blame <file>`.
- If a Godbolt shortlink is the only reproducer, fetch/decode it rather than
  reconstructing the source. Keep downloaded sources and reduced IR in a private
  `mktemp -d`, not in predictable shared `/tmp` paths.

## Build

Assertions always **ON**; build only the narrow tool you need; keep failures visible.
Build-dir layout varies per worktree (`.../NN/llvm-project/build/` *or*
`.../NN/build/llvm-project/`) — **discover it, don't assume** (see reference).

```bash
cmake -S llvm -B build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DLLVM_ENABLE_ASSERTIONS=ON \
  -DLLVM_TARGETS_TO_BUILD="AMDGPU;X86" \
  -DLLVM_ENABLE_PROJECTS="mlir;clang;lld" \
  -DLLVM_CCACHE_BUILD=ON -DLLVM_OPTIMIZED_TABLEGEN=ON

ninja -C build opt                         # narrow target; or llc/clang/mlir-opt/FileCheck
ninja -C build LLVMInstCombine && ninja -C build opt   # iterate: lib then tool
ninja -C build -d explain opt              # explain why outputs are dirty/clean
CCACHE_DISABLE=1 ninja -C build opt        # bypass ccache for work Ninja schedules
```

`CCACHE_DISABLE=1` does not make a clean Ninja target dirty. Edit or touch an
existing relevant source, or remove the specific suspect output, before rerunning
the target when a real rebuild is needed. Never create a placeholder source file.

## Test

```bash
build/bin/llvm-lit -sv llvm/test/Transforms/InstCombine/foo.ll   # single file
build/bin/llvm-lit -q  llvm/test/Transforms/JumpThreading        # directory sweep
ninja -C build opt && build/bin/llvm-lit -sv <test>              # build + test in one shot
build/bin/opt -S -passes=instcombine foo.ll | build/bin/FileCheck foo.ll   # one RUN line
```

**(Re)generate CHECK lines with the official UTC scripts** pointed at your build —
reviewers reject hand-written checks. Inspect the intended diff, then rerun the
same updater and verify that the file is byte-for-byte stable:
```bash
python3 llvm/utils/update_test_checks.py     --opt-binary build/bin/opt   <test>.ll
python3 llvm/utils/update_llc_test_checks.py --llc-binary build/bin/llc   <test>.ll
python3 clang/utils/update_cc_test_checks.py --clang build/bin/clang      <test>.c
before=$(sha256sum path/to/test.ll)
python3 llvm/utils/update_test_checks.py --opt-binary build/bin/opt path/to/test.ll
test "$before" = "$(sha256sum path/to/test.ll)"
git diff --check -- path/to/test.ll
```

## Debug

- **Passes (new PM):** `opt -passes=<pass> -S`; add `-verify-each` / `,verify` to
  catch broken modules; `-disable-output` when only checking for a crash/assert.
- **Tracing:** `-debug-only=<pass>` (e.g. `instcombine`, `attributor`, `isel`,
  `loop-reduce`, `indvars`), `-print-after=<pass>` / `-print-after-all`,
  `-print-changed=diff`, `-stats`.
- **Backend / ISel:** `llc -mtriple=amdgcn -mcpu=gfx900 -debug-only=isel -o /dev/null`,
  then slice one phase: `sed -n '/Initial selection DAG/,/Optimized lowered/p'`.
- **Temporary `errs() << "DBG171724…"`** prints are fine to localize — rebuild,
  grep the tag, then **remove them before committing**.
- **Reduce** with `llvm-reduce` + an `interesting.sh` that greps the *exact*
  assert text (candidate = last arg), then hand-finish to a minimal readable case:
  ```bash
  reduce_dir=$(mktemp -d "${TMPDIR:-/tmp}/llvm-reduce.XXXXXX")
  trap 'rm -rf -- "$reduce_dir"' EXIT
  cat > "$reduce_dir/interesting.sh" <<'EOF'
  #!/usr/bin/env bash
  /abs/build/bin/opt -passes=instcombine -disable-output "$1" 2>&1 \
    | grep -q 'cast<Ty>() argument of incompatible type'
  EOF
  chmod 700 "$reduce_dir/interesting.sh"
  llvm-reduce --test="$reduce_dir/interesting.sh" crash.ll -o reduced.ll
  ```
  (Use `llvm-reduce`, not bugpoint.)
- **Correctness / UB:** validate with alive2 (`alive-tv repro.ll`, or
  `alive-tv src.ll tgt.ll`) and/or llubi (`llubi --deterministic --verbose f.ll`,
  sweep `--seed=N`). Lit sweep + `grep -Fr "(unsound)" <alive-build>/logs/`.
  `llvm-diff a.ll b.ll` for IR equivalence across a refactor.
- **Shared-lib A/B gotcha:** tools link against `libLLVM*.so`, so copying
  `build/bin/llc` does *not* snapshot behavior — copy the `.so`s. See reference.
- **Bisect:** build `opt` at `<sha>^` and `<sha>` in private `mktemp -d` Release builds;
  `git bisect run` scripts use 0=good,1=bad,125=skip and rebuild inside.

## Test-case quality (reviewers care a lot)

- Reduce to the **smallest** reproducer. No `br i1 false` / branch-on-constant.
  Parameterize over function args, not magic constants. Strip unneeded
  `target datalayout`/`triple`; justify any required triple.
- Prefer `poison` over `undef` in new tests (or a function arg; or let an earlier
  pass create the `undef` naturally).
- **Precommit the test** (especially InstCombine) as its own commit so the fix
  commit's diff shows exactly which checks change. Also keep the **original**
  reproducer alongside the distilled regression for crash fixes.
- Don't use `-verify-machineinstrs` or explicit `verify` passes in committed tests;
  don't weaken a test to "compiles successfully" unless output is inherently
  unstable and you say so.

## Commit & PR

- Subject: **`[Component] Imperative summary`**. Add
  `Fixes: https://github.com/llvm/llvm-project/issues/NNNNN` to the fix commit.
- Two commits: **precommit test (NFC)** then **fix**. Split large infra/rename/NFC
  work into a separate pre-patch PR; keep each PR's diff tightly scoped.
- User-facing **Clang** changes need a `clang/docs/ReleaseNotes.rst` entry.
- clang-format only changed lines with the in-tree binary:
  `git clang-format --binary build/bin/clang-format HEAD~1 -- <files>`.
- Branch `YYYY-MM-DD-short-topic`; push to remote **`lijinpei-amd`** with
  `git push --force-with-lease lijinpei-amd <branch>`.
- **Search existing open PRs/issues** for duplicate/competing work and coordinate.
  Give real-world motivation. Wait for the right **domain** reviewer's LGTM. State
  a reason when closing a PR.

## References

- `references/local-workspace.md` — worktree/stash hazards, build-dir discovery,
  shared-lib A/B testing, ccache/PCH staleness, final local checks.
- `references/reviewer-patterns.md` — soundness bar, test expectations, code shape,
  PR etiquette, and how to respond to review, with concrete LLVM examples.

## Closing checklist

1. Pre-fix repro confirmed; post-fix confirmed fixed (empirically); tree restored.
2. CHECKs regenerated with UTC; a second updater run is byte-stable; intended
   diff inspected; `git diff --check` clean.
3. No stray debug prints, verbose comments, `verify`/`-verify-machineinstrs`, or
   unneeded triple/datalayout.
4. Relevant lit suite passes; alive2/llubi clean for correctness changes.
5. Precommit-test + fix as separate commits; `[Component]` subject; `Fixes:` URL;
   no `Co-Authored-By: Claude`.
6. clang-format applied; branch `YYYY-MM-DD-topic`; pushed `--force-with-lease` to
   `lijinpei-amd`.
7. Checked for duplicate PRs; release note added if Clang user-facing.
