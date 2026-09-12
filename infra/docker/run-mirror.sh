#!/usr/bin/env bash
# Stage 2.2 git mirror — `git daemon` (ADR-0029). Read-only, foreground.
set -euo pipefail

# Assert the mount, never create it (agreed-architecture-changes §2.4): if the
# repos are missing the service must fail loudly, not serve an empty mirror
# that reports every repo present and makes the gate GRANT on nothing.
if [ ! -d /srv/git/django/django.git ]; then
    echo "FATAL: git mirror content missing at /srv/git (image was not seeded)" >&2
    exit 1
fi

# Mark every repo exportable (the daemon's own export marker). This is also
# what the CGI-era script did; under the daemon it is doing what it was
# designed for — with `--export-all` it is belt-and-braces.
find /srv/git -maxdepth 3 -name '*.git' -type d -exec touch {}/git-daemon-export-ok \;

echo "git mirror serving ${GIT_MIRROR_HOST} from $(du -sh /srv/git 2>/dev/null | cut -f1) of repos"
# Foreground (no --detach). --verbose + --log-destination=stderr gives the
# "who cloned what" observability the CGI access_log provided, streamed to
# CloudWatch by the awslogs driver.
exec git daemon \
    --base-path=/srv/git \
    --export-all \
    --reuseaddr \
    --verbose \
    --log-destination=stderr \
    --port=9418 \
    --listen=0.0.0.0
