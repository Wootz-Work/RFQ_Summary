"""
One-off: run costing estimate from manually supplied payloads (no Glide fetch).
Usage:
    venv/bin/python scripts/run_costing_from_payload.py          # dry run
    venv/bin/python scripts/run_costing_from_payload.py --apply  # write back to Glide
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rfq_summary.config import load_settings
from rfq_summary.schema import QueryPayload

# Re-use helpers already in the regenerate script
sys.path.insert(0, str(ROOT / "scripts"))
from regenerate_costing_for_all_rfq_rows import generate_costing, write_costing_value


JOBS = [
    {
        "row_id": "08FKwsCrTd2pG.nFRT.o1w",
        "subject": "price offer – FAM Stumabo (In-feed chute, Discharge chute, Bracket plate Ø315)",
        "from_name": "Purchasing Department FAM",
        "body": """\
From: Purchasing Department FAM <purchasing@fam.be>
Subject: price offer

Hi Paul, Suraj,

Thank you for your nice visit this morning.

Can you please make a price offer, including shipping costs and duties and
leadtimes for the following items:

Ref Fam | Description | Qty options
406646  | In-feed chute Centris 315 P V2                    | 2, 4, 6, 8, 10
406648  | Supporting device in-feed chute Centris 315 V2    | 10, 15, 20, 25, 30
404976  | Discharge chute Centris 315 (rigidized)           | 1, 5, 10, 15, 20
403753  | Bracket plate for cutting head Ø315 (locking system) | 1, 10, 20, 30, 40

Please find the drawings of these items attached.

