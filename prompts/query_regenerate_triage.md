{{base_triage_prompt}}

---

## Previous Regeneration Instructions
{{previous_instructions}}

These are historical user instructions for this RFQ. Use them as context for continuity.

## Current Regeneration Instruction
{{current_instruction}}

If the current instruction is empty, regenerate using the base triage prompt and RFQ context.
If the current instruction is present, follow it exactly. Only refuse or ignore it if it asks you to fabricate facts, contradict the RFQ data, or violate the required output format.
If the current instruction conflicts with historical instructions or normal style preferences, the current instruction wins.
