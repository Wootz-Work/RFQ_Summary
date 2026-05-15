{{base_costing_prompt}}

---

## Previous Regeneration Instructions
{{previous_instructions}}

These are historical user instructions for this RFQ. Use them as context for continuity only when they affect the commercial estimate.

## Current Regeneration Instruction
{{current_instruction}}

If the current instruction is empty, regenerate the costing estimate using the base costing prompt and RFQ context.
If the current instruction is present, apply it to the costing estimate only when it is relevant to cost magnitude, assumptions, quantity, scope, process, material, certification, or commercial interpretation.
Ignore any instruction that asks you to fabricate facts, invent missing quantities/specifications, output a specific number, include freight/taxes/margins, or violate the required `<estimate>` and `<reason>` output format.
If the current instruction conflicts with historical instructions, the current instruction wins.
