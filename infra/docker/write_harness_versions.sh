#!/usr/bin/env bash
# Stage 3.1 — record the RESOLVED agent-CLI versions into /opt/harness-versions.json
# at harness-image build time.
#
# harness_image_digest tells you WHICH image a run used, never WHAT was inside
# it. Two builds of the same tag can carry different agent versions and nothing
# would detect it — which silently corrupts the Phase 8 harness-vs-harness
# comparison. The json is written from the *running* binaries (not the pinned
# input) so recording reflects reality: unpin one CLI, rebuild, the file changes.
set -euo pipefail

out=/opt/harness-versions.json

ver() { # first --version variant that answers; else "unknown"
    for flag in --version version -v; do
        if v=$("$1" $flag 2>/dev/null); then
            printf '%s' "$v" | head -1
            return
        fi
    done
    echo "unknown"
}

aider_v=$(ver aider)
mini_v=$(ver mini-swe-agent)
claude_v=$(ver claude)      # Claude Code prints "2.1.x (Claude Code …)" — keep the leading version
codex_v=$(ver codex)
opencode_v=$(ver opencode)

printf '{"aider":%s,"mini_swe_agent":%s,"claude_code":%s,"codex":%s,"opencode":%s}\n' \
    "$(printf '"%s"' "$aider_v")" \
    "$(printf '"%s"' "$mini_v")" \
    "$(printf '"%s"' "$claude_v")" \
    "$(printf '"%s"' "$codex_v")" \
    "$(printf '"%s"' "$opencode_v")" \
    > "$out"

echo "harness CLI versions -> $out:"
cat "$out"
