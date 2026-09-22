# modules/status/__init__.py
# Minimal package export so module loaders can `import modules.status`
from .plugin import register  # re-export for convenience

__all__ = ["register"]
