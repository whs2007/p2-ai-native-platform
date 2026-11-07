"""python -m specflow 入口。"""
from __future__ import annotations

import sys

from specflow.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))