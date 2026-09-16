"""Cross-platform client for the html-previewer ephemeral preview service."""
from .client import (PreviewClient, PreviewError, build_zip, default_topic,
                     parse_expiry, slugify_topic)

__version__ = "0.1.0"

__all__ = [
    "PreviewClient",
    "PreviewError",
    "build_zip",
    "default_topic",
    "parse_expiry",
    "slugify_topic",
    "__version__",
]
