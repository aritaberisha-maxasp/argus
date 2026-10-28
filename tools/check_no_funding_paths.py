"""Fail if a Wise funding path appears in ARGUS code (ADR 0004, NF-04).

ARGUS prepares payments in Wise and must never fund or release them. The Wise client
refuses these calls at runtime; this check makes sure nobody even writes them down.
It scans the app code and the stand ins, not the tests or docs, which name the
paths on purpose to prove they are refused.

Run from the repository root: ``python tools/check_no_funding_paths.py``
"""

import re
import sys
from pathlib import Path

SCANNED = ("src", "standins")
FUNDING = re.compile(r"batch-payments|payment-initiations|/payments\b", re.IGNORECASE)


def funding_paths(root: Path) -> list[str]:
    """Every line under the scanned folders that names a funding path, as file:line: text."""
    problems = []
    for folder in SCANNED:
        for path in sorted((root / folder).rglob("*")):
            if not path.is_file() or path.suffix in {".pyc", ".png", ".pdf", ".jpg"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                if FUNDING.search(line):
                    problems.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    return problems


def main() -> int:
    problems = funding_paths(Path.cwd())
    for problem in problems:
        print(f"Wise funding path found: {problem}")
    if problems:
        print("ARGUS must never fund or release a payment (NF-04). Remove these lines.")
        return 1
    print(f"No Wise funding path in {', '.join(SCANNED)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
