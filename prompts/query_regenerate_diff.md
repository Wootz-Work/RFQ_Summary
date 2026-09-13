You are marking up the current version of an RFQ triage summary to show a reader what is new or materially different since the previous version — in place, inline, with no separate summary section.

Someone who already read the previous version should be able to skim the current version and, from the underlining alone, know exactly what to re-examine.

---

## Input

- **Previous version** (for comparison only — never edit or reference its wording):
```
{{previous_response}}
```

- **Current version** (this is what you return, marked up):
```
{{current_response}}
```

- **Input diff** — a code-computed, exact list of what actually changed between the previous request and this one. This is ground truth. It may be empty (`(no prior input snapshot was sent — none available)`), which means you have no evidence of what moved in the input and must rely only on comparing the two versions' conclusions:
```
{{input_diff}}
```

- **Instruction that triggered this regeneration** (may be empty): `{{current_instruction}}`

---

## Your only two moves

1. **Copy the current version exactly.** Every word, number, heading, bullet, table cell and line break stays exactly as given. You are not rewriting, summarising, correcting, reordering or improving anything.
2. **Wrap spans that are new or materially different in `__double underscores__`**, using the material/not-material rules below to decide which ones qualify. That is the only change you are allowed to make to the text.

The output is a byte-for-byte copy of the current version except for inserted `__..__` pairs. If you cannot produce that, you have gone wrong.

---

## What counts as material — same bar as always

A change is material when it would alter what someone does next — how they price it, who they route it to, what they ask the customer, or whether they can commit to a date.

Material:

- A cost band, or the assumption the cost rests on
- The manufacturing route, process family, or supplier type
- A material, grade, coating, heat treatment, tolerance or standard
- Inspection, NDT, certification or documentation requirements
- Lead time, or the realistic-weeks view behind it
- A customer query appearing, disappearing, or changing its subject
- A risk or feasibility flag raised or withdrawn
- A quantity, or the basis a quantity was read on

**Not material — never underline these:**

- Rewording, reordering, tightening or lengthening that leaves the substance intact
- Formatting, punctuation, heading or table-layout changes
- A number restated in different units or rounding with the same meaning
- The same conclusion expressed with more or less hedging
- Anything the regeneration instruction asked for cosmetically ("make it shorter", "less formal")

---

## Never invent a cause — this is the rule that matters most

You may mark a span as new/changed only when one of these is true:

1. **The input diff names it.** The span reflects an entry in the input diff above — a field that was added, removed, or changed. This is the strong case: you have proof.
2. **The reading moved with no input diff to blame.** The input diff is empty or does not explain this span, but the same conclusion, fact or number genuinely does not appear anywhere in the previous version — this version noticed something the last one missed (a standard that governs, a conflict between sources, a cost driver that was overlooked). You may underline this, but never claim it came from new information you cannot point to in the input diff.

If a sentence merely restates something already present in the previous version in different words, it is not new — do not underline it, no matter how different the phrasing looks.

If you are not sure whether something changed versus was just reworded, do not underline it. Under-marking is the safe failure; inventing a change is not.

---

## Silence is the normal answer

**Most regenerations change nothing material.** When nothing in the current version qualifies under the rule above, return the current version completely unmodified — no `__` markers anywhere. Do not manufacture a difference to justify the pass. Do not underline something because the instruction touched that area of the document if the actual conclusion did not move.

You are not being judged on finding something.

---

## Output

Return the entire marked-up document inside a single `<annotated>` tag, and nothing else — no preamble, no explanation, no note about what you changed.

<annotated>
[the current version, verbatim, with __..__ added around qualifying spans — or completely unchanged if nothing qualifies]
</annotated>

Rules:

1. **Underline the specific fact, not the whole paragraph or bullet it sits in.** `__was zinc, now zinc flake__` inside a longer sentence, not the entire sentence.
2. **Every underlined span must make sense read alone** — a reader scanning only the underlined text should get the gist of what moved.
3. **Do not add "was X, now Y" commentary that is not already in the current version's own wording.** You are marking existing text, not writing new sentences. If the current version does not already state the before/after, underline the fact as written and let the input diff or the reader's own memory supply the contrast.
4. **Never touch the previous version.** It is reference only.
5. **Never emit `__` for any reason other than marking a qualifying change** — not for emphasis, not for anything the model would normally bold.
