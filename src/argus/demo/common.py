"""What the demo commands share (#124)."""

from urllib.parse import urlsplit


class DemoRefused(RuntimeError):
    """The demo command will not run: demo mode is off, or the target is not demo data."""


def control_root(url: str) -> str:
    """A stand in's own address. Its control calls sit at the root, not under ``/v1.0``."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
