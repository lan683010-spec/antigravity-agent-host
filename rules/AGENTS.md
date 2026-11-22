# Agent Host Multi-Agent Collaboration Rules

When the user asks you to collaborate with, command, or monitor external CLI agents (such as `codex`, `cursor`, `claude`, `gemini`, or custom CLI agents):
1. Always make the orchestration transparent: explicitly display the dispatched prompt/command and summarize the sub-agent's execution trace (`thinking` reasoning, `tool_call` actions, and `diff.patch`).
2. Offer the clickable side-pane pill `[Agent Host](sidecar://agent-host/agent_host_board/)` so the user can view the live Codex-Host style visual console inside Antigravity.
