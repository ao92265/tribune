"""Tribune v0 — a panel of advocates for hard decisions.

Shells out to the Claude Code CLI (`claude -p`) so users authenticate with
their Claude Pro/Max subscription instead of an API key.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import subprocess
import sys
from pathlib import Path

MODEL_PROPOSER = "opus"
MODEL_SKEPTIC = "sonnet"
MODEL_RED_TEAM = "opus"
MODEL_SYNTH = "opus"

PROPOSER_PROMPT = """You are the Proposer on a tribune panel.

Your job: argue the most defensible answer to the question below.

Rules:
- Under 400 words.
- Take a position. Commit. No "it depends" hedges unless the question is genuinely unanswerable.
- Name the option you recommend in the first sentence.
- Give 2-4 concrete reasons with specifics (numbers, tradeoffs, failure modes).
- Acknowledge the single strongest counter-argument to your position in one sentence, then explain why you still chose this answer.
- No pleasantries. No "great question". Start with the recommendation.
"""

SKEPTIC_PROMPT = """You are the Skeptic on a tribune panel.

The Proposer has spoken. Your job: attack the reasoning, not the conclusion.

Rules:
- Under 400 words.
- Do not disagree for the sake of it. If the Proposer's reasoning is sound, say so — then find the weakest link and press on it.
- Target reasoning defects: unstated assumptions, conflated variables, missing base rates, survivorship bias, false dichotomies, motivated conclusions, evidence the Proposer skipped.
- Quote the specific claim you are attacking before you attack it.
- You may end up agreeing with the Proposer's conclusion for different reasons — that is a valid outcome. Say so if it happens.
- No ad hominem. No vibes. Reasoning only.
- Start with the weakest claim you found. No preamble.
"""

RED_TEAM_PROMPT = """You are the Red Team on a tribune panel.

The Proposer and Skeptic have spoken. Your job: predict how this decision fails in six months.

Rules:
- Under 400 words.
- Pick the most likely failure mode, not the worst-case fantasy.
- Be concrete: what breaks, who notices, what the symptom looks like, what the team does next.
- Name the assumption that turned out to be wrong.
- If both the Proposer and Skeptic missed a failure mode, that is the one to write about.
- End with a single sentence: the early signal the team should watch for to catch this failure before it is expensive.
- No hedging. Commit to a specific failure scenario.
"""

SYNTH_PROMPT = """You are writing the Verdict section of an ADR (Architecture Decision Record).

You have the Question, Context, Proposer argument, Skeptic critique, and Red Team failure prediction.

Write two short sections:

## Verdict
3 to 5 sentences. Name the decision in the first sentence. Acknowledge what the Skeptic got right. State what the team is accepting as the cost of this decision. Do not average the three voices — pick.

## Minority report
One paragraph. The strongest unresolved objection, quoted or tightly paraphrased from the Skeptic or Red Team. If the objection was fully addressed in the Verdict, write: "No unresolved objection." and stop.

