"""One module per tool. Each defines `CONTRACT` and `run`, and nothing is discovered.

`app.copilot.registry.build_default_registry` imports these six by name. A sixth module dropped
into this directory is not a tool until someone registers it there, and a test asserts the set of
modules here equals the set of registered tools, so an unregistered one cannot linger either.
"""

from __future__ import annotations
