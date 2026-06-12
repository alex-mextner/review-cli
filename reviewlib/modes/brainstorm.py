"""--brainstorm: multi-round persona ideation with a moderator.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change). PERSONAS lives here because it is only
used by this mode.
"""
from __future__ import annotations

from pathlib import Path

from ..panel import PanelJob, format_result, run_panel, run_single

# Distinct expert personas for brainstorm rotation (pool >= 5). Each round assigns
# >= 3 of these, rotating so backends see a fresh role each round.
PERSONAS = (
    (
        "Pragmatic staff engineer",
        "20 years shipping production systems; values simplicity, "
        "incremental delivery, and proven tech over novelty.",
    ),
    (
        "Security-paranoid reviewer",
        "thinks adversarially about every input, trust boundary, secret, "
        "and failure mode; assumes the worst actor and the worst case.",
    ),
    (
        "DX / ergonomics designer",
        "obsessed with developer and user experience: clear APIs, good "
        "defaults, discoverability, error messages, and minimal friction.",
    ),
    (
        "Skeptical SRE",
        "cares about operability, observability, blast radius, rollback, "
        "and what breaks at 3am; distrusts anything without a failure plan.",
    ),
    (
        "Product-minded architect",
        "connects technical choices to user value and roadmap; weighs "
        "long-term flexibility against time-to-market.",
    ),
    (
        "Cost-conscious performance engineer",
        "watches latency, throughput, token/compute spend, and resource "
        "footprint; allergic to waste and premature scale.",
    ),
)


def mode_brainstorm(
    topic: str,
    models: list[str],
    cwd: Path,
    timeout: int,
    moderator: str,
    rounds: int,
    max_rounds: int,
) -> int:
    min_rounds = max(rounds, 5)
    max_rounds = max(max_rounds, min_rounds)
    panel = models  # run-as-is; personas always fill >= 3 slots even if panel < 3
    transcript_blocks: list[str] = []
    out: list[str] = [f"# Brainstorm: {topic}", f"panel={','.join(panel)} moderator={moderator} "
                      f"rounds>={min_rounds} max={max_rounds}"]

    persona_index = 0
    completed = 0
    for round_no in range(1, max_rounds + 1):
        shared = "\n\n".join(transcript_blocks) if transcript_blocks else "(this is the first round)"
        jobs: list[PanelJob] = []
        # Assign >= 3 distinct personas this round, rotating across the pool.
        slot_count = max(3, len(panel))
        for slot in range(slot_count):
            persona_name, persona_bg = PERSONAS[persona_index % len(PERSONAS)]
            persona_index += 1
            model = panel[slot % len(panel)]
            prompt = (
                f"You are a '{persona_name}' ({persona_bg}). You are in round {round_no} of a "
                "multi-round brainstorm. Build on the shared transcript of prior rounds — "
                "react, extend, challenge, or propose new angles from YOUR perspective. "
                "Be concrete; offer ideas, not pleasantries. Do not edit files.\n\n"
                f"TOPIC:\n{topic}\n\n=== SHARED TRANSCRIPT (prior rounds) ===\n{shared}"
            )
            jobs.append(PanelJob(model=model, prompt=prompt, diff="", label=f"{persona_name} ({model})"))

        round_results = run_panel(jobs, cwd, timeout)
        round_text = "\n\n".join(
            f"#### {r.model}\n{(r.stdout.strip() or r.stderr.strip() or '(no output)')}"
            for r in round_results
        )
        transcript_blocks.append(f"## Round {round_no}\n{round_text}")
        out.append(f"\n# Round {round_no}\n" + "\n\n---\n\n".join(format_result(r) for r in round_results))
        completed = round_no

        # Moderator summary + continue/stop decision (cannot stop before min_rounds).
        mod_prompt = (
            "You are the MODERATOR of a brainstorm. Summarize the round below in a few bullets, "
            "then decide whether the discussion has CONVERGED / saturated (diminishing new ideas). "
            "End your reply with a final line that is EXACTLY one of: 'DECISION: STOP' or "
            "'DECISION: CONTINUE'.\n\n"
            f"TOPIC:\n{topic}\n\n=== ROUND {round_no} ===\n{round_text}"
        )
        mod_result = run_single(moderator, mod_prompt, cwd, timeout)
        out.append(f"\n## Moderator (round {round_no})\n" + format_result(mod_result))

        if round_no < min_rounds:
            continue
        decision = mod_result.stdout.upper()
        if "DECISION: STOP" in decision:
            break

    # Final synthesis.
    full_transcript = "\n\n".join(transcript_blocks)
    synth_prompt = (
        "You are the MODERATOR. The brainstorm is complete. Read the full transcript and produce a "
        "final synthesis with: BEST IDEAS (ranked), TRADEOFFS, and a single concrete RECOMMENDATION.\n\n"
        f"TOPIC:\n{topic}\n\n=== FULL TRANSCRIPT ({completed} rounds) ===\n{full_transcript}"
    )
    synth = run_single(moderator, synth_prompt, cwd, timeout)
    out.append("\n# Final synthesis\n" + format_result(synth))
    print("\n\n".join(out))
    return 0 if synth.returncode == 0 else 1
