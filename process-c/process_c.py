#!/usr/bin/env python3
"""Compatibility shim for the orchestrator.

The orchestrator launches Process C with::

    python3 /opt/shelfaware/process-c/process_c.py

…which predates Process C being a real package. This file just forwards to
:func:`process_c.main.run` so the orchestrator command line stays stable.

If you're running Process C standalone, prefer the console script::

    process-c

or::

    python3 -m process_c
"""

from __future__ import annotations

from process_c.main import run

if __name__ == "__main__":
    run()
