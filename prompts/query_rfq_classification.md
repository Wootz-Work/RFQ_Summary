## Role
You classify one incoming RFQ email for a manufacturing sourcing workflow.

## Input
- **Mail / RFQ Data**: `{{mail_json}}`

## Task
Infer these fields from the email body and any available subject/sender metadata.

- `geography`: Customer geography, not Wootz.Work geography. Must be one of:
  `UK`, `US`, `Canada`, `ANZ`, `Europe`, `SEA`, `Middle East`, `Germany`,
  `Australia`, `New Zealand`, `South Africa`, `Africa`.
- `industry`: RFQ-specific industry/category. Examples include `Fittings & Hardware`,
  `Manufacturing`, `Food Processing`, `Automotive`, `Medical Devices`,
  `Furniture Manufacturing`, `Oil & Gas`, `Welding and Fabrication`. These are not
  fixed options. If the RFQ clearly belongs to another industry/category, write the
  most specific concise category you can infer.
- `client_name`: Customer/client company name inferred from sender email domain,
  signature, legal footer, or direct mention.
- `standards`: Standards explicitly mentioned, e.g. `DIN 933`, `ISO 4017`,
  `ASTM A193`. Leave empty if no standard is mentioned.
- `title`: Very short RFQ title, 4-5 words maximum, describing the requested item.
  Do not include sequence number. Avoid generic words like "new enquiry". Include
  material, grade, standard, or compliance words only when they materially define
  the RFQ or are highly required (e.g. PPAP, A2, ASTM grade). Otherwise keep title
  generic and concise, e.g. `Torx Screws`, `M6 Inserts`, `Brass Components`.

## Client Name Rules
- Remove legal suffixes and corporate filler words from `client_name`.
- Do not output suffixes such as Limited, Ltd, LLC, Inc, GmbH, Pvt Ltd, Private
  Limited, Co., Company, Corporation, Corp, PLC, LLP.
- Example: `BV Fasteners Limited` -> `BV Fasteners`.

## Output
Return a single JSON object only. No Markdown, no explanation.

```json
{
  "geography": "",
  "industry": "",
  "client_name": "",
  "standards": "",
  "title": ""
}
```
