"""Worker wrappers — harness and eval worker poll loops.

Phase 3: workers run as Python processes (not separate containers).
Phase 4: they become Docker containers in ECS — same interface,
different deployment.
"""
