"""Canonical human-readable size formatting (decimal GB in, MB/GB/TB/PB out).

Single source of truth for size display — the 2026-07-28 review found five
hand-rolled copies with divergent tier thresholds (one switched to TB at
10,000 GB instead of 1,000) and one mixing a binary GiB base with decimal
tier labels, so the same estate rendered in different units on one page.

All helpers take DECIMAL gigabytes (bytes / 1e9). Callers holding raw bytes
convert with :func:`bytes_to_gb` first.
"""
from __future__ import annotations


def bytes_to_gb(size_bytes: float) -> float:
    """Bytes → decimal gigabytes (the unit every formatter here expects)."""
    return size_bytes / 1e9


def fmt_size(gb: float) -> str:
    """Scale decimal GB to the most readable unit (MB / GB / TB / PB)."""
    if gb >= 1_000_000:
        return f"{gb / 1_000_000:,.1f} PB"
    if gb >= 1_000:
        return f"{gb / 1_000:,.1f} TB"
    if gb >= 1:
        return f"{gb:,.1f} GB"
    return f"{gb * 1_000:,.0f} MB"


def fmt_size_split(gb: float) -> tuple[str, str]:
    """Return (value, unit) for templates that render the unit in its own span."""
    if gb >= 1_000_000:
        return (f"{gb / 1_000_000:,.1f}", "PB")
    if gb >= 1_000:
        return (f"{gb / 1_000:,.1f}", "TB")
    if gb >= 1:
        return (f"{gb:,.1f}", "GB")
    return (f"{gb * 1_000:,.0f}", "MB")


def fmt_size_exact(gb: float) -> str:
    """Tiered size with the exact GB figure in parens once it leaves GB range.

    For cost source notes: the prose reads at human scale ("5.0 TB") while the
    exact figure ("(5,000 GB)") stays reconcilable against a bill line.
    """
    if gb >= 1_000:
        return f"{fmt_size(gb)} ({gb:,.0f} GB)"
    return f"{gb:,.1f} GB"
