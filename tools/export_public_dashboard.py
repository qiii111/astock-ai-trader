#!/usr/bin/env python3
"""Public GitHub Pages data: intentionally contains NO private account information."""
import datetime
import json
from pathlib import Path

out = Path("docs/data.json")
out.parent.mkdir(parents=True, exist_ok=True)
payload = {
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "mode": "public_market_research_only",
    "accounts": [],
    "notice": "真实持仓、成本、资金和交易记录不会发布到公开网页。",
}
out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
print("Published public-safe metadata only; no database connection.")
