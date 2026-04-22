"""Tribune — a panel of advocates for hard decisions.

Shells out to per-voice CLIs (claude / codex / gemini) so users authenticate
with their own subscriptions. No API keys.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

__version__ = "0.2.0"


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


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Voice:
    role: str           # display name
    bin: str            # "claude" | "codex" | "gemini"
    model: str | None   # model id, or None to use the CLI default
    system: str         # system prompt for this voice


@dataclass
class Panel:
    name: str
    description: str
    proposer: Voice
    skeptic: Voice
    red_team: Voice
    synth: Voice


# ---------------------------------------------------------------------------
# Built-in panels
# ---------------------------------------------------------------------------

DEFAULT_PANEL = Panel(
    name="default",
    description="Claude-only. Opus for Proposer/Red Team/Synth, Sonnet for Skeptic (divergence via different model).",
    proposer=Voice("Proposer", "claude", "opus", PROPOSER_PROMPT),
    skeptic=Voice("Skeptic", "claude", "sonnet", SKEPTIC_PROMPT),
    red_team=Voice("Red Team", "claude", "opus", RED_TEAM_PROMPT),
    synth=Voice("Verdict", "claude", "opus", SYNTH_PROMPT),
)

CROSS_PROVIDER_PANEL = Panel(
    name="cross-provider",
    description="Three providers. Claude Proposer, Codex Skeptic, Gemini Red Team, Claude synth.",
    proposer=Voice("Proposer", "claude", "opus", PROPOSER_PROMPT),
    skeptic=Voice("Skeptic", "codex", None, SKEPTIC_PROMPT),
    red_team=Voice("Red Team", "gemini", None, RED_TEAM_PROMPT),
    synth=Voice("Verdict", "claude", "opus", SYNTH_PROMPT),
)

BUILTINS: dict[str, Panel] = {
    DEFAULT_PANEL.name: DEFAULT_PANEL,
    CROSS_PROVIDER_PANEL.name: CROSS_PROVIDER_PANEL,
}


# ---------------------------------------------------------------------------
# Panel discovery (file-based custom personas + shareable rosters)
# ---------------------------------------------------------------------------

def _user_panels_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "tribune" / "panels"


def _panel_search_dirs() -> list[Path]:
    dirs: list[Path] = []
    local = Path.cwd() / ".tribune" / "panels"
    if local.is_dir():
        dirs.append(local)
    user = _user_panels_dir()
    if user.is_dir():
        dirs.append(user)
    return dirs


def _voice_from_toml(
    data: dict, role_key: str, role_display: str, fallback_system: str
) -> Voice:
    v = data.get(role_key) or {}
    return Voice(
        role=role_display,
        bin=v.get("bin", "claude"),
        model=v.get("model"),
        system=(v.get("system") or fallback_system).strip() + "\n",
    )


def _load_panel_file(path: Path) -> Panel:
    with path.open("rb") as f:
        data = tomllib.load(f)
    name = data.get("name") or path.stem
    desc = data.get("description", "")
    return Panel(
        name=name,
        description=desc,
        proposer=_voice_from_toml(data, "proposer", "Proposer", PROPOSER_PROMPT),
        skeptic=_voice_from_toml(data, "skeptic", "Skeptic", SKEPTIC_PROMPT),
        red_team=_voice_from_toml(data, "red_team", "Red Team", RED_TEAM_PROMPT),
        synth=_voice_from_toml(data, "synth", "Verdict", SYNTH_PROMPT),
    )


def _discover_panels() -> dict[str, Panel]:
    panels: dict[str, Panel] = dict(BUILTINS)
    for d in _panel_search_dirs():
        for p in sorted(d.glob("*.toml")):
            try:
                panel = _load_panel_file(p)
            except Exception as e:
                _eprint(f"tribune: skipping malformed panel {p}: {e}")
                continue
            panels[panel.name] = panel
    return panels


# ---------------------------------------------------------------------------
# Voice runners (one per CLI)
# ---------------------------------------------------------------------------

SUPPORTED_BINS = {"claude", "codex", "gemini"}


def _eprint(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def _header(name: str) -> str:
    bar = "─" * 18
    return f"\n\033[1m{bar} {name} ──────────────────\033[0m\n"


def _require_bin(bin_name: str, role: str) -> str:
    path = shutil.which(bin_name)
    if not path:
        _eprint(
            f"tribune: '{bin_name}' CLI not installed (required for {role})."
        )
        sys.exit(2)
    return path


def _stream_text(buf: list[str], chunk: str) -> None:
    sys.stdout.write(chunk)
    sys.stdout.flush()
    buf.append(chunk)


def _run_claude(bin_path: str, voice: Voice, user: str, *, stream: bool) -> str:
    cmd = [bin_path, "-p", "--append-system-prompt", voice.system]
    if voice.model:
        cmd += ["--model", voice.model]
    return _stream_subprocess(cmd, stdin_text=user, stream=stream)


def _run_gemini(bin_path: str, voice: Voice, user: str, *, stream: bool) -> str:
    prompt = f"[ROLE]\n{voice.system}\n\n[TASK]\n{user}"
    cmd = [bin_path, "-p", prompt, "-y"]
    if voice.model:
        cmd += ["-m", voice.model]
    return _stream_subprocess(
        cmd, stdin_text=None, stream=stream, stderr=subprocess.DEVNULL
    )


def _run_codex(bin_path: str, voice: Voice, user: str, *, stream: bool) -> str:
    # codex exec stdout is noisy (session banner, logs). Use --output-last-message
    # for a clean final-message capture; emit it as a single block when done.
    tf = tempfile.NamedTemporaryFile(
        "w+", delete=False, suffix=".txt", prefix="tribune-codex-"
    )
    tf.close()
    tmp = Path(tf.name)
    try:
        cmd = [bin_path, "exec", "--output-last-message", str(tmp)]
        if voice.model:
            cmd += ["-c", f"model={voice.model}"]
        prompt = f"[ROLE]\n{voice.system}\n\n[TASK]\n{user}"
        if stream:
            sys.stdout.write("(codex thinking…)\n")
            sys.stdout.flush()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        _, err = proc.communicate(input=prompt)
        if proc.returncode != 0:
            _eprint(f"\ntribune: codex exited {proc.returncode}.")
            if err and err.strip():
                _eprint(err.strip())
            sys.exit(1)
        out = tmp.read_text(encoding="utf-8").strip()
        if stream:
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
        return out
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _stream_subprocess(
    cmd: list[str], *, stdin_text: str | None, stream: bool,
    stderr: int | None = None,
) -> str:
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if stderr is None else stderr,
        text=True,
        bufsize=1,
    )
    if stdin_text is not None:
        assert proc.stdin
        proc.stdin.write(stdin_text)
        proc.stdin.close()
    assert proc.stdout
    buf: list[str] = []
    while True:
        chunk = proc.stdout.read(1)
        if not chunk:
            break
        if stream:
            _stream_text(buf, chunk)
        else:
            buf.append(chunk)
    err_text = ""
    if proc.stderr is not None:
        err_text = proc.stderr.read()
    rc = proc.wait()
    if stream:
        sys.stdout.write("\n")
    if rc != 0:
        _eprint(f"\ntribune: subprocess exited {rc}.")
        if err_text.strip():
            _eprint(err_text.strip())
        sys.exit(1)
    return "".join(buf).strip()


_RUNNERS = {
    "claude": _run_claude,
    "codex": _run_codex,
    "gemini": _run_gemini,
}


def _run_voice(voice: Voice, user: str, *, stream: bool) -> str:
    if voice.bin not in SUPPORTED_BINS:
        _eprint(
            f"tribune: voice '{voice.role}' uses unsupported bin '{voice.bin}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_BINS))}."
        )
        sys.exit(2)
    bin_path = _require_bin(voice.bin, voice.role)
    return _RUNNERS[voice.bin](bin_path, voice, user, stream=stream)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _slugify(text: str, max_len: int = 60) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:max_len].rstrip("-") or "decision"


def _read_context(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path).expanduser()
    if not p.exists():
        _eprint(f"tribune: context file not found: {path}")
        sys.exit(2)
    return p.read_text(encoding="utf-8")


def _build_user(
    question: str, context: str, *, prior: dict[str, str] | None = None
) -> str:
    parts = [f"QUESTION:\n{question}"]
    if context:
        parts.append(f"\nCONTEXT:\n{context}")
    if prior:
        for name, text in prior.items():
            parts.append(f"\n── {name} said ──\n{text}")
    return "\n".join(parts)


def _voice_label(voice: Voice) -> str:
    model = voice.model or "default"
    return f"{voice.role} — {voice.bin}:{model}"


def _write_adr(
    *,
    question: str,
    context: str,
    panel: Panel,
    proposer: str,
    skeptic: str,
    red_team: str,
    synthesis: str,
    out_dir: Path,
    filename_prefix: str | None = None,
) -> Path:
    today = dt.date.today().isoformat()
    slug = _slugify(question)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{filename_prefix}-" if filename_prefix else ""
    path = out_dir / f"{today}-{prefix}{slug}.md"

    title = question.strip().rstrip("?.")
    body = f"""# Decision: {title}

