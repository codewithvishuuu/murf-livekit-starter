"""Telephony helpers for the Aarogya Sahayak voice agent.

Submodules are imported lazily so ``python -m telephony.outbound ...`` does
not trigger double-import warnings.
"""

from typing import Any

__all__ = [
    "OutboundCallError",
    "OutboundCallResult",
    "SIPCallFailedError",
    "classify_sip_failure",
    "dial_outbound",
    "is_sip_uri",
    "normalize_destination",
    "normalize_phone_number",
    "validate_destination",
    "validate_phone_number",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from telephony.outbound import (
            OutboundCallError,
            OutboundCallResult,
            SIPCallFailedError,
            classify_sip_failure,
            dial_outbound,
            is_sip_uri,
            normalize_destination,
            normalize_phone_number,
            validate_destination,
            validate_phone_number,
        )

        return {
            "OutboundCallError": OutboundCallError,
            "OutboundCallResult": OutboundCallResult,
            "SIPCallFailedError": SIPCallFailedError,
            "classify_sip_failure": classify_sip_failure,
            "dial_outbound": dial_outbound,
            "is_sip_uri": is_sip_uri,
            "normalize_destination": normalize_destination,
            "normalize_phone_number": normalize_phone_number,
            "validate_destination": validate_destination,
            "validate_phone_number": validate_phone_number,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
