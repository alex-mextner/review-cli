# Suite: smoke

## Case: add 2 and 2 gives 4
Preconditions:
- `add.sh` is executable in the SUT root.
Steps:
- run `sh add.sh 2 2` from the SUT root.
Expected:
- it prints exactly `4` on stdout.
- it exits 0.

If the output is anything other than `4`, this case FAILS — cite the actual output as proof.
