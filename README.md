# Tribune

A panel of advocates for hard decisions.

You have a decision to make. You want more than one perspective, with real disagreement baked in, and a written record at the end. Tribune convenes three advocates — a **Proposer**, a **Skeptic**, and a **Red Team** — argues it out, and writes an ADR you can commit to your repo.

Not a chatbot. Not an SDLC framework. A decision instrument.

## Install

```
pip install tribune-cli
export ANTHROPIC_API_KEY=sk-ant-...
```

## Use

```
tribune ask "Should we migrate the audit log to Postgres or keep SQLite?"
tribune ask "Which auth library for the new service?" --context ./notes.md
```

Three advocates speak in turn, streamed to your terminal. When they're done, Tribune writes a markdown ADR to `./decisions/` with the verdict and the strongest unresolved objection.

Commit it. Review it in six months. See whether the Red Team was right.

## Output

A file at `./decisions/YYYY-MM-DD-slugified-question.md`:

```
# Decision: Wraith audit log storage
Date: 2026-04-22
Status: proposed

## Question
## Context
## Panel
  ### Proposer
  ### Skeptic
  ### Red Team
## Verdict
  (synthesized, 3 to 5 sentences)
## Minority report
  (strongest unresolved objection, verbatim)
```

## Why

Most AI dev tools collaborate. They agree with you, build what you ask, and ship. That's fine for code. It's dangerous for decisions.

Every serious decision needs a dissent. Tribune forces it.

Inspired by the Roman tribunes who spoke for the plebeians against the senate. Your advocate, on the record.

## What's in v0

Three hardcoded advocates. One question at a time. Markdown output. That's it.

## What's not in v0, deliberately

- Custom personas
- Shareable rosters
- Git hooks
- Web UI
- Hosted service
- Telemetry

If v0 gets run a second time by real users without being asked, v1 happens. If not, Tribune stays small and honest.

## License

MIT.
