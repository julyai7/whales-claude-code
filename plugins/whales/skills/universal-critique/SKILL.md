---
name: universal-critique
description: Have Whales critique a screen the designer shares — a screenshot (pasted or dragged in), a live web page, or a Figma file — the same critique the Whales web app gives. Use when the designer asks for a critique, review or feedback on a screen or design ("critique this", "what's wrong with this page", "review my Figma file", "give me feedback on this screenshot"). This is Whales critiquing THEIR screen; recording the designer's own feedback on a design is `record_critique`, a different thing.
---

# Universal critique

Whales critiques a screen in three steps, and you run them with the
`universal_critique` tool: it settles what the screen is FOR, critiques it
against that, and then — only if the designer asks — you generate an updated
version. Pass `client_session_id` on every call.

## 1. Get the screen to Whales

**A web page or a Figma link** — nothing to upload. Call `universal_critique`
with `url`. A Figma link critiques every screen in the file.

**HTML already in this session** — a page you just wrote, or the designer
pasted. Call `universal_critique` with that markup as `html`. Do not
screenshot a recreation of it, and do not draw a second page that stands in
for the one already written.

**A screenshot** — upload it first with the script beside this skill (in this
skill's base directory):

```bash
python3 "<this skill's base directory>/critique_source.py" upload "<image path>"
```

It prints JSON with a `source_id`. Call `universal_critique` with that
`source_id` (and `filename`).

Where the image path comes from:
- **An image file already in this session**: the path you wrote. Upload that file. Do not redraw it.
- **Dragged in from Finder**: the path is in the designer's message as text.
  That is the original file — the best source.
- **Pasted**: Claude Code saved it to disk and gave you its path beside the
  image, as `[Image: source: <path>]`. Use that path. It is a reduced copy
  (2000px on its long edge) — if the upload comes back `likely_downscaled`,
  mention once that the original file would give a more accurate read of text
  sizes and tap targets. Don't block on it.

Never take a screenshot of your own, or describe the image, in place of
uploading what the designer gave you.

## 2. Settle the goal

The first result is `status: "awaiting_goal"`. Follow its `instructions`: say
in a sentence or two what Whales reads the screen as, and ask what it is
actually for. **Do not critique it yourself while you wait**, even though you
can see it.

When they answer, call `universal_critique` with the `critique_id` and
`reply` set to **their words, exactly as written** — Whales reads the reply
against its own reading, so a paraphrase loses what they meant.

If the designer said what the screen is for in the same message as the
request, pass it as `goal` on the first call and skip the question.

## 3. Relay the critique

`status: "ready"` carries the critique. Relay it as its own text instructs —
faithfully, keeping its table as a table, without adding scores or jargon.

Then follow `next_step`: offer, in one line, to generate an updated version.
**Do not generate unless they ask.**

## 4. Generate — only when asked

1. Call `get_rebuild_contract` (surface `host-agent`) before writing anything,
   and follow it.
2. Rebuild from the critiqued screen itself:
   - a screenshot → the file the designer gave you;
   - a web page or Figma file → the exact images that were critiqued, one per
     entry in `renders`:
     ```bash
     python3 "<this skill's base directory>/critique_source.py" fetch <critique_id> <index>
     ```
     Don't take a fresh screenshot — a live page can have changed since.
3. Don't call `get_design_profile` or restyle the screen into the designer's own
   conventions unless they ask in so many words. The critiqued screen wins.
4. Don't call `submit_design` for this rebuild.
5. In that same turn, open the rendered image so the designer sees the screen.
   On Cursor, open the PNG. Do not open the HTML as the thing they look at —
   that opens source, and it reads as if nothing was generated.

## Other results

- `empty` — the source had no screens (a component library, a moodboard). Say
  what it holds, from `source_summary`.
- `failed` — say so plainly. Any read of your own is yours, not Whales'.
- `not_enabled` — critique is off for this account; say so.
- An upload error — relay it. A missing token means re-running the installer.
