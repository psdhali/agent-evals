"""Dataset loading — base protocol and SWE-bench Lite loader."""

from swebench_eval.dataset.base import DatasetLoader, Instance
from swebench_eval.dataset.swebench_loader import SwebenchLiteLoader, load_single_instance

__all__ = ["DatasetLoader", "Instance", "SwebenchLiteLoader", "load_single_instance"]
