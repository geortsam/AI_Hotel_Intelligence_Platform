"""The copilot's tool boundary. Stage 7.6 — tools, their registry and the bounded loop.

    app.copilot.contracts   §7.3's per-tool template as types; the tool context and outcome
    app.copilot.registry    the allow-list: name -> contract -> function, checked at import
    app.copilot.catalogue   contracts -> the ToolSpecs a ChatRequest carries
    app.copilot.loop        §5.4's bounded loop and §5.7 row 4's failure rule
    app.copilot.tools       one module per tool, each delegating to one existing service method

Invocation — hotel binding, role check, argument validation, execution and audit — is a unit of
work, so it lives in the service layer: `app.services.tool_invocation`.

**What is not here, deliberately:** no endpoint, no `CopilotService`, no conversation, no prompt
for a copilot, no retrieval and no recommendation. Those are Stage 7.7 onward. Nothing in this
package opens a session, builds a query or names a vendor.

This module re-exports nothing, for the same reason `app.llm` does not.
"""

from __future__ import annotations
