"""Synthetic comparison-only evaluation; Django must be explicitly initialized."""


def build_report(cases=None):
    """Evaluate strict synthetic fixtures, never an actual recommendation model."""
    from .runner import build_report as run
    return run(cases)


__all__ = ["build_report"]
