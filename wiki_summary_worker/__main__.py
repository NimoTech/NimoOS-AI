"""Entry point for `python -m wiki_summary_worker`.

Main loop lives in worker.py (added in Task 14). __main__ just dispatches.
"""
from wiki_summary_worker.worker import main

if __name__ == "__main__":
    main()
