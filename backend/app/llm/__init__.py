"""The language-model boundary. One seam, one taxonomy, one place a vendor is named.

Stage 7.5 builds the abstraction and nothing on top of it: there is no endpoint here, no tool,
no retrieval and no copilot. `docs/v2-architecture.md` §5 is the specification.

    app.llm.base        the ChatModel protocol, and the request/response types
    app.llm.errors      the six declared failures of §5.7, as typed AppErrors
    app.llm.boundary    where the timeout, retry and budget rules are applied
    app.llm.prompts     versioned, content-checksummed prompt records
    app.llm.providers   adapters -- the ONLY place a vendor SDK may be imported
    app.llm.factory     settings -> a guarded ChatModel
    app.llm.testing     the doubles, so no test in CI makes a network call

Nothing in this package opens a database session, builds a query, reads a row or receives a
tenant identifier. That is structural rather than advisory: there is no field on `ChatRequest`
any of them could travel in, and an architecture test asserts the package imports no repository,
no model and no SQLAlchemy.

This module deliberately re-exports nothing. A star-import surface would let a caller reach the
adapter as easily as the protocol, and the whole value of the seam is that reaching past it is
inconvenient enough to be noticed in review.
"""

from __future__ import annotations
