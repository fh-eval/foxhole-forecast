# Foxhole Forecast — design guidelines

Current language: **"The Field Notes"** (redesign, Aug 2026). These rules
govern `web/src/pages/` and `web/src/styles/`; update this file whenever the
design language changes.

## Concept

A warm field journal / scout's notebook. The previous design was a dark,
serious war-room dashboard; this one is deliberately bright and playful —
cream paper, chunky ink borders, hard offset "cut-out" shadows, rubber-stamp
labels, and pill-shaped status tags. A fun toy site that happens to contain a
rigorous experiment.

The reader is a classmate or friend who knows nothing about forecasting or
Foxhole. Every section leads with a plain-language question or conclusion
("Did anyone call it?"), and every technical term is defined in plain words
at the point of use.

## The four building blocks

1. **Paper.** `--paper #f3eedd` page, `--card #fbf7ea` surfaces, subtle dot
   texture on the body. Sections are separated by 2px *dashed* rules
   (notebook lines), not solid ones.
2. **Ink borders + hard shadows.** Cards and tables get
   `border: 2px solid var(--inkline)` plus `box-shadow: 3px 3px 0 var(--inkline)`
   (`--hard`; `2px 2px` for `--hard-sm`). Never soft blur shadows. Small
   rotations (±0.3–0.4deg) on clippings, briefings, and flow steps give the
   "cut out and pinned" feel — only on those component families, and
   disabled on mobile.
3. **Stamps & tags.** Kickers are solid ink blocks with paper text. The live
   war status is a rotated double-bordered rubber stamp. Statuses are
   bordered pill tags; dashed borders mark provisional states (open,
   censored).
4. **Marker highlight.** `--hl #f2dc9b` is used sparingly: `::selection`,
   key-phrase highlights in notes, hover fills on tag-like buttons.

## Two typefaces, two voices

Unchanged rule, still load-bearing:

- **Newsreader (serif) tells the story** — headlines, prose, briefings.
  Serif prose is always ≥ 15px with line-height ≥ 1.55.
- **IBM Plex Mono reads the instruments** — numbers, timestamps, tables,
  labels, buttons. Mono is never used for prose sentences.

**Nothing below 11px.** Serif headings use weight 650, not 500, on paper.

## Color

Light scheme (`color-scheme: light`). All body text colors hold ≥ 4.5:1 on
the paper/card surfaces:

- `--ink #262b21` text and structural borders; `--soft #4f5747` secondary
  text; `--faint #6c7462` captions (11px+ only).
- `--accent #b4551e` / `--accent-ink #93431a` (burnt orange) — attention,
  primary metrics, interactive affordances, stamp ink.
- `--olive #586b41` and `--steel #3f5a78` are reserved: faction identity
  (Colonials / Wardens). Olive doubles as the "settled / hit" state color;
  never use either decoratively.
- `--signal #a06a08` (amber) — partial/open/warning states.
- `--danger #a63d2a` — misses and model errors only.
- Color is never the only carrier of meaning: statuses and trends always
  have a text label or symbol (↑ ↓ →).

## Copy voice

- Friendly and direct, intro-undergrad level. Second person sparingly;
  contractions fine; a little humor allowed, clarity mandatory.
- **Scores are evidence, not verdicts.** Never present a rate as "accuracy",
  never declare a best model, and always show counts next to percentages.
  The caveat sections ("Don't crown a winner yet") stay on the page.
- **Auditability is non-negotiable.** Exact predictions, cutoffs, evidence,
  and settlement details stay reachable — the technical tables simply live
  behind clearly labeled "Audit view" disclosure widgets. Plain answer
  first, technical depth on demand.
- The human-note block at the top is the project owner's own corner; its tone
  is personal, not institutional.

## Layout and interaction

- Content column `min(1180px, 100% - 40px)`; prose capped ~620–850px.
- Longform human essays use a ~790px Newsreader column, a field-index contents
  card, and distinct epigraph/disclaimer treatments. Preserve the author's
  wording; visual hierarchy should do the editorial work instead of rewriting
  personal prose into the site's explanatory voice.
- Wide audit tables may scroll horizontally; `.table-shell` shows
  scroll-shadow affordances so they never look truncated.
- `:focus-visible` gets a 3px `--accent` outline. Motion (the status pulse,
  smooth scroll) is decorative and fully disabled under
  `prefers-reduced-motion`.
- Empty and error states use dashed-border cards and plain sentences.

## Housekeeping rules learned the hard way

- If JS emits a class, it must have a CSS rule (`status--complete` once
  shipped unstyled). Grep pages before deleting a rule; grep the stylesheet
  before inventing a new class.
- Reuse shared components (`plain-note`, `table-caption`, `method-note`,
  `rule-card`) across pages instead of page-local variants.
- Verify with `npm run build` in `web/` (includes `astro check`).