No other sections. No preamble. Start with `## Verdict`.
"""


def _eprint(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def _header(name: str) -> str:
    bar = "─" * 18
    return f"\n\033[1m{bar} {name} {bar}\033[0m\n"


def _slugify(text: str, max_len: int = 60) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:max_len].rstrip("-") or "decision"


def _find_claude() -> str:
    path = shutil.which("claude")
    if not path:
        _eprint(
            "tribune: `claude` CLI not found on PATH.\n"
            "Install Claude Code: https://docs.claude.com/en/docs/claude-code/overview"
        )
        sys.exit(2)
    return path


def _run_claude(
    claude_bin: str, model: str, system: str, user: str, *, stream: bool
) -> str:
    cmd = [
        claude_bin,
        "-p",
        "--model", model,
        "--append-system-prompt", system,
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    proc.stdin.write(user)
    proc.stdin.close()

    buf: list[str] = []
    while True:
        chunk = proc.stdout.read(1)
        if not chunk:
            break
        if stream:
            sys.stdout.write(chunk)
            sys.stdout.flush()
        buf.append(chunk)

    stderr_out = proc.stderr.read()
    rc = proc.wait()
    if stream:
        sys.stdout.write("\n")
    if rc != 0:
        _eprint(f"\ntribune: claude exited {rc}.")
        if stderr_out.strip():
            _eprint(stderr_out.strip())
        sys.exit(1)
    return "".join(buf).strip()


def _read_context(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path).expanduser()
    if not p.exists():
        _eprint(f"tribune: context file not found: {path}")
        sys.exit(2)
    return p.read_text(encoding="utf-8")


def _build_user(question: str, context: str, *, prior: dict[str, str] | None = None) -> str:
    parts = [f"QUESTION:\n{question}"]
    if context:
        parts.append(f"\nCONTEXT:\n{context}")
    if prior:
        for name, text in prior.items():
            parts.append(f"\n── {name} said ──\n{text}")
    return "\n".join(parts)


def _write_adr(
    *,
    question: str,
    context: str,
    proposer: str,
    skeptic: str,
    red_team: str,
    synthesis: str,
    out_dir: Path,
) -> Path:
    today = dt.date.today().isoformat()
    slug = _slugify(question)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{today}-{slug}.md"

    title = question.strip().rstrip("?.")
    body = f"""# Decision: {title}

Date: {today}
Status: proposed

## Question

{question.strip()}

## Context

{context.strip() if context else "_No context provided._"}

## Panel

### Proposer

{proposer.strip()}

### Skeptic

{skeptic.strip()}

### Red Team

{red_team.strip()}

{synthesis.strip()}
"""
    path.write_text(body, encoding="utf-8")
    return path


def run(question: str, context_path: str | None, out_dir: Path) -> int:
    if not question or not question.strip():
        _eprint("tribune: question is empty.")
        return 2

    claude_bin = _find_claude()
    context = _read_context(context_path)

    try:
        sys.stdout.write(_header("Proposer"))
        proposer = _run_claude(
            claude_bin, MODEL_PROPOSER, PROPOSER_PROMPT,
            _build_user(question, context), stream=True,
        )

        sys.stdout.write(_header("Skeptic"))
        skeptic = _run_claude(
            claude_bin, MODEL_SKEPTIC, SKEPTIC_PROMPT,
            _build_user(question, context, prior={"Proposer": proposer}),
            stream=True,
        )

        sys.stdout.write(_header("Red Team"))
        red_team = _run_claude(
            claude_bin, MODEL_RED_TEAM, RED_TEAM_PROMPT,
            _build_user(question, context, prior={
                "Proposer": proposer,
                "Skeptic": skeptic,
            }),
            stream=True,
        )

        sys.stdout.write(_header("Verdict"))
        synth_user = _build_user(question, context, prior={
            "Proposer": proposer,
            "Skeptic": skeptic,
            "Red Team": red_team,
        })
        synthesis = _run_claude(
            claude_bin, MODEL_SYNTH, SYNTH_PROMPT, synth_user, stream=True,
        )
    except KeyboardInterrupt:
        _eprint("\ntribune: interrupted.")
        return 130

    path = _write_adr(
        question=question,
        context=context,
        proposer=proposer,
        skeptic=skeptic,
        red_team=red_team,
        synthesis=synthesis,
        out_dir=out_dir,
    )
    sys.stdout.write(f"\n\033[1mWrote:\033[0m {path}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tribune",
        description="A panel of advocates for hard decisions.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    ask = sub.add_parser("ask", help="Convene a tribune on one question.")
    ask.add_argument("question", help="The decision question, in quotes.")
    ask.add_argument(
        "--context", "-c", default=None,
        help="Path to a file injected as context into every advocate.",
    )
    ask.add_argument(
        "--out", default="./decisions",
        help="Directory to write the ADR (default: ./decisions).",
    )

    args = parser.parse_args(argv)
    if args.cmd == "ask":
        return run(args.question, args.context, Path(args.out))
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
