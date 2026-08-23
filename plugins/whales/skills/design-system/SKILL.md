---
name: design-system
description: Create, extract, or maintain a design system for a product with Whales, from a Figma file or a live site. Use when the user asks to build/extract/generate a design system, points at a Figma link or website and wants its design system, registers or imports an existing one, updates or maintains one as their designs evolve, or asks what design systems they already have. Triggers on "design system", "design tokens", "style guide", "extract my design system", "maintain my design system", "design system from this Figma file".
---

# Design system extraction & maintenance

Whales either **extracts** a design system from a product's artifacts, or
**registers** one the designer already authored and maintains it from
there. These are different operations with different tools — routing
correctly matters more than moving fast.

## Always start by listing

Call `list_design_systems` **before asking the designer anything**. It's
cheap, returns metadata only, and it changes the first question from an
open one into a specific one.

Then establish which product this is for. Product name is a **join key**,
not a label — it's how every future update, critique, and generation finds
this design system again. A near-miss creates a second, half-populated
design system that never merges with the first.

- **If they have design systems already**: name them and ask which this
  is, or whether it's a new product. Never guess by fuzzy-matching what
  they said against the list — "the jobs one" is not a confirmation that
  they mean "Careers Portal". Offer, let them pick.
- **If the list is empty**: this is a new product. Confirm the exact name
  you'll register it under before proceeding, in one sentence. You're
  naming something permanent; a two-second check beats a rename that
  isn't supported.

## Then route: their system, or their artifacts?

Two paths, and you can usually tell which without asking:

- A **design system** looks like already-decided rules: a published
  component library, a token collection, a documented style guide, a
  variables file.
- **Product artifacts** look like output: screens, mockups, a live site,
  page designs.

**Infer, then confirm — don't ask cold.** "This looks like an existing
design system (40 published components, a token set) — want me to register
it as-is and maintain it from here, or treat these as artifacts and extract
a fresh system?" is a better question than "is this your design system or
your artifacts?". Only ask the open question when the input is genuinely
ambiguous.

### If they gave you their design system → `register_design_system`

Pass their content **verbatim**. Do not reformat it, reorder it,
normalize its naming, fill in gaps, or "improve" it on the way through.
A designer handing over their own system is asserting it's already
correct; tidying it destroys the thing they asked you to maintain.

If one already exists for that product, the tool stores the new content as
**pending** rather than replacing the live one, and says so. Relay that
plainly — tell them the live one is untouched and what to compare.

### If they gave you product artifacts → `generate_design_system`

This runs a real extraction pipeline and takes tens of seconds, reporting
progress as it goes. Don't call it more than once for the same product
concurrently.

Pass the source they actually gave you: `url` for a live site, `figma_url`
for a Figma file. Either one is used only when the product has nothing
ingested yet, so it's harmless to pass. A Figma source is the better one
where both exist — it's what the designer specified, rather than one
implementation of it, and it's the only source that fills in Organisms and
Templates.

If a design system **already exists** for that product, stop and ask
first. Regenerating is not obviously what they want — they may want to see
the current one (`get_design_system`), or update it. Confirm before
spending the run.

### A Figma link → look before you extract

When the artifact is a Figma file, call `extract_figma` **first**. It's
read-only, writes nothing, and costs no extra Figma request — the file is
cached server-side, so `extract_figma` then `generate_design_system` uses one
request between them.

It's worth the step because it answers the two things you'd otherwise be
guessing at, and it turns the product-name question above from open into
specific:

- **Is this their product, or reference material?** The file's own name, its
  page names, and whether it declares a real named-style vocabulary tell you.
  A file whose pages are "Comp Analysis" and "Inspo", with hundreds of loose
  images and no declared styles, is competitor research — extracting a
  "design system" from it would produce a confident description of someone
  else's work.
- **Which product name?** Cross the file name and page names against
  `list_design_systems`, then propose one name in a sentence.

**Figma's rate limit is the reason to be deliberate here.** A file allows
roughly six requests per twenty-eight days, and the seventh is refused for
days — not throttled, refused. So: don't call `extract_figma` repeatedly on
the same file to re-check something, and only pass `refresh_source: true`
when the designer says they've edited the file since.

## Reporting results

**Fetch the documents, don't retype them.** `generate_design_system` returns
`download_url` and `html_url`, not the document. Pull both to disk and tell
the designer where they are:

```bash
mkdir -p "design-system/<product>"
curl -sL "<download_url>" -o "design-system/<product>/design_system.md"
curl -sL "<html_url>"     -o "design-system/<product>/index.html"
```

Then summarize from `stats` — typeface, spacing base unit, palette,
component counts, screens found — in two or three sentences, and name the
two paths. Never reproduce the document from your own context: a designer
once received a file with whole sections replaced by "see the full
extraction for details" because a model rewrote a long, repetitive section
from memory. Fetching the bytes makes that impossible.

Report `caveats` too, and mention explicitly where evidence was thin if the
document says so. The document records its own gaps; a summary that drops
them reads as more complete than the document is.

## Don't

- Don't fuzzy-match a product name against the existing list — confirm.
- Don't reformat designer-supplied content.
- Don't regenerate over an existing design system without asking.
- Don't ask a question you can answer from `list_design_systems`,
  `extract_figma`, or the artifact itself.
- Don't extract from a Figma file without looking at it first — and don't
  re-read the same file to double-check. Each read spends a scarce request.
- Don't paste a design system into the chat or write it out yourself.
  Fetch the links.
