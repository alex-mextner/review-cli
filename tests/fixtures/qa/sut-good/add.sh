#!/bin/sh
# add.sh — the tiny CLI under test (GOOD variant). Prints the sum of two integers.
# Usage: ./add.sh <a> <b>   ->   prints "<a+b>" and exits 0.
#
# This is the known-GOOD half of the review-qa DoD fixture pair: `./add.sh 2 2` prints
# exactly "4", so the must-PASS case verdicts PASS. The buggy sibling (../sut-buggy/add.sh)
# is byte-identical EXCEPT for the arithmetic, so the only thing the tester can find is the
# real bug — not an incidental difference.
set -eu
a="$1"
b="$2"
echo "$((a + b))"
