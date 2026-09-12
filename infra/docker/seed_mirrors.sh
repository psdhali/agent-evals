#!/usr/bin/env bash
# Stage 2.2 — bake the SWE-bench repo set into the mirror image as bare mirrors.
#
# The 12 repos are the DISTINCT repo set across Lite/Verified/full (it does NOT
# grow with instance count — agreed-architecture-changes §2.4). Each is a FULL
# mirror here (Stage 2 flat mirror); Stage 7.2 later adds per-instance PRUNED
# refs so future fix commits are not even transferable. Read-only server-side.
#
# The image is the boundary: building it requires internet (this RUN), running
# it never does. A component that writes to a mount must assert it, never
# create it (agreed-architecture-changes §2.4) — so we FAIL if /srv/git is not
# present rather than mkdir an empty one.
set -euo pipefail

if [ ! -d /srv/git ]; then
    echo "FATAL: /srv/git missing (image build context broken)" >&2
    exit 1
fi

REPOS=(
    django/django
    sympy/sympy
    astropy/astropy
    scikit-learn/scikit-learn
    matplotlib/matplotlib
    sphinx-doc/sphinx
    pydata/xarray
    mwaskom/seaborn
    pytest-dev/pytest
    pylint-dev/pylint
    psf/requests
    pallets/flask
)

for r in "${REPOS[@]}"; do
    org="${r%/*}"
    dest="/srv/git/${r}.git"
    if [ -d "$dest" ]; then
        echo "mirror exists: ${r} (skipping)"
        continue
    fi
    echo "cloning mirror: ${r}"
    mkdir -p "/srv/git/${org}"
    git clone --mirror --quiet "https://github.com/${r}.git" "$dest"
    echo "  -> ${dest}"
done

echo "seeded $(find /srv/git -maxdepth 3 -name '*.git' -type d | wc -l) mirrors"
