import os, sys
sys.path.insert(0, "src")  # adjust if your project structure differs

from rfq_summary.config import Settings
from rfq_summary.attachments import analyze_attachments

settings = Settings()

# Paste your real Drive file ID here — same one that passed the Drive read test
file_id = "1ND_DuVONvywbF6aJ5hOfoxZwnu568mxS"

# This is exactly the format Glide sends — a comma-separated string
results = analyze_attachments(settings, [file_id])

for r in results:
    print(f"kind    : {r.kind}")
    print(f"summary : {r.summary}")
    print(f"data    : {r.data}")