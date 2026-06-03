#!/usr/bin/env python3
"""file_io.py — small atomic file write helpers."""

import json
import os


def _atomic_write(path, writer, mode=None):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            writer(f)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        elif os.path.exists(path):
            os.chmod(tmp_path, os.stat(path).st_mode & 0o777)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise


def atomic_write_json(path, data, indent=2):
    """Write JSON via temp-file + fsync + os.replace."""
    def _writer(f):
        json.dump(data, f, ensure_ascii=False, indent=indent)
        f.write("\n")
    _atomic_write(path, _writer)


def atomic_write_text(path, text, mode=None):
    """Write text via temp-file + fsync + os.replace."""
    _atomic_write(path, lambda f: f.write(text), mode=mode)
