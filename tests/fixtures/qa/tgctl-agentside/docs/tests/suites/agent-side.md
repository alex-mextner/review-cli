# Suite: bridge bot agent-side (question card + tap + #98 no-duplicate)

## Case: a question forwards ONE card and the tap reaches the agent
The agent emits an AskUserQuestion; the bridge must forward exactly one inline-button card, and
the user's tap must deliver the chosen answer back to the agent.
Ask-question: {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "session_id": "qa-sess-01", "tool_input": {"questions": [{"question": "Ship PR #42 now?", "header": "Ship decision", "options": [{"label": "Ship it"}, {"label": "Hold"}]}]}}
Expect-card: 1
Tap: Ship it
Expect-answer: Ship it

## Case: re-firing the SAME question replays the answer with NO second card (tg-cli#97/#98)
The identical question (same session + text) re-fires after it was answered. The bridge must
replay the stored answer down the hook client and post NO new card — a second card is the #98
duplicate bug whose tap reads as "expired".
Ask-question: {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion", "session_id": "qa-sess-01", "tool_input": {"questions": [{"question": "Ship PR #42 now?", "header": "Ship decision", "options": [{"label": "Ship it"}, {"label": "Hold"}]}]}}
Expect-card: 0
Expect-answer: Ship it
