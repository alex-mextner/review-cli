# tgctl-real — the AGENT-SIDE suite against the REAL tg-ctl

This is the agent-side review-qa suite pointed at a **real `tg-ctl` checkout** (not the hermetic
miniature in `../tgctl-agentside`). It proves the agent-side harness catches the real tg-cli#98
duplicate-card bug class: the SAME suite verdicts **PASS** against a fixed `tg-ctl` and **FAIL**
against pre-#98 `tg-ctl`.

## What it drives

The daemon (`tg-ctl run`) and hook client (`tg-ctl ask`) are tg-ctl itself, driven against the
in-process fake Telegram (`reviewlib/qa/bot_harness.FakeTelegram`). The two cases:

1. an `AskUserQuestion` forwards exactly ONE inline-button card, and the user's tap delivers the
   chosen answer back to the agent (the hook client's stdout);
2. the identical question re-fires after it was answered — a fixed tg-ctl REPLAYS the stored
   answer and posts NO second card; pre-#98 tg-ctl re-posts a duplicate, superseded card (the bug).

## Running the RED→GREEN proof

It needs `bun` and a `tg-ctl` checkout (it is NOT run in normal CI — tg-cli is a separate repo).
The gated test `test_real_tgctl_*` in `tests/test_qa_bot_agent_side.py` runs it when
`REVIEW_QA_TGCTL_DIR` points at a tg-cli checkout, else SKIPs:

```sh
# GREEN — current tg-ctl (>= 1.19.2, the #98 fix)
REVIEW_QA_TGCTL_DIR=/path/to/tg-cli python3 tests/test_qa_bot_agent_side.py

# RED — a pre-#98 tg-ctl in a detached worktree (the duplicate-card bug)
git -C /path/to/tg-cli worktree add --detach /tmp/tgctl-pre98 271ff1d
ln -sfn /path/to/tg-cli/node_modules /tmp/tgctl-pre98/node_modules
REVIEW_QA_TGCTL_DIR=/tmp/tgctl-pre98 REVIEW_QA_TGCTL_EXPECT=FAIL \
  python3 tests/test_qa_bot_agent_side.py
```

`REVIEW_QA_TGCTL_EXPECT` (`PASS` default, or `FAIL`) is the verdict the gated test asserts, so the
same test pins both the green (fixed) and red (buggy) ends of the proof.
