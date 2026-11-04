#!/usr/bin/env python3
"""Print the CHANGELOG.md section for a version (used as GitHub release notes and shown in "Update Available")."""
import re
import sys
from pathlib import Path

v = sys.argv[1]
text = Path(__file__).resolve().parents[1].joinpath("CHANGELOG.md").read_text(encoding="utf-8")
m = re.search(rf"^## \[?{re.escape(v)}\]?.*?\n(.*?)(?=^## |\Z)", text, re.S | re.M)
print(m.group(1).strip() if m else f"YouTube AI Agent Pro {v}")
