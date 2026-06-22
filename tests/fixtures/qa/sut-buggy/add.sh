#!/bin/sh
# add.sh — the tiny CLI under test (BUGGY variant). SHOULD print the sum of two integers,
# but has an off-by-one bug: it adds 1 too many.
# Usage: ./add.sh <a> <b>   ->   prints "<a+b+1>" (WRONG) and exits 0.
#
# This is the known-BUG half of the review-qa DoD fixture pair: `./add.sh 2 2` prints "5"
# (the suite's Expected is "4"), so the must-FAIL case verdicts FAIL with evidence (the
# expected-vs-actual mismatch). The bug is a single `+ 1` versus the good sibling — a
# realistic, evidence-producing defect, not a crash.
set -eu
a="$1"
b="$2"
echo "$((a + b + 1))"
