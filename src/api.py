"""
api.py
======
FastAPI REST interface for the Trade Message Parser and Holdings Engine.

Exposes the hybrid NLP parser, validation layer, and holdings engine as
stateless JSON endpoints suitable for microservice integration.

Endpoints
---------
    POST /api/v1/parse         — Parse a raw trade message → structured event
    GET  /api/v1/holdings      — List open positions for a portfolio
    GET  /api/v1/health        — Liveness / readiness probe

Usage
-----
    uvicorn src.api:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from src.config import DATABASE_PATH, DEFAULT_PORTFOLIO_ID, PARSER_VERSION
from src.database import get_connection, init_db
from src.hybrid_parser import HybridParser
from src.sqlite_repository import SQLitePositionRepository
from src.validator import validate


# ---------------------------------------------------------------------------
# Lifespan: ensure database is initialised on startup
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise SQLite database on application startup."""
    init_db(DATABASE_PATH)
    yield


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Trade Message Parser API",
    version="1.0.0",
    description=(
        "Hybrid NLP trade-broadcast parser with deterministic rule engine, "
        "scikit-learn classifier, and exact-Decimal holdings engine."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class ParseRequest(BaseModel):
    """Incoming message to parse."""
    raw_text: str = Field(..., description="Raw trade broadcast message text.")
    source_message_id: Optional[str] = Field(
        None, description="Unique message identifier for idempotency tracking."
    )
    segment: Optional[str] = Field(
        None, description="Market segment hint (e.g. NSE, MCX, GLOBAL_EQUITY)."
    )
    portfolio_id: str = Field(
        DEFAULT_PORTFOLIO_ID,
        description="Portfolio to check for existing holdings context.",
    )

    model_config = {"json_schema_extra": {
        "examples": [
            {
                "raw_text": "BUY 50% TSLA @250 SL 240 TGT 270",
                "source_message_id": "broadcast-001",
                "segment": "GLOBAL_EQUITY",
                "portfolio_id": "default",
            }
        ]
    }}


class ParseResponse(BaseModel):
    """Structured parse result."""
    source_message_id: Optional[str]
    parser_version: str
    timestamp: str
    prediction_type: str
    validation_status: str
    needs_review: bool
    auto_apply_eligible: bool
    event: dict[str, Any]
    validation_errors: list[str]
    validation_warnings: list[str]


class PositionResponse(BaseModel):
    """A single position entry."""
    symbol: str
    direction: Optional[str]
    market_group: Optional[str]
    current_allocation_pct: str
    average_entry_price: Optional[str]
    stop_loss: Optional[str]
    status: str


class HoldingsResponse(BaseModel):
    """Portfolio holdings summary."""
    portfolio_id: str
    count: int
    positions: list[PositionResponse]


class HealthResponse(BaseModel):
    """Service health status."""
    status: str
    parser_version: str
    database: str
    timestamp: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post(
    "/api/v1/parse",
    response_model=ParseResponse,
    summary="Parse a trade broadcast message",
    tags=["Parser"],
)
async def parse_message(request: ParseRequest) -> ParseResponse:
    """
    Parse a raw trade broadcast message into a structured event.

    The hybrid parser applies:
    1. Text normalisation
    2. TF-IDF + Logistic Regression ML classification
    3. Regex entity extraction (symbols, prices, targets, stop-loss)
    4. Deterministic rule engine override
    5. Validation layer with safety guards
    """
    parser = HybridParser()

    # Resolve holdings context if a database is available
    existing_long = False
    existing_short = False
    has_holdings_context = False

    try:
        conn = get_connection(DATABASE_PATH)
        repo = SQLitePositionRepository(conn)

        # Pre-parse to extract the symbol for context lookup
        pre_event = parser.parse(request.raw_text)
        symbol = pre_event.symbol or ""

        if symbol:
            long_pos = repo.get_position(
                request.portfolio_id, symbol, direction="LONG"
            )
            short_pos = repo.get_position(
                request.portfolio_id, symbol, direction="SHORT"
            )
            existing_long = bool(long_pos and long_pos.status == "OPEN")
            existing_short = bool(short_pos and short_pos.status == "OPEN")
            has_holdings_context = True
        conn.close()
    except Exception:
        pass  # Proceed without context — parser still works

    event = parser.parse(
        raw_text=request.raw_text,
        source_message_id=request.source_message_id,
        existing_long=existing_long,
        existing_short=existing_short,
        has_holdings_context=has_holdings_context,
    )

    validated = validate(event)

    validation_status = "VALID" if not validated.validation_errors else "INVALID"

    return ParseResponse(
        source_message_id=request.source_message_id,
        parser_version=PARSER_VERSION,
        timestamp=datetime.now(timezone.utc).isoformat(),
        prediction_type="event",
        validation_status=validation_status,
        needs_review=validated.needs_review,
        auto_apply_eligible=validated.auto_apply_eligible,
        event=validated.to_dict(),
        validation_errors=list(validated.validation_errors),
        validation_warnings=list(validated.validation_warnings),
    )


@app.get(
    "/api/v1/holdings",
    response_model=HoldingsResponse,
    summary="List open portfolio positions",
    tags=["Holdings"],
)
async def get_holdings(
    portfolio_id: str = Query(
        DEFAULT_PORTFOLIO_ID,
        description="Portfolio identifier.",
    ),
    include_closed: bool = Query(
        False,
        description="Include closed positions.",
    ),
) -> HoldingsResponse:
    """
    Return the current open (or all) positions for a portfolio.

    Allocation values are exact Decimal strings — no floating-point drift.
    """
    try:
        conn = get_connection(DATABASE_PATH)
        repo = SQLitePositionRepository(conn)

        if include_closed:
            positions = repo.list_all_positions(portfolio_id)
        else:
            positions = repo.list_open_positions(portfolio_id)

        conn.close()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Database error: {exc}",
        )

    return HoldingsResponse(
        portfolio_id=portfolio_id,
        count=len(positions),
        positions=[
            PositionResponse(
                symbol=p.symbol,
                direction=p.direction,
                market_group=p.market_group,
                current_allocation_pct=str(p.current_allocation_pct),
                average_entry_price=(
                    str(p.average_entry_price) if p.average_entry_price else None
                ),
                stop_loss=str(p.stop_loss) if p.stop_loss else None,
                status=p.status,
            )
            for p in positions
        ],
    )


@app.get(
    "/api/v1/health",
    response_model=HealthResponse,
    summary="Health check",
    tags=["Operations"],
)
async def health_check() -> HealthResponse:
    """Liveness probe — returns OK if the parser is loaded and DB reachable."""
    db_status = "unknown"
    try:
        conn = get_connection(DATABASE_PATH)
        conn.execute("SELECT 1")
        conn.close()
        db_status = "connected"
    except Exception as exc:
        db_status = f"error: {exc}"

    return HealthResponse(
        status="ok",
        parser_version=PARSER_VERSION,
        database=db_status,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
