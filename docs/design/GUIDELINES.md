# UI guidelines

The approved look is defined by [mockup-welcome.html](mockup-welcome.html) and [mockup-stage.html](mockup-stage.html):
clean, delicate and crisp at 4K. [mockup-stage-dark.html](mockup-stage-dark.html) is the dark-theme reference.
This file covers the two rules the mockups can't show on their own.

**Dark mode** uses the same design tokens with a dark override set (`DARK_TOKENS` / `DARK_CSS` in `ui/style.py`, switched by
the moon/sun button and kept as `?theme=dark`); text and numbers keep WCAG AA contrast on the dark surfaces, as they do on white.

## Capitalization: Title Case for UI labels, sentence case for sentences

**The rule in one line:** if it *names* something (a control, a section, a column, a state), use Title Case. If it *says*
something (a full sentence, a description, a message), use sentence case.

### Title Case

Use it for short labels that name things, usually 1–5 words, without final punctuation:

| Element | Examples |
|---|---|
| Buttons and links that act | Approve and Continue · Ask for Changes · Start Migration · New Run · Download All Artifacts |
| Navigation and agent names | Discovery · Requirements · Modernization · Infrastructure · Validation · FinOps |
| Page, card, section and dialog titles | Discovery & Assessment · What the Six Agents Do · The Legacy Estate |
| Tabs, segmented controls, menu items, toggles | Overview · Details · Files · Local · AgentCore · Live Deploy to kind · Show AWS Transform Notes |
| Table column headers and KPI labels | Severity · Finding · Evidence · Monthly Cost · Readiness · Smoke Tests |
| Status labels, chips and badges | Needs Review · Not Started · In Progress · Approved · Recorded Run · Resolved |
| Small field labels | Tools Found · Model Decided · In AWS Transform · Benefit |

How to Title Case:
- Capitalize the first and the last word, and every major word: nouns, verbs, adjectives, adverbs and pronouns.
- Keep these lowercase unless they come first or last: articles (*a, an, the*), coordinating conjunctions
  (*and, but, or, nor, for, so, yet*) and short prepositions of four letters or fewer (*as, at, by, for, in, of, on,
  to, up, via, with*).
- Capitalize both parts of a hyphenated word: *Right-Sizing*, *Built-In*.
- Never change the casing of product names, acronyms or code: *AgentCore, FinOps, EKS, IaC, RDS, kind, Nova 2 Lite,
  DeepSeek V3.2, `app.py`, `/api/items`*.

### Sentence case

Use it for anything that reads as a sentence or a description:
- headlines that are sentences ("17 findings, 3 of them critical. Recommended path: replatform.")
- the greeting ("Hi, let's migrate your legacy app.") and its subline
- body text, summaries, captions under KPIs ("3 critical", "Some preparation needed")
- gate messages ("Step 1 of 6 is ready for review. Next: requirements.")
- placeholders ("Ask for changes to Discovery…"), tooltips, toasts, alert bodies, empty states
- table cell content, and anything the model wrote

When in doubt: if it ends with a period, a question mark or an ellipsis, it's sentence case.

## Numbers: thin, clean, clear

- **Hero numbers** (KPIs, finale tiles, FinOps totals) use Inter **Light 300**, tracking −0.02em, with tabular
  lining figures (`font-variant-numeric: tabular-nums lining-nums`).
- **Units and qualifiers** stay quieter than the number: "/ 100", "/mo", "s" and "%" use the tertiary grey at about half
  the number's size and weight 400. A leading "$" is the same weight as the number, at about 70% of its size.
- **Numbers inside text and tables** use the surrounding weight (400 or 500) with tabular figures, so columns line up.
- **Formatting:** thousands separators ($1,250); no trailing ".0" ($365, not $365.0); at most one decimal for seconds
  (9.8 s); tokens in thousands (14.4k); a thin space before units where the font allows it.
- **Colour:** numbers are ink by default. Use colour only to carry meaning: critical/over budget in danger red,
  improvement in success green. Never colour a number just for decoration.
