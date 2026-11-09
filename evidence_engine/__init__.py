"""Independent educational evidence index. See README for provenance and limits."""
from .adapters import Adapter, CodexAdapter, ClaudeAdapter
from .store import Store
from .reader import Reader

__all__ = ["Adapter", "CodexAdapter", "ClaudeAdapter", "Store", "Reader"]
