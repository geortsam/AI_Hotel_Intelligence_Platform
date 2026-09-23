"""Provider adapters. The only place in this repository a vendor SDK may be imported.

One module per provider. Each translates between `app.llm.base`'s types and a vendor's API and
does nothing else -- no retry, no timeout, no validation, no budget, all of which
`app.llm.boundary` applies once for every adapter.

Nothing is re-exported here, and no adapter is imported at package import time. A vendor SDK is
an optional dependency (`backend/requirements-llm.txt`), so importing this package must not
require one; `app.llm.factory` imports the specific adapter it was configured for, and the
adapter defers the SDK import to first use.
"""

from __future__ import annotations