Date: {today}
Status: proposed
Panel: {panel.name}

## Question

{question.strip()}

## Context

{context.strip() if context else "_No context provided._"}

## Panel

### {_voice_label(panel.proposer)}

{proposer.strip()}

### {_voice_label(panel.skeptic)}

{skeptic.strip()}

### {_voice_label(panel.red_team)}

{red_team.strip()}

{synthesis.strip()}
"""
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _resolve_panel(name: str) -> Panel:
    panels = _discover_panels()
    if name not in panels:
        _eprint(
            f"tribune: unknown panel '{name}'. Available: "
            f"{', '.join(sorted(panels)) or '(none)'}."
        )
        sys.exit(2)
    return panels[name]


def _convene(
    question: str,
    context: str,
    panel: Panel,
    out_dir: Path,
    filename_prefix: str | None = None,
) -> int:
    try:
        sys.stdout.write(_header(_voice_label(panel.proposer)))
        proposer = _run_voice(
            panel.proposer, _build_user(question, context), stream=True
        )

        sys.stdout.write(_header(_voice_label(panel.skeptic)))
        skeptic = _run_voice(
            panel.skeptic,
            _build_user(question, context, prior={"Proposer": proposer}),
            stream=True,
        )

        sys.stdout.write(_header(_voice_label(panel.red_team)))
        red_team = _run_voice(
            panel.red_team,
            _build_user(question, context, prior={
                "Proposer": proposer,
                "Skeptic": skeptic,
            }),
            stream=True,
        )

        sys.stdout.write(_header(_voice_label(panel.synth)))
        synthesis = _run_voice(
            panel.synth,
            _build_user(question, context, prior={
                "Proposer": proposer,
                "Skeptic": skeptic,
                "Red Team": red_team,
            }),
            stream=True,
        )
    except KeyboardInterrupt:
        _eprint("\ntribune: interrupted.")
        return 130

    path = _write_adr(
        question=question,
        context=context,
        panel=panel,
        proposer=proposer,
        skeptic=skeptic,
        red_team=red_team,
        synthesis=synthesis,
        out_dir=out_dir,
        filename_prefix=filename_prefix,
    )
    sys.stdout.write(f"\n\033[1mWrote:\033[0m {path}\n")
    return 0


def cmd_ask(
    question: str,
    context_path: str | None,
    out_dir: Path,
    panel_name: str,
    filename_prefix: str | None = None,
) -> int:
    if not question or not question.strip():
        _eprint("tribune: question is empty.")
        return 2
    panel = _resolve_panel(panel_name)
    context = _read_context(context_path)
    return _convene(question, context, panel, out_dir, filename_prefix)


REVIEW_QUESTION = (
    "Should the following diff be committed as-is, or does it hide a "
    "regression, a scope-creep, or a failure mode that a maintainer six "
    "months from now will curse the author for?"
)


def cmd_review(panel_name: str, out_dir: Path, ref: str | None) -> int:
    if not Path(".git").exists() and not _in_git_repo():
        _eprint("tribune: not inside a git repository.")
        return 2

    if ref:
        diff_cmd = ["git", "show", "--no-color", ref]
        label = _git_short(ref) or ref
    else:
        diff_cmd = ["git", "diff", "--cached", "--no-color"]
        label = "staged"

    r = subprocess.run(diff_cmd, capture_output=True, text=True)
    if r.returncode != 0:
        _eprint(f"tribune: git failed: {r.stderr.strip()}")
        return 1
    diff = r.stdout
    if not diff.strip():
        _eprint("tribune: nothing to review "
                "(no staged changes; stage with `git add` first, "
                "or pass --ref HEAD to review a commit).")
        return 0

    panel = _resolve_panel(panel_name)
    context = f"Diff under review ({label}):\n\n```diff\n{diff}\n```"
    return _convene(
        REVIEW_QUESTION, context, panel, out_dir,
        filename_prefix=f"review-{label}",
    )


def _in_git_repo() -> bool:
    r = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and r.stdout.strip() == "true"


def _git_short(ref: str) -> str | None:
    r = subprocess.run(
        ["git", "rev-parse", "--short", ref],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        return r.stdout.strip()
    return None


def cmd_panel_list() -> int:
    panels = _discover_panels()
    if not panels:
        print("(no panels found)")
        return 0
    width = max(len(n) for n in panels)
    for name in sorted(panels):
        p = panels[name]
        tag = "built-in" if name in BUILTINS else "user"
        print(f"  {name.ljust(width)}  [{tag}]  {p.description}")
    return 0


def cmd_panel_show(name: str) -> int:
    panels = _discover_panels()
    if name not in panels:
        _eprint(f"tribune: unknown panel '{name}'.")
        return 2
    p = panels[name]

    def dump(v: Voice) -> dict:
        return {"role": v.role, "bin": v.bin, "model": v.model, "system": v.system}

    print(json.dumps({
        "name": p.name,
        "description": p.description,
        "proposer": dump(p.proposer),
        "skeptic": dump(p.skeptic),
        "red_team": dump(p.red_team),
        "synth": dump(p.synth),
    }, indent=2))
    return 0


def cmd_panel_install(src: str) -> int:
    src_path = Path(src).expanduser()
    if not src_path.is_file():
        _eprint(f"tribune: file not found: {src}")
        return 2
    if src_path.suffix.lower() != ".toml":
        _eprint("tribune: panel files must be .toml")
        return 2
    try:
        panel = _load_panel_file(src_path)
    except Exception as e:
        _eprint(f"tribune: invalid panel file: {e}")
        return 2
    dest_dir = _user_panels_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{panel.name}.toml"
    if dest.exists():
        _eprint(f"tribune: '{panel.name}' already installed at {dest}. "
                "Remove it manually to replace.")
        return 1
    shutil.copyfile(src_path, dest)
    print(f"Installed panel '{panel.name}' → {dest}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tribune",
        description="A panel of advocates for hard decisions.",
    )
    parser.add_argument("--version", action="version", version=f"tribune {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ask = sub.add_parser("ask", help="Convene a tribune on one question.")
    ask.add_argument("question", help="The decision question, in quotes.")
    ask.add_argument("--context", "-c", default=None,
                     help="Path to a file injected into every advocate.")
    ask.add_argument("--out", default="./decisions",
                     help="Directory to write the ADR (default: ./decisions).")
    ask.add_argument("--panel", default="default",
                     help="Panel name (default: default). See `tribune panel list`.")

    review = sub.add_parser(
        "review",
        help="Convene a tribune on a git diff (staged, or a given commit).",
    )
    review.add_argument("--panel", default="default",
                        help="Panel name (default: default).")
    review.add_argument("--out", default="./decisions",
                        help="Directory to write the ADR.")
    review.add_argument("--ref", default=None,
                        help="Review a specific commit (e.g. HEAD). "
                             "Default: staged changes.")

    panel_parser = sub.add_parser("panel", help="Manage panels.")
    panel_sub = panel_parser.add_subparsers(dest="panel_cmd", required=True)
    panel_sub.add_parser("list", help="List available panels.")
    show = panel_sub.add_parser("show", help="Show a panel config as JSON.")
    show.add_argument("name")
    install = panel_sub.add_parser(
        "install", help="Install a panel TOML file into the user config dir."
    )
    install.add_argument("file")

    args = parser.parse_args(argv)

    if args.cmd == "ask":
        return cmd_ask(args.question, args.context, Path(args.out), args.panel)
    if args.cmd == "review":
        return cmd_review(args.panel, Path(args.out), args.ref)
    if args.cmd == "panel":
        if args.panel_cmd == "list":
            return cmd_panel_list()
        if args.panel_cmd == "show":
            return cmd_panel_show(args.name)
        if args.panel_cmd == "install":
            return cmd_panel_install(args.file)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
