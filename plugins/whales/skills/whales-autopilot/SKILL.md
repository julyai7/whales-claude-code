---
name: whales-autopilot
description: Run Whales Autopilot — whales plans, directs and judges a redesign while you build each version in this repo. Use ONLY when the designer explicitly asks for whales autopilot ("run whales autopilot on…", "start autopilot"). Not for ordinary design or critique requests.
---

# Whales Autopilot

whales runs the design process; you are its hands. Every move comes back from
the `whales_autopilot` tool as an **Instruction**. You build exactly what it
says, shoot it, and send the **HostResult** back with `action=next`. whales
judges every version — **never grade, rank or critique your own work**, and
never decide for it whether a version is kept.

`autopilot_shoot.py` sits beside this skill. `critique_source.py` is in the
`universal-critique` skill beside it. Below, both mean those paths.

## Start

1. Read the repo enough to write the `codebase_summary`:
   `{app_url, routes_in_scope: [str], components: [{name, path, what}], tokens: {colors, type, spacing}, notes}`.
   `tokens` are the values you actually find (theme file, CSS variables, Tailwind config). Start the dev server if it is not running; `app_url` is where it serves.
2. Shoot today's screens in scope and upload them:
   ```bash
   python3 autopilot_shoot.py --url <app_url> --frames '[{"name":"<screen>","url_or_path":"<route>"}, ...]' --viewport <WxH> --out .whales/autopilot/start
   ```
   Its `source_ids` are the run's `source_ids`.
3. Make sure `.whales/` is ignored without touching tracked files: add a `.whales/` line to `.git/info/exclude` if it is not there.
4. Call `whales_autopilot` with `action=start`, `mode=repo`, the designer's brief (`core` at least: their words), `source_ids` and `codebase_summary`.

## Every response

Read `status`, relay `summary` to the designer in a sentence, then:

| status | do |
| --- | --- |
| `waiting_host` | carry out `instruction` (below), then `action=next` with the HostResult |
| `working` / `running` | call `action=next` again with only `run_id` |
| `waiting_person` | ask the designer each of `questions` (say what whales assumed, if given). Pass each answer with `action=steer`, `kind=answer`, `question`, `text` = their words exactly. Then `action=next` |
| `paused` | tell the designer; resume only when they say so (`steer kind=resume`) |
| `done` | report the best version (`best.tag`, `best.commit_sha`) and the branch; stop |
| `failed` / `stopped` | say so plainly with `message`; stop |

Follow `message` when it says more. The designer can steer any time: their note goes in `steer kind=note` with their words; "pause"/"stop" are `kind=pause`/`kind=stop`.

## Carry out a build Instruction (`kind: build`)

1. **Branch.** `git checkout <branch>` (create it from `base_commit`, or the current HEAD, if missing). If `reset_to` is set: `git reset --hard <reset_to>` before anything else — the last version was not kept.
2. **Build** what `task` says, using `context` (read every item), `images` (open each url; they expire, fetch them now), `reuse` (use these components and files, don't re-create them) and `step_note` (the designer's instruction for this step; it wins over your own taste). Produce one screen state per item in `frames`, in order.
3. **Where:**
   - `where: repo` — edit the codebase. Commit only the files you changed, message `autopilot <version>: <move>` (e.g. `autopilot v3: refine`). Note the commit sha and changed files.
   - `where: scratch` (wireframes) — write ONE throwaway HTML file at `.whales/autopilot/<run name>/<version>.html` (never a tracked file, never commit it). Put every frame inside one `<div id="window">`, side by side, labelled with its frame name. No commit.
4. **Shoot.** Write `scripts.lint_js` and `scripts.conv_js` to files under `.whales/autopilot/<run name>/`, then:
   ```bash
   # repo: one frame per item in `frames`, each at the route/state that shows it
   python3 autopilot_shoot.py --url <app_url> --frames '[{"name":"<frame>","url_or_path":"<route>","selector":"<optional>"}, ...]' \
     --viewport <viewport WxH> --lint-js <lint file> --conv-js <conv file> [--walk if scripts.walk] --out .whales/autopilot/<run name>/<version>
   # scratch: the board
   python3 autopilot_shoot.py --url file://<abs path to version.html> --viewport <WxH> --lint-js ... --conv-js ... [--walk] --out ...
   ```
   Exit 2 means Playwright is missing: run the install command it prints, then shoot again. Any `upload_errors`: fix and re-run; never send a HostResult without screenshots.
5. **Send back** with `action=next`, `run_id`, and `result` = HostResult, exactly as `send_back` says — never drop a field it names:
   `{instruction_id, source_ids (frame order), measurements: {lint, conv, walk}, commit_sha, changed_files, note, html}`.
   `html` is the scratch file's full text (wireframes only). `note` is one plain line on anything you could not build as asked — not a judgement of quality.

## Carry out a panel step (`kind: panel_step`)

whales plays a test user; you drive the browser. `panel.press` is what they press (none on step 1: just shoot the screen).

```bash
python3 autopilot_shoot.py --url <app_url or the page panel.screen_hint names> --viewport <WxH> \
  --press "<earlier presses of this task, in order>" ... --press "<panel.press>" --out .whales/autopilot/<run name>/panel-<step>
```

Each run starts a fresh browser, so repeat this task's earlier presses before the new one. Send back `{instruction_id, source_ids, panel: {pressed, changed, error?}}` from its output. Don't interpret what the user would think — whales does.

## Rules

- Never grade your own work, never skip a move, never reorder `frames`.
- Never send a HostResult missing a field `send_back` asks for; if you truly cannot produce one, say why in `note`.
- One commit per repo version; never push, never touch `main`.
- Keep the designer posted in one line per move; don't paste instructions or JSON at them.
