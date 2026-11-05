"""Deriver entrypoint: install the rerank hook, then run the stock deriver.

Run from Honcho's root (``/app``)::

    python -m src.honcho_rerank.deriver_main                 # then runs src.deriver
    python -m src.honcho_rerank.deriver_main next_entry.py   # then runs that script instead

The second form chains another entrypoint that itself ends by running ``src.deriver``,
such as the scheduler's ``deriver_main.py``, so both hooks are installed in one process.
"""

from __future__ import annotations

import runpy
import sys


def main(argv: list[str]) -> None:
    from .hook import install, wrap_deriver_main

    from .routing_telemetry import install as install_routing
    install_routing()
    try:
        install()
    except RuntimeError:
        # check_contract logs the miss and raises. Run stock Honcho with no wrapper.
        pass
    else:
        import src.deriver.queue_manager as queue_manager

        # src.deriver.__main__ imports main from queue_manager when it runs, after this.
        wrap_deriver_main(queue_manager)
    if argv:
        sys.argv = list(argv)
        runpy.run_path(argv[0], run_name="__main__")
    else:
        runpy.run_module("src.deriver", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main(sys.argv[1:])
