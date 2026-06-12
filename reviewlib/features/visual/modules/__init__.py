"""Built-in visual-check modules (§4).

These ship in review's own source and are trusted implicitly (only *contributed*
per-project modules go through the Stage-3 TOFU quarantine). Each wraps a cvGate
signal as a `VisualModule` so it can be force-run via `--check <name>` and contributes
its question to the single vision call.
"""
