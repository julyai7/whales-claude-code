---
name: design-profile
description: Apply the designer's own established design conventions — their colours, type, spacing, radii — when generating or revising any UI, and record what they accept or reject. Use whenever building, restyling, or reviewing an interface, component, page, or screen for this designer, and whenever they give feedback on one. Triggers on "build a page", "make a component", "restyle this", "does this match my design system", "use my conventions", and on any UI work in a project connected to Whales.
---

# Designing with this designer's own conventions

Whales holds this designer's established conventions, derived from their real
past work — Figma files, live sites, and designs they have already accepted or
rejected. The point of this skill is that you stop guessing at values they have
already decided.

## Before you write any UI

Call `get_design_profile`. Not after a first draft — before, because a draft
built on invented values has to be thrown away rather than nudged, and because
"I'll check it against the profile afterwards" reliably becomes a restyle pass
that loses whatever was good about the draft.

Pass `product` when you know which product this is for. Their products can
genuinely disagree with each other, so an unscoped read returns everything
rather than an average, and an average of two products' palettes is a palette
neither of them uses.

**Use what it returns literally.** If it states a spacing base unit, use
multiples of it. If it names six colours, use those six. A value that is not in
the profile is a value you are inventing on the designer's behalf — sometimes
correct, but it should be a deliberate, stated choice, never a silent one.

If the profile comes back empty, say so plainly and design from first
principles. An empty profile is a real and expected state for a designer whose
history has not been imported yet; treating it as a failure, or padding it with
generic "modern SaaS" defaults presented as theirs, is worse than saying it's
empty.

## When you need to know what they did before

Call `search_design_history` rather than reasoning from this conversation.
"How did I handle nav hierarchy last time?" has an actual recorded answer, and
inferring one from the current thread produces a confident guess that reads
exactly like a real memory.

## After you produce or revise a design

Call `submit_design` with the HTML and the product. It returns a conformance
articulation: which of their established conventions this matches, and which
values fall outside them.

**Report that back, including the parts that fail.** The out-of-convention list
is the useful half — it is the difference between "here's a design" and "here's
a design, and these four values aren't ones you've used before." Quietly
dropping the mismatches removes the only reason the call was worth making.

Re-submitting a revision for the same product is also how the system learns:
the diff against your previous submission becomes a real signal about what they
actually changed, with no extra step from anyone.

## When they react to a design

- They give feedback, however brief → `record_critique` with **their words**,
  not your paraphrase. "Too heavy" is the signal; "the user requested reduced
  visual weight" is your interpretation of the signal, and the interpretation
  is what the classifier is for.
- They accept it as-is → `record_approval`.

Both are quick and both matter more than they look: an approval is the only
positive evidence in the system. Without it, every convention is inferred from
complaints.

## Don't

- Don't generate UI first and reconcile with the profile afterwards.
- Don't merge conventions across products, or present one product's palette as
  the designer's general taste.
- Don't paraphrase a critique into `record_critique`.
- Don't report only the conventions a design matched.
- Don't invent values to fill a gap in the profile without saying that's what
  you're doing.
