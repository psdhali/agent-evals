"""Scripts as an importable package.

Lets tests type-check cleanly at CI scope (``mypy swebench_eval/ scripts/
tests/``): without this, mypy sees ``scripts/warm_image_cache.py`` both as the
top-level module ``warm_image_cache`` and as ``scripts.warm_image_cache``, and
bails out of checking the whole tree. The scripts themselves are still
executed directly as before; this only makes them importable by the tests that
verify them.
"""
