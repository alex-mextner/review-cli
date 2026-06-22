#!/bin/sh
# sut.sh — the deterministic self-check the MOCKED tester (REVIEW_QA_FAKE_TESTER) runs to
# drive the BUGGY SUT through the isolated worktree (reviewlib/qa/executor._fake_drive_sut).
# It runs the SAME assertion the prose `## Case:` suite states — `add.sh 2 2` must print
# "4" — but the buggy add.sh prints "5", so this exits NON-ZERO (FAIL). That is what makes
# the mocked tester verdict the buggy fixture FAIL, mirroring what a real backend concludes
# from the expected-vs-actual mismatch.
set -eu
dir="$(cd "$(dirname "$0")" && pwd)"
actual="$(sh "$dir/add.sh" 2 2)"
if [ "$actual" = "4" ]; then
  echo "PASS: add 2 2 == 4"
  exit 0
fi
echo "FAIL: add 2 2 expected 4, got $actual"
exit 1
