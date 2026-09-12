"""The orchestrator API package.

Modules:

* ``main`` — the FastAPI app: control endpoints + the read-only dashboard
  endpoints (architecture §10), kept within the API service's blast radius
  (the control plane never imports this package).
* ``schemas`` — the typed response models that ``openapi-typescript`` turns
  into the frontend's API types via the OpenAPI spec.
* ``queries`` — pure read-side SQL for runs / instances / capacity.
* ``artifacts`` — S3 artifact proxying for patch / trajectory / log / report.
"""

from __future__ import annotations

_PACKAGE_UNUSED = "__api__"
