#!/bin/sh
# sut.sh — the deterministic self-check the MOCKED tester (REVIEW_QA_FAKE_TESTER) runs to
# drive this SUT through the isolated worktree (reviewlib/qa/executor._fake_drive_sut).
# It runs the SAME assertion the prose `## Case:` suite states — `add.sh 2 2` must print
# "4" — and exits 0 on PASS, non-zero on FAIL. This keeps the mocked verdict in lockstep
# with what a real backend would conclude from reading the suite, so the fixture proves the
# executor plumbing (worktree -> exec -> verdict) without a paid model.
set -eu
dir="$(cd "$(dirname "$0")" && pwd)"
actual="$(sh "$dir/add.sh" 2 2)"
if [ "$actual" = "4" ]; then
  echo "PASS: add 2 2 == 4"
  exit 0
fi
echo "FAIL: add 2 2 expected 4, got $actual"
exit 1