Kind regards,
Kris
FAM | Neerveld 2, 2550 Kontich, Belgium
""",
        "attachment_urls": [
            "15s1FdK8OPrHY-LhkoQYHx9DsalZX5uye",
            "1oqNwM_xn7wB_n8HSK06PlPGlqJ0bKYdg",
            "1AqGXVsyvZBtjoVLyP9icFbtRU5iIOO2J",
            "1VBi4FFBZwZmFmqj8HEKtnjpU2QHdkELZ",
            "1O1u8z5kcZEZevYzPd9GGBuy8lTf6kz6z",
            "1_EQpVLBULpLSCMBCaHBhbJcDkTwPVLhA",
            "1LLywlVWLAIjcM7-QQifjlA1YovGF9xFA",
            "16dXdeNv79wAxRXfPhn6MKmsm8dYtJUs0",
            "1k0eqxE4VmwACxhEHMtq3iBeSXOtQnxl6",
            "1npIV0e98MZcytbbgH4frWC1yCX58C1Rj",
            "1EYBlv70pW8NYb-kIVw1frk3k8oyL6zuQ",
            "13SRJZAE116iMT64sjuKzkdhYl-vauAId",
            "1hmTxhRwLgRaLpsJ80intnKbRHKVmMSV2",
            "1n8dS4fGby1ix0Gq41qziIqkSWUMSfdcr",
            "1kNaf4b0WCH28ooXsniBgCi6jBWv1V8Nw",
            "1cceF9PwSnWirNcn1sHk4dndLBVe_vGNZ",
            "1OvHIJEeUAbUVyaaN14jloWaRjIJy5AQZ",
            "1YImfwWA7qsKTOFBaWYvxlO1gV_5qKC4U",
            "1Y1FQmx303Joeg3QFk_uq5K6fbmlrI4cm",
            "1YRfERhIYJ0friSC1uXs6b2Y4p0Ksr6P8",
            "1OdIu3VkszfiAqLEE6d7Wn2E76_6kjGQy",
            "13B9pfSJ0d4WBLCz534iWfbx4KLG9BEeM",
            "1yDx3D5ypF7ZS_YhEI-rTEBwMogPFUD0t",
            "1-xbjQx4DfZS6TYcLJBpy0jUjugoxkCOW",
            "1b49hNpnNTPZ5vOEry6ThAqrKnaT8Qi7R",
            "1mBFIy04oMcjqzKFetwRLbZjxtGD8E2aq",
            "1E9BeRe5B-EMtz2pcKclpFfr4VkbunQqU",
            "1u5VyfSH97ec9OjwIuqGAfMdt-_PzUyXg",
            "1uiZEeWds-EHckbqsEaNwUqBLbg9p3L2q",
            "122fSJU6k6qSKtMpgZSxmQFoGiucz66il",
            "1kXO5JE7pezTqaQ4uSe-UvsjjxpXLUJ5R",
            "1NJPSGgr8w3r9ZMCrNxlxRycVtcFl3OVk",
            "1UDHgfHbipdYHXM3WPF3sPmlUihPmC1oP",
            "1VWmNRrDYoYQEPrQeILbg6dbP6AJKRU4A",
            "1W_Yn-KlMSJbV-abUl1VBhv3PitdrESSP",
            "1qLpr3irk3fq2SpDQh-eWFFDPN5v1bMeW",
            "11Q0bn-5BEEEWflOlLHmPPzerM6lNmjmw",
            "1TNccFAJ2I-Bkc06kp-SbnnlyeE4js3W1",
            "1Uyft7OxQ436r3f654UHnX1LlCsUFoPU0",
            "14bKQ-lJwNhn9SQCa8iMp9RkchJVKp5kt",
        ],
    },
    {
        "row_id": "a-79y91LvSsaYDNR-Z3Pifg",
        "subject": "Shakti new RFQ – 50 pcs each per year",
        "from_name": "himanshu",
        "body": "Shakti new RFQ - 50 pcs each per year only",
        "attachment_urls": [
            "1T6GPyT6IMRESyEZ6E7Hc1WtTvyTtBzEy",
            "1dqDA534W2eI24N08qjrbpMToTtaj-lVk",
            "1fk-34suQpG0G4fxJlz-bUq1hdWd99ghF",
        ],
    },
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write results back to Glide.")
    args = parser.parse_args()

    settings = load_settings()
    failures: list[tuple[str, str]] = []

    for idx, job in enumerate(JOBS, 1):
        row_id = job["row_id"]
        payload = QueryPayload(
            row_id=row_id,
            subject=job["subject"],
            from_="",
            from_name=job["from_name"],
            body=job["body"],
            received_at="",
            attachment_urls=job["attachment_urls"],
            attached_media=[],
        )

        print(f"\n[{idx}/{len(JOBS)}] row_id={row_id}")
        print(f"[{idx}/{len(JOBS)}] subject={payload.subject!r}")
        print(f"[{idx}/{len(JOBS)}] attachments={len(payload.attachment_urls)}")

        t0 = time.perf_counter()
        try:
            estimate, reason, raw, attachments_ms = generate_costing(settings, payload)
            elapsed = int((time.perf_counter() - t0) * 1000)
            print(f"[{idx}/{len(JOBS)}] estimate={estimate!r}")
            print(f"[{idx}/{len(JOBS)}] reason={reason!r}")
            print(f"[{idx}/{len(JOBS)}] attachments_ms={attachments_ms}  total_ms={elapsed}")
            if not estimate:
                print(f"[{idx}/{len(JOBS)}] raw={raw!r}")

            if args.apply:
                write_costing_value(settings, row_id, estimate, reason)
                print(f"[{idx}/{len(JOBS)}] wrote to Glide OK")
            else:
                print(f"[{idx}/{len(JOBS)}] dry-run — pass --apply to write back")
        except Exception as exc:
            failures.append((row_id, f"{type(exc).__name__}: {exc}"))
            print(f"[{idx}/{len(JOBS)}] FAILED: {type(exc).__name__}: {exc}")

    print()
    if failures:
        print("Failures:")
        for row_id, err in failures:
            print(f"  {row_id}: {err}")
        return 1

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
