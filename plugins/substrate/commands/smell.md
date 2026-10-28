---
description: Scan a file (or all staged deliverables) for AI-writing smell before it ships
---

Run the prose-smell gate on the file(s) the user names — or, with no
argument, on every prose file changed in the working tree. Resolve `$ROOT`
from `CLAUDE_PLUGIN_ROOT`, then `PLUGIN_ROOT`, or the parent of this loaded
command's `commands/` directory:

```
node "$ROOT/scripts/prose-smell.mjs" <files…>
```

Exit 2 means HIGH findings — rewrite those sentences as plain declarative
statements and re-run until clean. HIGH covers the canonical contrastive
frame (`isn't just about X — it's about Y`), its slogan spellings
(`a computation, not a decision`; `The product is not the gap.`), the
connective spellings (`rather than`, `instead of`, `X, never Y`,
`no X and no Y`, `would rather X than Y`) at four or more per 500 words,
whether-you're triples, assistant artifacts, and stock LLM vocabulary.
Medium findings (from-to coverage openers, em-dash density, two or three
connective contrasts per 500 words, a short slogan restating the long
sentence before it) are advice: apply judgment, especially for external
artifacts.

Inline code spans and fenced blocks are never scanned, so a report that
quotes a tell is not itself flagged. A `.prose-smell.json` above the file
with `{"contrastive": "advise"}` marks a tree as in-house prose: the
contrastive family is reported there as advice rather than failure, and
everything else keeps its severity.

The gate deliberately does not flag metaphors, qualifiers, or structure —
normal writing is not a tell. What it cannot check, you must: every claim
defensible cold by the human sender, and human pacing on sends.
