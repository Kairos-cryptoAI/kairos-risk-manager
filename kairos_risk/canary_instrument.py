"""Exact EVEDEX DEV instrument rules for the manually armed technical canary.

The public venue response is normalized into decimal strings before it is
bound to a canary intent.  Risk recomputes the exact minimum quantity from
that immutable binding; Execution independently refreshes the official SDK
rules immediately before any mutation.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

import aiohttp
from kairos_core.contracts import EvidenceReferenceV1, StrategyIntentV1, canonical_sha256

EVEDEX_DEV_INSTRUMENT_URL = "https://trading-api.evedex.tech/api/market/instrument"
INSTRUMENT_RULE_DOMAIN = "evedex-dev-instrument-rule.v1"
CANARY_ENTRY_ORDER = "MARKETABLE_IOC_LIMIT"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_FETCH_TIMEOUT_SECONDS = 5.0

_RULE_DECIMAL_FIELDS = (
    "venue_lot_size",
    "venue_price_increment",
    "venue_quantity_increment",
    "venue_multiplier",
    "venue_min_volume_usd",
    "venue_min_price",
    "venue_max_price",
    "venue_min_quantity",
    "venue_max_quantity",
)


class CanaryInstrumentError(RuntimeError):
    """The current official DEV instrument rule is unavailable or unsafe."""


def canonical_decimal(value: Decimal) -> str:
    """Return one non-exponent, minimal representation for a finite decimal."""

    if not value.is_finite():
        raise ValueError("decimal must be finite")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        return "0"
    return text


def _decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return parsed


def _timestamp_ms(value: object) -> int:
    if not isinstance(value, str) or not value:
        raise ValueError("updatedAt must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("updatedAt must be a valid ISO-8601 timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError("updatedAt must be timezone-aware")
    return int(parsed.astimezone(UTC).timestamp() * 1_000)


def _string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class EvedexDevInstrumentRule:
    """Canonical subset of the official SDK/API instrument semantics."""

    venue_symbol: str
    trading: str
    market_state: str
    updated_at_ms: int
    lot_size: Decimal
    price_increment: Decimal
    quantity_increment: Decimal
    multiplier: Decimal
    min_volume_usd: Decimal
    min_price: Decimal
    max_price: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    fetched_at_ms: int

    def __post_init__(self) -> None:
        if not self.venue_symbol.endswith(":DEV") or self.venue_symbol != self.venue_symbol.upper():
            raise ValueError("venue_symbol must be one normalized EVEDEX DEV instrument")
        if self.trading != "all" or self.market_state != "OPEN":
            raise ValueError("EVEDEX DEV instrument must have trading=all and marketState=OPEN")
        for field in (
            "updated_at_ms",
            "fetched_at_ms",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        for field in (
            "lot_size",
            "price_increment",
            "quantity_increment",
            "multiplier",
            "min_volume_usd",
            "min_price",
            "max_price",
            "min_quantity",
            "max_quantity",
        ):
            object.__setattr__(self, field, _decimal(getattr(self, field), name=field))
        if self.lot_size != Decimal(1) or self.multiplier != Decimal(1):
            raise ValueError("technical canary supports only lotSize=1 and multiplier=1")
        if self.min_price >= self.max_price:
            raise ValueError("instrument minPrice must be below maxPrice")
        if self.min_quantity > self.max_quantity:
            raise ValueError("instrument minQuantity must not exceed maxQuantity")
        if self.min_price % self.price_increment != 0 or self.max_price % self.price_increment != 0:
            raise ValueError("instrument price bounds must align with priceIncrement")
        if self.min_quantity % self.quantity_increment != 0:
            raise ValueError("instrument minQuantity must align with quantityIncrement")

    @classmethod
    def from_api(cls, payload: dict[str, Any], *, fetched_at_ms: int) -> EvedexDevInstrumentRule:
        return cls(
            venue_symbol=_string(payload.get("name"), name="name"),
            trading=_string(payload.get("trading"), name="trading"),
            market_state=_string(payload.get("marketState"), name="marketState"),
            updated_at_ms=_timestamp_ms(payload.get("updatedAt")),
            lot_size=_decimal(payload.get("lotSize"), name="lotSize"),
            price_increment=_decimal(payload.get("priceIncrement"), name="priceIncrement"),
            quantity_increment=_decimal(payload.get("quantityIncrement"), name="quantityIncrement"),
            multiplier=_decimal(payload.get("multiplier"), name="multiplier"),
            min_volume_usd=_decimal(payload.get("minVolume"), name="minVolume"),
            min_price=_decimal(payload.get("minPrice"), name="minPrice"),
            max_price=_decimal(payload.get("maxPrice"), name="maxPrice"),
            min_quantity=_decimal(payload.get("minQuantity"), name="minQuantity"),
            max_quantity=_decimal(payload.get("maxQuantity"), name="maxQuantity"),
            fetched_at_ms=fetched_at_ms,
        )

    def rule_payload(self) -> dict[str, object]:
        return {
            "domain": INSTRUMENT_RULE_DOMAIN,
            "venue_lot_size": canonical_decimal(self.lot_size),
            "venue_market_state": self.market_state,
            "venue_max_price": canonical_decimal(self.max_price),
            "venue_max_quantity": canonical_decimal(self.max_quantity),
            "venue_min_price": canonical_decimal(self.min_price),
            "venue_min_quantity": canonical_decimal(self.min_quantity),
            "venue_min_volume_usd": canonical_decimal(self.min_volume_usd),
            "venue_multiplier": canonical_decimal(self.multiplier),
            "venue_price_increment": canonical_decimal(self.price_increment),
            "venue_quantity_increment": canonical_decimal(self.quantity_increment),
            "venue_symbol": self.venue_symbol,
            "venue_trading": self.trading,
            "venue_updated_at_ms": self.updated_at_ms,
        }

    @property
    def rules_sha256(self) -> str:
        return canonical_sha256(self.rule_payload())

    def effective_min_quantity(self, entry_price: Decimal | float) -> Decimal:
        price = _decimal(entry_price, name="entry_price")
        if price < self.min_price or price > self.max_price:
            raise ValueError("entry price is outside current EVEDEX instrument bounds")
        if price % self.price_increment != 0:
            raise ValueError("entry price is not aligned with EVEDEX priceIncrement")
        volume_minimum = (self.min_volume_usd / price / self.quantity_increment).to_integral_value(
            rounding=ROUND_CEILING
        ) * self.quantity_increment
        result = max(self.min_quantity, volume_minimum)
        if result > self.max_quantity:
            raise ValueError("effective minimum quantity exceeds venue maxQuantity")
        return result

    def metadata(self, *, quantity: Decimal) -> tuple[tuple[str, str], ...]:
        payload = self.rule_payload()
        return tuple(
            sorted(
                (
                    ("canary_entry_order", CANARY_ENTRY_ORDER),
                    ("canary_quantity", canonical_decimal(quantity)),
                    ("instrument_rules_sha256", self.rules_sha256),
                    *((key, str(value)) for key, value in payload.items() if key != "domain"),
                )
            )
        )

    def evidence(self) -> EvidenceReferenceV1:
        return EvidenceReferenceV1(
            kind="venue_instrument",
            reference=f"EVEDEX_DEV:{self.venue_symbol}:{self.updated_at_ms}",
            content_sha256=self.rules_sha256,
            observed_at_ms=self.updated_at_ms,
        )


async def fetch_evedex_dev_instrument(
    venue_symbol: str,
    *,
    fetched_at_ms: int,
) -> EvedexDevInstrumentRule:
    """Fetch one exact rule from the fixed public DEV endpoint."""

    timeout = aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout, raise_for_status=True) as session:
            async with session.get(EVEDEX_DEV_INSTRUMENT_URL) as response:
                body = await response.read()
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise CanaryInstrumentError("official EVEDEX DEV instrument endpoint is unavailable") from exc
    if len(body) > _MAX_RESPONSE_BYTES:
        raise CanaryInstrumentError("official EVEDEX DEV instrument response is unexpectedly large")
    try:
        decoded = json.loads(body, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanaryInstrumentError("official EVEDEX DEV instrument response is invalid JSON") from exc
    if not isinstance(decoded, list):
        raise CanaryInstrumentError("official EVEDEX DEV instrument response is not a list")
    matches = [item for item in decoded if isinstance(item, dict) and item.get("name") == venue_symbol]
    if len(matches) != 1:
        raise CanaryInstrumentError("exact EVEDEX DEV instrument is missing or duplicated")
    try:
        return EvedexDevInstrumentRule.from_api(matches[0], fetched_at_ms=fetched_at_ms)
    except (TypeError, ValueError) as exc:
        raise CanaryInstrumentError("official EVEDEX DEV instrument rule is unsafe") from exc


def bound_canary_instrument(
    intent: StrategyIntentV1,
    *,
    account_id: str,
) -> tuple[EvedexDevInstrumentRule, Decimal]:
    """Revalidate the immutable instrument snapshot and quantity binding."""

    metadata = dict(intent.metadata)
    expected_keys = {
        "account_id",
        "alpha_claim",
        "canary_entry_order",
        "canary_quantity",
        "entry_policy",
        "instrument_rules_sha256",
        "purpose",
        *_RULE_DECIMAL_FIELDS,
        "venue_market_state",
        "venue_symbol",
        "venue_trading",
        "venue_updated_at_ms",
    }
    if set(metadata) != expected_keys:
        raise ValueError("technical canary instrument metadata set is not exact")
    if metadata["account_id"] != account_id:
        raise ValueError("technical canary account binding is invalid")
    if (
        metadata["alpha_claim"] != "false"
        or metadata["canary_entry_order"] != CANARY_ENTRY_ORDER
        or metadata["entry_policy"] != "NEXT_BAR_MARKET"
        or metadata["purpose"] != "technical_execution_canary"
        or metadata["venue_trading"] != "all"
        or metadata["venue_market_state"] != "OPEN"
    ):
        raise ValueError("technical canary fixed policy metadata is invalid")
    try:
        updated_at_ms = int(metadata["venue_updated_at_ms"])
    except ValueError as exc:
        raise ValueError("venue_updated_at_ms is invalid") from exc
    if str(updated_at_ms) != metadata["venue_updated_at_ms"] or updated_at_ms < 0:
        raise ValueError("venue_updated_at_ms is not canonical")
    decimals: dict[str, Decimal] = {}
    for field in _RULE_DECIMAL_FIELDS:
        value = _decimal(metadata[field], name=field)
        if canonical_decimal(value) != metadata[field]:
            raise ValueError(f"{field} is not canonical")
        decimals[field] = value
    rule = EvedexDevInstrumentRule(
        venue_symbol=metadata["venue_symbol"],
        trading=metadata["venue_trading"],
        market_state=metadata["venue_market_state"],
        updated_at_ms=updated_at_ms,
        lot_size=decimals["venue_lot_size"],
        price_increment=decimals["venue_price_increment"],
        quantity_increment=decimals["venue_quantity_increment"],
        multiplier=decimals["venue_multiplier"],
        min_volume_usd=decimals["venue_min_volume_usd"],
        min_price=decimals["venue_min_price"],
        max_price=decimals["venue_max_price"],
        min_quantity=decimals["venue_min_quantity"],
        max_quantity=decimals["venue_max_quantity"],
        fetched_at_ms=updated_at_ms,
    )
    if metadata["instrument_rules_sha256"] != rule.rules_sha256:
        raise ValueError("technical canary instrument rule hash is invalid")
    venue_evidence = tuple(item for item in intent.evidence if item.kind == "venue_instrument")
    if len(venue_evidence) != 1 or venue_evidence[0] != rule.evidence():
        raise ValueError("technical canary venue instrument evidence is invalid")
    bound_text = metadata["canary_quantity"]
    bound = _decimal(bound_text, name="canary_quantity")
    if canonical_decimal(bound) != bound_text:
        raise ValueError("technical canary quantity is not canonical")
    if bound < rule.min_quantity or bound > rule.max_quantity or bound % rule.quantity_increment != 0:
        raise ValueError("technical canary quantity violates bound instrument increments")
    return rule, bound


def bound_canary_quantity(
    intent: StrategyIntentV1,
    *,
    account_id: str,
    worst_entry_price: float,
) -> float:
    """Return the exact venue minimum after rechecking it at Risk's worst entry."""

    rule, bound = bound_canary_instrument(intent, account_id=account_id)
    expected_quantity = rule.effective_min_quantity(Decimal(str(worst_entry_price)))
    if bound != expected_quantity:
        raise ValueError("technical canary quantity is not the current effective venue minimum")
    result = float(bound)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("technical canary quantity is not representable")
    return result
