"""Tribune v0 — a panel of advocates for hard decisions."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

try:
    from anthropic import Anthropic, APIError
except ImportError:
    sys.stderr.write(
        "tribune: anthropic SDK not installed. Run: pip install anthropic\n"
    )
    sys.exit(2)


MODEL_PROPOSER = "claude-opus-4-7"
MODEL_SKEPTIC = "claude-sonnet-4-6"
MODEL_RED_TEAM = "claude-opus-4-7"
MODEL_SYNTH = "claude-opus-4-7"
MAX_TOKENS = 1600

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


def _stream(client: Anthropic, model: str, system: str, user: str) -> str:
    buf: list[str] = []
    with client.messages.stream(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        for text in stream.text_stream:
            sys.stdout.write(text)
            sys.stdout.flush()
            buf.append(text)
    sys.stdout.write("\n")
    return "".join(buf)


def _complete(client: Anthropic, model: str, system: str, user: str) -> str:
    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "".join(parts)


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

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        _eprint("tribune: ANTHROPIC_API_KEY is not set.")
        return 2

    context = _read_context(context_path)
    client = Anthropic(api_key=api_key)

    try:
        sys.stdout.write(_header("Proposer"))
        proposer = _stream(
            client, MODEL_PROPOSER, PROPOSER_PROMPT,
            _build_user(question, context),
        )

        sys.stdout.write(_header("Skeptic"))
        skeptic = _stream(
            client, MODEL_SKEPTIC, SKEPTIC_PROMPT,
            _build_user(question, context, prior={"Proposer": proposer}),
        )

        sys.stdout.write(_header("Red Team"))
        red_team = _stream(
            client, MODEL_RED_TEAM, RED_TEAM_PROMPT,
            _build_user(question, context, prior={
                "Proposer": proposer,
                "Skeptic": skeptic,
            }),
        )

        sys.stdout.write(_header("Verdict"))
        synth_user = _build_user(question, context, prior={
            "Proposer": proposer,
            "Skeptic": skeptic,
            "Red Team": red_team,
        })
        synthesis = _complete(client, MODEL_SYNTH, SYNTH_PROMPT, synth_user)
        sys.stdout.write(synthesis.strip() + "\n")
    except APIError as e:
        _eprint(f"\ntribune: API error: {e}")
        return 1
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
