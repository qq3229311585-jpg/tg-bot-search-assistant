#!/usr/bin/env python3
"""Validate deployment configuration without starting Telegram polling."""

from __future__ import annotations

import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main() -> int:
    try:
        from tg_bot.config import ensure_data_dir, validate_config
    except Exception as exc:
        print(f"ERROR config_import_failed: {exc}", file=sys.stderr)
        return 1

    diagnostics = validate_config()
    for warning in diagnostics["warnings"]:
        print(f"WARNING {warning}")
    if not diagnostics["ok"]:
        for error in diagnostics["errors"]:
            print(f"ERROR {error}", file=sys.stderr)
        return 1
    try:
        data_dir = ensure_data_dir()
    except Exception as exc:
        print(f"ERROR data_dir_unavailable: {exc}", file=sys.stderr)
        return 1
    print(f"OK configuration valid; data_dir={data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
