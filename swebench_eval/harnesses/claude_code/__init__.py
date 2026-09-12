"""Claude Code harness adapter — `claude -p` routed through the LiteLLM gateway
Anthropic-passthrough (ADR-0002's F2 hardest case)."""

from swebench_eval.harnesses.claude_code.harness import ClaudeCodeHarness

__all__ = ["ClaudeCodeHarness"]
