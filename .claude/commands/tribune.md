---
description: Convene a tribune on a decision (Proposer / Skeptic / Red Team)
argument-hint: <question>
allowed-tools: Bash
---

You are the user's assistant, not the tribune itself. The tribune is a separate program the user installed; your job is to invoke it and present the result.

Run:

```bash
tribune ask "$ARGUMENTS"
```

Stream the output verbatim (all three advocates plus the verdict) so the user sees it. After it finishes, tribune prints a line like `Wrote: decisions/YYYY-MM-DD-slug.md`. Read that file and offer to commit it to the user's repo if they want — but do not commit without explicit confirmation.

Do not paraphrase the advocates. Do not re-summarise the verdict. The whole point is that each voice speaks on the record, verbatim.

If `tribune` is not found on PATH, offer to install it automatically. Ask the user first, then run whichever works:

```bash
pip install tribune-cli
# or
pip3 install tribune-cli
# If not on PyPI yet, install direct from GitHub:
pip install git+https://github.com/ao92265/tribune.git
```

After install, re-run the original command.

Also confirm Claude Code is on the PATH (tribune shells out to `claude -p`).
