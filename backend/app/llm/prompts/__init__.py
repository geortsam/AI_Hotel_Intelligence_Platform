"""Versioned, content-checksummed prompt records. §5.3.

A prompt is data with an identity, not a string literal in a service -- the same argument
`accuracy_v1` and `distribution_v1` make about protocols, and the same checksum mechanism.

The registry lives in `app.llm.prompts.registry`; this module re-exports the two names a caller
actually needs so that `from app.llm.prompts import get_prompt` reads naturally.
"""

from __future__ import annotations

from app.llm.prompts.registry import REGISTRY, PromptRecord, get_prompt

__all__ = ["REGISTRY", "PromptRecord", "get_prompt"]
