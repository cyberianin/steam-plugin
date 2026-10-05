from __future__ import annotations


ZERO_DECIMAL = {"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF", "KZT"}
THREE_DECIMAL = {"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"}


def format_minor_units(amount_minor: int, currency: str) -> str:
    exponent = 0 if currency in ZERO_DECIMAL else 3 if currency in THREE_DECIMAL else 2
    major = amount_minor / (10**exponent)
    formatted = f"{major:,.{exponent}f}"
    return f"{formatted} {currency}"
