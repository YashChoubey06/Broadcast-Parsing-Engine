"""
Position identity normalisation and guarded lookup helpers.

Positions are keyed by canonical, non-null string values so SQLite unique
indexes cannot admit duplicate NULL variants for the same instrument.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from typing import Iterable, Optional

from src.alias_repository import get_alias_repository
from src.schemas import ParsedTradeEvent, PositionState


UNKNOWN_MARKET_GROUP = "UNKNOWN"

_GLOBAL_EQUITIES = {
    "AAPL", "AMD", "AMAT", "ASML", "AVGO", "CBRE", "CRDO", "CRWD", "CRWV",
    "DELL", "GEV", "HOOD", "INTU", "LLY", "LRCX", "MDB", "META", "MRVL",
    "MSFT", "MU", "NBIS", "NOW", "NVDA", "OKTA", "ORCL", "PANW", "PLTR",
    "RKLB", "SMCI", "SNDK", "SNOW", "SOXL", "STX", "WDC", "XOM",
}
_GLOBAL_INDEX_OR_MACRO = {
    "SP500", "NASDAQ", "DOW", "RUSSELL", "VIX", "DOLLAR_INDEX",
    "US_30Y_T_BOND", "EURUSD", "DASHUSD",
}
_GLOBAL_COMMODITIES = {"GOLD", "SILVER", "CRUDE", "CRUDE_MINI", "US_COFFEE"}
_INDIA_INDEXES = {"NIFTY", "BANKNIFTY", "BANKNNIFTY", "NIFTY_STRIKE"}
_INDIA_COMMODITIES = {
    "ALUMINIUM", "COPPER", "CRUDE", "CRUDE_MINI", "GOLD", "SILVER", "NATURAL_GAS",
}

_MONTHS = {
    "JAN": "01", "JANUARY": "01",
    "FEB": "02", "FEBRUARY": "02",
    "MAR": "03", "MARCH": "03",
    "APR": "04", "APRIL": "04",
    "MAY": "05",
    "JUN": "06", "JUNE": "06",
    "JUL": "07", "JULY": "07",
    "AUG": "08", "AUGUST": "08",
    "SEP": "09", "SEPT": "09", "SEPTEMBER": "09",
    "OCT": "10", "OCTOBER": "10",
    "NOV": "11", "NOVEMBER": "11",
    "DEC": "12", "DECEMBER": "12",
}


class IdentityMatchStatus(str, Enum):
    EXACT_MATCH = "EXACT_MATCH"
    UNIQUE_FALLBACK_MATCH = "UNIQUE_FALLBACK_MATCH"
    NO_MATCH = "NO_MATCH"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"


@dataclass(frozen=True)
class PositionIdentity:
    portfolio_id: str
    market_group: str
    symbol: str
    contract_month: str = ""
    option_type: str = ""
    strike_price: str = ""
    direction: str = ""
    supplied_fields: frozenset[str] = field(default_factory=frozenset)
    unresolved_fields: frozenset[str] = field(default_factory=frozenset)

    def full_key(self) -> tuple[str, str, str, str, str, str, str]:
        return (
            self.portfolio_id,
            self.market_group,
            self.symbol,
            self.contract_month,
            self.option_type,
            self.strike_price,
            self.direction,
        )


@dataclass
class IdentityResolutionResult:
    status: IdentityMatchStatus
    matched_position: Optional[PositionState] = None
    candidate_count: int = 0
    candidate_position_ids: list[int] = field(default_factory=list)
    explanation: str = ""
    fields_used: list[str] = field(default_factory=list)
    fields_missing: list[str] = field(default_factory=list)


def _blank(value) -> bool:
    return value is None or str(value).strip() == ""


def canonical_symbol(symbol: Optional[str]) -> str:
    if _blank(symbol):
        return ""
    raw = str(symbol).strip().upper()
    if raw == "SPX":
        return "SP500"
    return get_alias_repository().resolve(raw) or raw.replace(" ", "")


def canonical_direction(direction: Optional[str]) -> str:
    if _blank(direction):
        return ""
    raw = str(direction).strip().upper()
    if raw in {"LONG", "BUY"}:
        return "LONG"
    if raw in {"SHORT", "SELL"}:
        return "SHORT"
    return raw


def canonical_option_type(option_type: Optional[str]) -> str:
    if _blank(option_type):
        return ""
    raw = str(option_type).strip().upper()
    if raw == "CE":
        return "CALL"
    if raw == "PE":
        return "PUT"
    if raw in {"CALL", "PUT"}:
        return raw
    return raw


def canonical_decimal_string(value) -> str:
    if _blank(value):
        return ""
    try:
        dec = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return str(value).strip().upper()
    normalized = dec.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f").split(".")[0]
    return format(normalized, "f").rstrip("0").rstrip(".")


def canonical_contract_month(value: Optional[str]) -> tuple[str, bool]:
    if _blank(value):
        return "", False
    raw = str(value).strip().upper()
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        return raw, False

    match = re.search(r"\b([A-Z]+)\s+(\d{4})\b", raw)
    if match and match.group(1) in _MONTHS:
        return f"{match.group(2)}-{_MONTHS[match.group(1)]}", False

    match = re.search(r"\b(\d{4})\s+([A-Z]+)\b", raw)
    if match and match.group(2) in _MONTHS:
        return f"{match.group(1)}-{_MONTHS[match.group(2)]}", False

    if raw in _MONTHS:
        return raw, True
    return raw, True


def canonical_market_group(market_group: Optional[str], symbol: Optional[str]) -> str:
    sym = canonical_symbol(symbol)
    raw = "" if market_group is None else str(market_group).strip().upper()
    compact = raw.replace("-", " ").replace("_", " ")

    if raw in {
        "GLOBAL_EQUITY", "GLOBAL_INDEX_OR_MACRO", "GLOBAL_COMMODITY",
        "NSE", "INDIA_EQUITY", "INDIA_INDEX", "MCX", "INDIA_COMMODITY",
        UNKNOWN_MARKET_GROUP,
    }:
        return raw

    if compact in {"INDIAN INDICES", "INDIA INDICES", "INDICES", "INDEX"}:
        return "INDIA_INDEX"
    if compact in {"COMMODITIES", "COMMODITY"}:
        if sym in _GLOBAL_COMMODITIES:
            return "GLOBAL_COMMODITY"
        return "INDIA_COMMODITY"
    if compact in {"EQUITY", "EQUITIES", "NSE"}:
        return "INDIA_EQUITY"
    if compact in {"MCX"}:
        return "INDIA_COMMODITY"
    if compact in {"INTERNATIONAL MARKET", "GLOBAL", "US MARKET", "US"}:
        if sym in _GLOBAL_EQUITIES:
            return "GLOBAL_EQUITY"
        if sym in _GLOBAL_INDEX_OR_MACRO:
            return "GLOBAL_INDEX_OR_MACRO"
        if sym in _GLOBAL_COMMODITIES:
            return "GLOBAL_COMMODITY"
        return UNKNOWN_MARKET_GROUP

    if not raw:
        if sym in _INDIA_INDEXES:
            return "INDIA_INDEX"
        if sym in _GLOBAL_EQUITIES:
            return "GLOBAL_EQUITY"
        if sym in _GLOBAL_INDEX_OR_MACRO:
            return "GLOBAL_INDEX_OR_MACRO"
        return UNKNOWN_MARKET_GROUP

    return raw


def identity_from_values(
    *,
    portfolio_id: str,
    symbol: Optional[str],
    market_group: Optional[str] = None,
    contract_month: Optional[str] = None,
    option_type: Optional[str] = None,
    strike_price=None,
    direction: Optional[str] = None,
) -> PositionIdentity:
    supplied: set[str] = set()
    unresolved: set[str] = set()

    sym = canonical_symbol(symbol)
    market = canonical_market_group(market_group, sym)
    contract, contract_unresolved = canonical_contract_month(contract_month)
    opt = canonical_option_type(option_type)
    strike = canonical_decimal_string(strike_price)
    side = canonical_direction(direction)

    if market_group is not None:
        supplied.add("market_group")
    if contract_month is not None:
        supplied.add("contract_month")
    if option_type is not None:
        supplied.add("option_type")
    if strike_price is not None:
        supplied.add("strike_price")
    if direction is not None:
        supplied.add("direction")
    if contract_unresolved:
        unresolved.add("contract_month")

    return PositionIdentity(
        portfolio_id=str(portfolio_id or "default"),
        market_group=market or UNKNOWN_MARKET_GROUP,
        symbol=sym,
        contract_month=contract,
        option_type=opt,
        strike_price=strike,
        direction=side,
        supplied_fields=frozenset(supplied),
        unresolved_fields=frozenset(unresolved),
    )


def identity_from_event(event: ParsedTradeEvent, portfolio_id: str) -> PositionIdentity:
    return identity_from_values(
        portfolio_id=portfolio_id,
        symbol=event.symbol,
        market_group=event.market_group,
        contract_month=event.contract_month,
        option_type=event.option_type,
        strike_price=event.strike_price,
        direction=event.direction,
    )


def canonicalize_position(position: PositionState) -> PositionState:
    identity = identity_from_values(
        portfolio_id=position.portfolio_id,
        symbol=position.symbol,
        market_group=position.market_group,
        contract_month=position.contract_month,
        option_type=position.option_type,
        strike_price=position.strike_price,
        direction=position.direction,
    )
    position.portfolio_id = identity.portfolio_id
    position.market_group = identity.market_group
    position.symbol = identity.symbol
    position.contract_month = identity.contract_month
    position.option_type = identity.option_type
    position.strike_price = Decimal(identity.strike_price) if identity.strike_price else None
    position.direction = identity.direction
    return position


def position_matches_supplied_fields(position: PositionState, identity: PositionIdentity) -> bool:
    pos_identity = identity_from_values(
        portfolio_id=position.portfolio_id,
        symbol=position.symbol,
        market_group=position.market_group,
        contract_month=position.contract_month,
        option_type=position.option_type,
        strike_price=position.strike_price,
        direction=position.direction,
    )
    for field_name in identity.supplied_fields:
        if getattr(pos_identity, field_name) != getattr(identity, field_name):
            return False
    return True


def resolve_from_candidates(
    identity: PositionIdentity,
    candidates: Iterable[PositionState],
) -> IdentityResolutionResult:
    fields_used = ["portfolio_id", "symbol", *sorted(identity.supplied_fields)]
    fields_missing = [
        field_name for field_name in (
            "market_group", "contract_month", "option_type", "strike_price", "direction"
        )
        if field_name not in identity.supplied_fields
    ]

    if identity.unresolved_fields:
        return IdentityResolutionResult(
            status=IdentityMatchStatus.IDENTITY_CONFLICT,
            candidate_count=0,
            explanation=f"Unresolved identity fields: {', '.join(sorted(identity.unresolved_fields))}",
            fields_used=fields_used,
            fields_missing=fields_missing,
        )

    filtered = [p for p in candidates if position_matches_supplied_fields(p, identity)]
    ids = [p.position_id for p in filtered if p.position_id is not None]
    if not filtered:
        return IdentityResolutionResult(
            status=IdentityMatchStatus.NO_MATCH,
            candidate_count=0,
            candidate_position_ids=ids,
            explanation="No open position matches the supplied identity fields.",
            fields_used=fields_used,
            fields_missing=fields_missing,
        )
    if len(filtered) > 1:
        return IdentityResolutionResult(
            status=IdentityMatchStatus.AMBIGUOUS_MATCH,
            candidate_count=len(filtered),
            candidate_position_ids=ids,
            explanation="Multiple open positions match the supplied identity fields.",
            fields_used=fields_used,
            fields_missing=fields_missing,
        )

    status = (
        IdentityMatchStatus.EXACT_MATCH
        if not fields_missing
        else IdentityMatchStatus.UNIQUE_FALLBACK_MATCH
    )
    return IdentityResolutionResult(
        status=status,
        matched_position=filtered[0],
        candidate_count=1,
        candidate_position_ids=ids,
        explanation=status.value,
        fields_used=fields_used,
        fields_missing=fields_missing,
    )


def audit_duplicate_identity_keys(positions: Iterable[PositionState]) -> list[dict]:
    grouped: dict[tuple[str, str, str, str, str, str, str], list[PositionState]] = {}
    for pos in positions:
        identity = identity_from_values(
            portfolio_id=pos.portfolio_id,
            symbol=pos.symbol,
            market_group=pos.market_group,
            contract_month=pos.contract_month,
            option_type=pos.option_type,
            strike_price=pos.strike_price,
            direction=pos.direction,
        )
        grouped.setdefault(identity.full_key(), []).append(pos)

    report = []
    for key, rows in grouped.items():
        if len(rows) > 1:
            report.append({
                "identity_key": key,
                "position_ids": [p.position_id for p in rows],
                "count": len(rows),
            })
    return report
