# JohnStudio Safety Model

## MVP guarantees

1. **No model API usage.** Workers are subprocess launches of local CLIs you've already authenticated.
2. **No browser scraping.** All research is via `WebFetch` at design time and baked into `seeds/research_report.md` for offline use.
3. **Human-in-the-loop merge.** `johnstudio merge` requires an explicit y/N confirmation (or `confirm=True` programmatically). No auto-merge.
4. **No `--no-verify`, no `--force` pushes.** Merger uses `--no-ff`. Pushing is never automatic.
5. **Imported skills are quarantined.** All external skills land with `enabled: false` + `trust_level: unreviewed`. You must `johnstudio skill enable <id>` (or pin it) to make the router see them.
6. **Workers cannot spawn workers.** They can request help via `HANDOFF_REQUEST.md`; the orchestrator decides.

## Scans (deterministic, in `safety.py`)

- **Protected paths**: `.env`, `.env.*`, `**/*.pem`, `**/*.key`, `~/.ssh/**`, `~/.aws/**`, `~/.config/gcloud/**`.
- **Dangerous commands**: `rm -rf`, `sudo`, `curl | bash`, `wget | bash`, `git push --force`, `chmod -R 777`.
- **Approval-required**: `npm install`, `pnpm install`, `pip install`, `brew install`, `docker compose up`, `git push`.

The collector runs all three scans across each worker's diff + RESULT.md. The reviewer subtracts heavily from the score when any hit is present and flags it in `FINAL_REVIEW.md`.

## What JohnStudio will NOT do

- It will not bypass provider terms.
- It will not write to your global git config.
- It will not delete worktrees without a `--prune-worktrees` opt-in.
- It will not auto-install dependencies.
