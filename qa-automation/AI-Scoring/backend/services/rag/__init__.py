"""RAG provider seam — PulpoConnection.md §4.1.

The retrieval policy imports ONLY `provider` types and the `factory`
accessor. Vendor-shaped code (today: Pulpo, an external platform) lives
in exactly one module per vendor — swapping providers is one new module
plus one env value, mirroring the `data_provider` factory seam.
"""

from backend.services.rag.factory import get_rag_provider, close_rag_provider
from backend.services.rag.provider import RagDoc, RagFlag, RagHit, RagProvider

__all__ = [
    "RagDoc",
    "RagFlag",
    "RagHit",
    "RagProvider",
    "get_rag_provider",
    "close_rag_provider",
]
