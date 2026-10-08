"""
AutoFund — Paper Trading Engine
================================
Core class that manages a simulated portfolio with real market prices.
Designed to be called by AI agents (buy / sell / get_portfolio) or
via the CLI.

State is persisted to a JSON ledger so the portfolio survives restarts.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

from .allowed_assets import validate_ticker
from .market_hours import is_market_open

log = logging.getLogger("trader.engine")

DEFAULT_PORTFOLIO_DIR = Path(__file__).parent / "data"
DEFAULT_PORTFOLIO_PATH = DEFAULT_PORTFOLIO_DIR / "portfolio.json"
DEFAULT_INITIAL_CAPITAL = 100_000.0


class TradeError(Exception):
    """Raised when a trade cannot be executed."""


class PaperTrader:
    """
    Paper trading engine backed by live yfinance prices.

    Parameters
    ----------
    portfolio_path : str | Path, optional
        Location of the JSON ledger file.
        Defaults to ``trader/data/portfolio.json``.
    initial_capital : float, optional
        Starting cash balance.  Defaults to $100 000.
    """

    def __init__(
        self,
        portfolio_path: str | Path | None = None,
        initial_capital: float | None = None,
    ):
        self.portfolio_path = Path(portfolio_path or DEFAULT_PORTFOLIO_PATH)
        self.initial_capital = initial_capital or DEFAULT_INITIAL_CAPITAL
        self._state: dict = {}
        self._load_or_create()

    # ── Persistence ───────────────────────────────────────────────────────

    def _load_or_create(self) -> None:
        if self.portfolio_path.exists():
            with open(self.portfolio_path, "r", encoding="utf-8") as fh:
                self._state = json.load(fh)
            # Ensure pending_sells exists for portfolios saved before this field was added
            self._state.setdefault("pending_sells", [])
            log.info("Loaded portfolio from %s", self.portfolio_path)
        else:
            self._state = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "initial_capital": self.initial_capital,
                "cash": self.initial_capital,
                "positions": {},
                "transactions": [],
                "pending_sells": [],
            }
            self._save()
            log.info(
                "Created new portfolio with $%,.2f capital at %s",
                self.initial_capital,
                self.portfolio_path,
            )

    def _save(self) -> None:
        self.portfolio_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.portfolio_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._state, fh, indent=2, ensure_ascii=False)
        tmp.replace(self.portfolio_path)

    # ── Price fetching ────────────────────────────────────────────────────

    @staticmethod
    def _get_live_price(symbol: str) -> float:
        """Fetch the most recent price for *symbol* from yfinance."""
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}

        price = (
            info.get("currentPrice")
            or info.get("regularMarketPrice")
            or info.get("previousClose")
        )

        # Last resort: latest close from history
        if price is None:
            hist = ticker.history(period="5d")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])

        if price is None:
            raise TradeError(
                f"Could not determine a price for '{symbol}'. "
                "The ticker may be delisted or data is unavailable."
            )

        return float(price)

    # ── Condition evaluation helpers ───────────────────────────────────────

    OPERATORS = {
        "gte": lambda val, target: val >= target,
        "lte": lambda val, target: val <= target,
        "gt":  lambda val, target: val > target,
        "lt":  lambda val, target: val < target,
        "eq":  lambda val, target: val == target,
    }

    def _evaluate_condition(
        self, cond: dict, current_value: float
    ) -> bool:
        """Evaluate a single condition against a current value."""
        op_fn = self.OPERATORS.get(cond["operator"])
        if op_fn is None:
            log.warning("Unknown operator '%s' in condition %s", cond["operator"], cond.get("condition_id"))
            return False
        return op_fn(current_value, cond["target"])

    def _get_fundamental(self, symbol: str, metric: str) -> float | None:
        """Fetch a single fundamental metric from yfinance."""
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info or {}
            val = info.get(metric)
            return float(val) if val is not None else None
        except Exception as exc:
            log.warning("Failed to fetch fundamental '%s' for %s: %s", metric, symbol, exc)
            return None

    def _is_market_open_for_symbol(self, symbol: str) -> tuple[bool, str]:
        """Return (is_open, message)."""
        return is_market_open(symbol)

    def _condition_summary_for_position(
        self, pos_state: dict, current_price: float, total_portfolio_value: float
    ) -> dict:
        """Build condition_summary dict for a single position."""
        conditions = pos_state.get("sell_conditions", [])
        if not conditions:
            return {"total": 0, "triggered": 0, "approaching": 0, "distant": 0}

        avg_cost = pos_state["avg_cost"]
        peak_price = pos_state.get("peak_price", current_price)
        triggered = approaching = distant = 0

        for cond in conditions:
            if cond.get("triggered"):
                triggered += 1
                continue
            cond_type = cond.get("type")
            target = cond["target"]
            operator = cond["operator"]
            current_value: float | None = None

            if cond_type == "price_target":
                current_value = current_price
            elif cond_type == "pnl_pct":
                current_value = ((current_price - avg_cost) / avg_cost) * 100 if avg_cost else 0.0
            elif cond_type == "trailing_stop":
                drawdown = ((peak_price - current_price) / peak_price) * 100 if peak_price else 0.0
                current_value = drawdown
            elif cond_type == "weight_pct":
                current_value = (current_price * pos_state["shares"] / total_portfolio_value) * 100 if total_portfolio_value else 0.0
            elif cond_type == "fundamental":
                metric = cond.get("metric")
                if metric:
                    current_value = self._get_fundamental(pos_state["symbol"], metric)

            is_approaching = False
            if current_value is not None:
                effective_target = abs(target) if cond_type == "trailing_stop" else target
                if effective_target != 0:
                    if cond_type == "trailing_stop":
                        # Distance as % of threshold remaining
                        dist = abs((effective_target - current_value) / effective_target * 100)
                    elif operator in ("gte", "gt"):
                        dist = abs((target - current_value) / target * 100)
                    elif operator in ("lte", "lt"):
                        dist = abs((current_value - target) / target * 100)
                    else:
                        dist = None
                    if dist is not None and dist <= 5.0:
                        is_approaching = True

            if is_approaching:
                approaching += 1
            else:
                distant += 1

        return {
            "total": len(conditions),
            "triggered": triggered,
            "approaching": approaching,
            "distant": distant,
        }

    def _approaching_for_position(
        self, pos_state: dict, current_price: float, total_portfolio_value: float
    ) -> list[dict]:
        """Return list of approaching conditions with distance_pct for a position."""
        conditions = pos_state.get("sell_conditions", [])
        avg_cost = pos_state["avg_cost"]
        peak_price = pos_state.get("peak_price", current_price)
        result = []

        for cond in conditions:
            if cond.get("triggered"):
                continue
            cond_type = cond.get("type")
            target = cond["target"]
            operator = cond["operator"]
            current_value: float | None = None

            if cond_type == "price_target":
                current_value = current_price
            elif cond_type == "pnl_pct":
                current_value = ((current_price - avg_cost) / avg_cost) * 100 if avg_cost else 0.0
            elif cond_type == "trailing_stop":
                drawdown = ((peak_price - current_price) / peak_price) * 100 if peak_price else 0.0
                current_value = drawdown
            elif cond_type == "weight_pct":
                current_value = (current_price * pos_state["shares"] / total_portfolio_value) * 100 if total_portfolio_value else 0.0
            elif cond_type == "fundamental":
                metric = cond.get("metric")
                if metric:
                    current_value = self._get_fundamental(pos_state["symbol"], metric)

            if current_value is None:
                continue

            effective_target = abs(target) if cond_type == "trailing_stop" else target
            if effective_target == 0:
                continue

            if cond_type == "trailing_stop":
                distance_pct = abs((effective_target - current_value) / effective_target * 100)
            elif operator in ("gte", "gt"):
                distance_pct = abs((target - current_value) / target * 100)
            elif operator in ("lte", "lt"):
                distance_pct = abs((current_value - target) / target * 100)
            else:
                continue

            if distance_pct <= 5.0:
                severity = "high" if distance_pct <= 2.0 else "moderate" if distance_pct <= 4.0 else "low"
                result.append({
                    "condition_id": cond.get("condition_id"),
                    "type": cond_type,
                    "target": target,
                    "operator": operator,
                    "current": round(current_value, 4),
                    "distance_pct": round(distance_pct, 2),
                    "severity": severity,
                })

        return result

    # ── Sell condition monitoring ─────────────────────────────────────────

    def check_sell_conditions(self) -> dict:
        """
        Evaluate all sell conditions on all open positions.

        Returns a dict with:
          - ``checked_at``: ISO timestamp
          - ``positions_checked``: number of positions evaluated
          - ``conditions_triggered``: list of triggered condition details
          - ``conditions_approaching``: list of conditions within 5% of firing

        For each triggered condition where the market is open, the sell
        is executed automatically.
        """
        triggered: list[dict] = []
        approaching: list[dict] = []
        positions_checked = 0

        portfolio = self.get_portfolio()
        position_by_symbol = {p["symbol"]: p for p in portfolio["positions"]}

        for sym, pos_state in self._state["positions"].items():
            sell_conditions = pos_state.get("sell_conditions", [])
            if not sell_conditions:
                continue

            positions_checked += 1
            current_price = position_by_symbol.get(sym, {}).get("current_price") or self._get_live_price(sym)
            avg_cost = pos_state["avg_cost"]

            # Read peak_price BEFORE evaluating conditions.  We update it
            # AFTER evaluation so the trailing-stop drawdown is measured
            # from the *previous* peak — not the price we just fetched.
            peak_price = pos_state.get("peak_price", current_price)

            for cond in sell_conditions:
                if cond.get("triggered"):
                    continue

                cond_id = cond.get("condition_id", "unknown")
                cond_type = cond.get("type")
                target = cond["target"]
                operator = cond["operator"]
                hit = False
                current_value: float | None = None

                if cond_type == "price_target":
                    current_value = current_price
                    hit = self._evaluate_condition(cond, current_price)

                elif cond_type == "pnl_pct":
                    pnl_pct = ((current_price - avg_cost) / avg_cost) * 100 if avg_cost else 0.0
                    current_value = pnl_pct
                    hit = self._evaluate_condition(cond, pnl_pct)

                elif cond_type == "trailing_stop":
                    # trailing_stop: target is the drawdown % threshold (e.g. 10 = 10%)
                    # drawdown is always >= 0; condition fires when drawdown >= target.
                    drawdown = ((peak_price - current_price) / peak_price) * 100 if peak_price else 0.0
                    current_value = drawdown
                    # Normalise: regardless of how the operator/target were stored,
                    # the semantic is "sell when drawdown from peak >= threshold".
                    effective_target = abs(target)
                    hit = drawdown >= effective_target

                elif cond_type == "weight_pct":
                    total_value = portfolio["cash"] + sum(
                        p["market_value"] for p in portfolio["positions"]
                    )
                    weight = (current_price * pos_state["shares"] / total_value) * 100 if total_value else 0.0
                    current_value = weight
                    hit = self._evaluate_condition(cond, weight)

                elif cond_type == "fundamental":
                    metric = cond.get("metric")
                    if metric:
                        current_value = self._get_fundamental(sym, metric)
                        if current_value is not None:
                            hit = self._evaluate_condition(cond, current_value)

                # Distance to target (approaching check)
                if current_value is not None and not hit:
                    distance_pct = None
                    eff_target = abs(target) if cond_type == "trailing_stop" else target
                    if eff_target != 0:
                        if cond_type == "trailing_stop":
                            distance_pct = abs((eff_target - current_value) / eff_target * 100)
                        elif operator in ("gte", "gt"):
                            distance_pct = abs((target - current_value) / target * 100)
                        elif operator in ("lte", "lt"):
                            distance_pct = abs((current_value - target) / target * 100)
                    if distance_pct is not None and distance_pct <= 5.0:
                        approaching.append({
                            "symbol": sym,
                            "condition_id": cond_id,
                            "type": cond_type,
                            "target": target,
                            "operator": operator,
                            "current_value": round(current_value, 4),
                            "distance_pct": round(distance_pct, 2),
                        })

                if hit:
                    cond["triggered"] = True
                    cond["triggered_at"] = datetime.now(timezone.utc).isoformat()
                    triggered.append({
                        "symbol": sym,
                        "condition_id": cond_id,
                        "type": cond_type,
                        "target": target,
                        "operator": operator,
                        "triggered_at": cond["triggered_at"],
                        "sell_executed": False,
                        "sell_error": None,
                    })
                    self._save()

        # Update peak_price AFTER all conditions have been evaluated so
        # trailing-stop drawdown is measured from the *prior* peak.
        for sym, pos_state in self._state["positions"].items():
            current_price = position_by_symbol.get(sym, {}).get("current_price")
            if current_price is not None and current_price > pos_state.get("peak_price", 0):
                pos_state["peak_price"] = current_price

        # Persist peak_price updates so they survive process restarts.
        self._save()

        # Execute sells for triggered conditions where market is open
        sold_symbols: set[str] = set()  # track already-sold positions
        pending_queued_symbols: set[str] = set()  # avoid duplicate pending entries
        for trigger in triggered:
            sym = trigger["symbol"]
            if sym in sold_symbols:
                trigger["sell_executed"] = True
                trigger["sell_error"] = "already sold by earlier condition"
                continue
            mkt_open, mkt_msg = self._is_market_open_for_symbol(sym)
            if mkt_open:
                pos = self._state["positions"].get(sym)
                if pos:
                    try:
                        self.sell(
                            sym,
                            quantity=pos["shares"],
                            reason=f"Triggered: {trigger['type']} {trigger['operator']} {trigger['target']}",
                            force=True,
                        )
                        trigger["sell_executed"] = True
                        sold_symbols.add(sym)
                    except Exception as exc:
                        trigger["sell_error"] = str(exc)
                        # Queue for retry so the sell is not lost — the
                        # condition is already marked triggered and will
                        # not re-fire, so without this the sell is gone.
                        if sym not in pending_queued_symbols:
                            self._state.setdefault("pending_sells", []).append({
                                "symbol": sym,
                                "condition_id": trigger["condition_id"],
                                "condition_type": trigger["type"],
                                "target": trigger["target"],
                                "operator": trigger["operator"],
                                "reason": f"Triggered: {trigger['type']} {trigger['operator']} {trigger['target']}",
                                "queued_at": datetime.now(timezone.utc).isoformat(),
                            })
                            pending_queued_symbols.add(sym)
                            self._save()
            else:
                trigger["sell_error"] = f"market closed ({mkt_msg})"
                # Persist the pending sell so it survives restarts;
                # deduplicate per symbol so we don't queue redundant sells.
                if sym not in pending_queued_symbols:
                    self._state.setdefault("pending_sells", []).append({
                        "symbol": sym,
                        "condition_id": trigger["condition_id"],
                        "condition_type": trigger["type"],
                        "target": trigger["target"],
                        "operator": trigger["operator"],
                        "reason": f"Triggered: {trigger['type']} {trigger['operator']} {trigger['target']}",
                        "queued_at": datetime.now(timezone.utc).isoformat(),
                    })
                    pending_queued_symbols.add(sym)
                    self._save()

        # Flush any pending sells whose market is now open.
        # Skip symbols already sold during the triggered-sell phase above.
        pending = self._state.get("pending_sells", [])
        still_pending = []
        for ps in pending:
            sym = ps["symbol"]
            if sym in sold_symbols:
                # Already sold in this check cycle — drop the stale pending entry.
                continue
            mkt_open, _ = self._is_market_open_for_symbol(sym)
            if mkt_open:
                pos = self._state["positions"].get(sym)
                if pos:
                    try:
                        self.sell(
                            sym,
                            quantity=pos["shares"],
                            reason=ps.get("reason", "Pending sell executed"),
                            force=True,
                        )
                        sold_symbols.add(sym)
                    except Exception as exc:
                        log.warning("Pending sell for %s failed: %s", sym, exc)
                        still_pending.append(ps)
                # If position no longer exists, drop the pending sell
            else:
                still_pending.append(ps)
        if len(still_pending) != len(pending):
            self._state["pending_sells"] = still_pending
            self._save()

        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "positions_checked": positions_checked,
            "conditions_triggered": triggered,
            "conditions_approaching": approaching,
        }

    def get_condition_status(self, symbol: str) -> dict | None:
        """
        Return per-condition status for a single position.

        Includes ``condition_summary`` (total, triggered, approaching, distant)
        and a list of conditions with their current values and distance to target.
        """
        pos_state = self._state["positions"].get(symbol.upper().strip())
        if pos_state is None:
            return None

        sell_conditions = pos_state.get("sell_conditions", [])
        try:
            current_price = self._get_live_price(symbol)
        except TradeError:
            current_price = pos_state["avg_cost"]

        avg_cost = pos_state["avg_cost"]
        peak_price = pos_state.get("peak_price", current_price)
        portfolio = self.get_portfolio()
        total_value = portfolio["cash"] + sum(
            p["market_value"] for p in portfolio["positions"]
        )

        conditions_out = []
        triggered = 0
        approaching = 0

        for cond in sell_conditions:
            cond_type = cond.get("type")
            target = cond["target"]
            operator = cond["operator"]
            current_value: float | None = None
            distance_pct: float | None = None

            if cond_type == "price_target":
                current_value = current_price
            elif cond_type == "pnl_pct":
                current_value = ((current_price - avg_cost) / avg_cost) * 100 if avg_cost else 0.0
            elif cond_type == "trailing_stop":
                drawdown = ((peak_price - current_price) / peak_price) * 100 if peak_price else 0.0
                current_value = drawdown
            elif cond_type == "weight_pct":
                current_value = (current_price * pos_state["shares"] / total_value) * 100 if total_value else 0.0
            elif cond_type == "fundamental":
                metric = cond.get("metric")
                if metric:
                    current_value = self._get_fundamental(symbol, metric)

            if current_value is not None:
                effective_target = abs(target) if cond_type == "trailing_stop" else target
                if effective_target != 0:
                    if cond_type == "trailing_stop":
                        distance_pct = abs((effective_target - current_value) / effective_target * 100)
                    elif operator in ("gte", "gt"):
                        distance_pct = abs((target - current_value) / target * 100)
                    elif operator in ("lte", "lt"):
                        distance_pct = abs((current_value - target) / target * 100)

            status = "triggered" if cond.get("triggered") else (
                "approaching" if distance_pct is not None and distance_pct <= 5.0 else "distant"
            )
            if status == "triggered":
                triggered += 1
            elif status == "approaching":
                approaching += 1

            conditions_out.append({
                "condition_id": cond.get("condition_id"),
                "type": cond_type,
                "metric": cond.get("metric"),
                "target": target,
                "operator": operator,
                "reason": cond.get("reason"),
                "triggered": cond.get("triggered", False),
                "triggered_at": cond.get("triggered_at"),
                "current_value": round(current_value, 4) if current_value is not None else None,
                "distance_pct": round(distance_pct, 2) if distance_pct is not None else None,
                "status": status,
            })

        return {
            "symbol": symbol.upper().strip(),
            "condition_summary": {
                "total": len(sell_conditions),
                "triggered": triggered,
                "approaching": approaching,
                "distant": len(sell_conditions) - triggered - approaching,
            },
            "conditions": conditions_out,
        }

    def add_sell_condition(
        self, symbol: str, condition: dict
    ) -> dict | None:
        """
        Add a sell condition to an existing open position.

        Parameters
        ----------
        symbol : str
            Ticker symbol.
        condition : dict
            Condition dict with keys: type, target, operator, reason,
            and optionally metric (for fundamental type).

        Returns
        -------
        dict or None
            The processed condition with ID and timestamps, or None if
            the position doesn't exist.
        """
        symbol = symbol.upper().strip()
        pos = self._state["positions"].get(symbol)
        if pos is None:
            return None

        processed = {
            "condition_id": f"cond_{uuid.uuid4().hex[:8]}",
            "type": condition["type"],
            "target": float(condition["target"]),
            "operator": condition["operator"],
            "reason": condition.get("reason", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "triggered": False,
            "triggered_at": None,
        }
        if condition.get("metric"):
            processed["metric"] = condition["metric"]

        pos.setdefault("sell_conditions", []).append(processed)
        self._save()
        log.info(
            "Added sell condition %s to %s: %s %s %s",
            processed["condition_id"], symbol,
            condition["type"], condition["operator"], condition["target"],
        )
        return processed

    # ── Agent API: BUY ────────────────────────────────────────────────────

    def buy(
        self,
        symbol: str,
        quantity: int,
        reason: str = "",
        sell_conditions: list[dict] | None = None,
        force: bool = False,
    ) -> dict:
        """
        Execute a market BUY order.

        Parameters
        ----------
        symbol : str
            Ticker symbol (e.g. ``"AAPL"``, ``"GC=F"``).
        quantity : int
            Number of shares / contracts to buy.  Must be > 0.
        reason : str, optional
            Free-text rationale recorded in the transaction log.
        force : bool, optional
            If ``True``, skip the market-hours check (for testing).

        Returns
        -------
        dict
            The recorded transaction.

        Raises
        ------
        TradeError
            If the trade is invalid (bad ticker, market closed,
            insufficient cash, etc.).
        """
        symbol = symbol.upper().strip()
        quantity = int(quantity)

        if quantity <= 0:
            raise TradeError("Quantity must be a positive integer.")

        # Validate ticker
        valid, msg = validate_ticker(symbol)
        if not valid:
            raise TradeError(f"Ticker '{symbol}' rejected: {msg}")

        # Market hours
        if not force:
            mkt_open, mkt_msg = is_market_open(symbol)
            if not mkt_open:
                raise TradeError(
                    f"Cannot trade '{symbol}' right now: {mkt_msg}"
                )

        # Get live price
        price = self._get_live_price(symbol)
        total_cost = round(price * quantity, 2)

        # Cash check
        if total_cost > self._state["cash"]:
            raise TradeError(
                f"Insufficient cash.  "
                f"Order total: ${total_cost:,.2f}, "
                f"available: ${self._state['cash']:,.2f}."
            )

        # Build sell conditions with IDs and timestamps
        processed_conditions = []
        if sell_conditions:
            for cond in sell_conditions:
                processed = {
                    "condition_id": f"cond_{uuid.uuid4().hex[:8]}",
                    "type": cond["type"],
                    "target": float(cond["target"]),
                    "operator": cond["operator"],
                    "reason": cond.get("reason", ""),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "triggered": False,
                    "triggered_at": None,
                }
                if cond.get("metric"):
                    processed["metric"] = cond["metric"]
                processed_conditions.append(processed)

        # ── Execute ───────────────────────────────────────────────────────
        self._state["cash"] = round(self._state["cash"] - total_cost, 2)

        if symbol in self._state["positions"]:
            pos = self._state["positions"][symbol]
            new_shares = pos["shares"] + quantity
            new_total_cost = round(pos["total_cost"] + total_cost, 2)
            pos["shares"] = new_shares
            pos["total_cost"] = new_total_cost
            pos["avg_cost"] = round(new_total_cost / new_shares, 4)
            # Update peak_price if the new buy price is higher, so
            # trailing stops correctly track the high-water mark.
            if price > pos.get("peak_price", 0):
                pos["peak_price"] = price
            if processed_conditions:
                pos.setdefault("sell_conditions", []).extend(processed_conditions)
        else:
            self._state["positions"][symbol] = {
                "symbol": symbol,
                "shares": quantity,
                "avg_cost": round(price, 4),
                "total_cost": total_cost,
                "buy_reason": reason,
                "buy_date": datetime.now(timezone.utc).isoformat(),
                "peak_price": price,
                "sell_conditions": processed_conditions,
            }

        txn = self._record_transaction(
            "BUY", symbol, quantity, price, total_cost, reason,
        )
        self._save()

        log.info(
            "BUY  %d × %s @ $%.2f = $%s  |  Cash remaining: $%s",
            quantity, symbol, price,
            f"{total_cost:,.2f}", f"{self._state['cash']:,.2f}",
        )
        return txn

    # ── Agent API: SELL ───────────────────────────────────────────────────

    def sell(
        self,
        symbol: str,
        quantity: int,
        reason: str = "",
        force: bool = False,
    ) -> dict:
        """
        Execute a market SELL order.

        Parameters
        ----------
        symbol : str
            Ticker symbol.
        quantity : int
            Number of shares / contracts to sell.  Must be > 0.
        reason : str, optional
            Free-text rationale.
        force : bool, optional
            Skip market-hours check.

        Returns
        -------
        dict
            The recorded transaction.

        Raises
        ------
        TradeError
            If the trade is invalid.
        """
        symbol = symbol.upper().strip()
        quantity = int(quantity)

        if quantity <= 0:
            raise TradeError("Quantity must be a positive integer.")

        # Must own the position
        if symbol not in self._state["positions"]:
            raise TradeError(
                f"No position in '{symbol}'. Cannot sell what you don't own."
            )

        pos = self._state["positions"][symbol]
        if quantity > pos["shares"]:
            raise TradeError(
                f"Insufficient shares of '{symbol}'. "
                f"Own {pos['shares']}, trying to sell {quantity}."
            )

        # Validate ticker
        valid, msg = validate_ticker(symbol)
        if not valid:
            raise TradeError(f"Ticker '{symbol}' rejected: {msg}")

        # Market hours
        if not force:
            mkt_open, mkt_msg = is_market_open(symbol)
            if not mkt_open:
                raise TradeError(
                    f"Cannot trade '{symbol}' right now: {mkt_msg}"
                )

        # Get live price
        price = self._get_live_price(symbol)
        total_proceeds = round(price * quantity, 2)

        # ── Execute ───────────────────────────────────────────────────────
        self._state["cash"] = round(self._state["cash"] + total_proceeds, 2)

        if quantity == pos["shares"]:
            # Full liquidation — remove the position entirely
            del self._state["positions"][symbol]
        else:
            pos["shares"] -= quantity
            # Reduce total cost proportionally (keeps avg_cost unchanged)
            pos["total_cost"] = round(pos["avg_cost"] * pos["shares"], 2)

        txn = self._record_transaction(
            "SELL", symbol, quantity, price, total_proceeds, reason,
        )
        self._save()

        log.info(
            "SELL %d × %s @ $%.2f = $%s  |  Cash now: $%s",
            quantity, symbol, price,
            f"{total_proceeds:,.2f}", f"{self._state['cash']:,.2f}",
        )
        return txn

    # ── Agent API: Portfolio ──────────────────────────────────────────────

    def get_portfolio(self) -> dict:
        """
        Return the full portfolio state with live valuations.

        This method fetches current prices for every position, so it
        makes one yfinance call per holding.

        Returns
        -------
        dict
            Keys: ``cash``, ``positions`` (list), ``total_invested``,
            ``total_market_value``, ``total_value``, ``total_pnl``,
            ``total_return_pct``, ``transaction_count``.
        """
        positions_out: list[dict] = []
        total_market_value = 0.0
        total_cost_basis = 0.0

        # Fetch all prices in a single pass and cache them
        pos_prices: dict[str, float] = {}
        for sym, pos in self._state["positions"].items():
            try:
                pos_prices[sym] = self._get_live_price(sym)
            except TradeError:
                pos_prices[sym] = pos["avg_cost"]  # fallback

        # First pass: compute totals for portfolio-level metrics
        for sym, pos in self._state["positions"].items():
            current_price = pos_prices[sym]
            market_value = round(current_price * pos["shares"], 2)
            total_market_value += market_value
            total_cost_basis += pos["total_cost"]

        # total_value needed by helpers — compute before building output
        total_value = round(self._state["cash"] + total_market_value, 2)

        # Second pass: build position dicts (using cached prices)
        for sym, pos in self._state["positions"].items():
            current_price = pos_prices[sym]
            market_value = round(current_price * pos["shares"], 2)
            cost_basis = pos["total_cost"]
            pnl = round(market_value - cost_basis, 2)
            pnl_pct = round((pnl / cost_basis) * 100, 2) if cost_basis else 0.0

            positions_out.append({
                "symbol": sym,
                "shares": pos["shares"],
                "avg_cost": pos["avg_cost"],
                "current_price": current_price,
                "market_value": market_value,
                "cost_basis": cost_basis,
                "unrealized_pnl": pnl,
                "unrealized_pnl_pct": pnl_pct,
                "buy_reason": pos.get("buy_reason"),
                "buy_date": pos.get("buy_date"),
                "peak_price": pos.get("peak_price", current_price),
                "sell_conditions": pos.get("sell_conditions", []),
                "condition_summary": self._condition_summary_for_position(pos, current_price, total_value),
                "approaching_conditions": self._approaching_for_position(pos, current_price, total_value),
            })

        # Compute weights
        for p in positions_out:
            p["weight_pct"] = (
                round((p["market_value"] / total_value) * 100, 2)
                if total_value
                else 0.0
            )

        total_pnl = round(total_value - self._state["initial_capital"], 2)
        total_return_pct = (
            round((total_pnl / self._state["initial_capital"]) * 100, 2)
            if self._state["initial_capital"]
            else 0.0
        )

        return {
            "cash": self._state["cash"],
            "initial_capital": self._state["initial_capital"],
            "positions": positions_out,
            "total_invested": round(total_cost_basis, 2),
            "total_market_value": round(total_market_value, 2),
            "total_value": total_value,
            "total_pnl": total_pnl,
            "total_return_pct": total_return_pct,
            "transaction_count": len(self._state["transactions"]),
        }

    def get_position(self, symbol: str) -> dict | None:
        """
        Return details for a single position, or ``None`` if not held.
        """
        symbol = symbol.upper().strip()
        pos = self._state["positions"].get(symbol)
        if pos is None:
            return None

        try:
            current_price = self._get_live_price(symbol)
        except TradeError:
            current_price = pos["avg_cost"]

        market_value = round(current_price * pos["shares"], 2)
        pnl = round(market_value - pos["total_cost"], 2)
        pnl_pct = (
            round((pnl / pos["total_cost"]) * 100, 2)
            if pos["total_cost"]
            else 0.0
        )

        # Compute total portfolio value — fetch prices per-ticker with individual fallback
        pos_prices: dict[str, float] = {}
        for s, p in self._state["positions"].items():
            try:
                pos_prices[s] = self._get_live_price(s)
            except TradeError:
                pos_prices[s] = p["avg_cost"]
        total_value = self._state["cash"] + sum(
            round(pos_prices[s] * self._state["positions"][s]["shares"], 2) for s in self._state["positions"]
        )

        return {
            "symbol": symbol,
            "shares": pos["shares"],
            "avg_cost": pos["avg_cost"],
            "current_price": current_price,
            "market_value": market_value,
            "cost_basis": pos["total_cost"],
            "unrealized_pnl": pnl,
            "unrealized_pnl_pct": pnl_pct,
            "buy_reason": pos.get("buy_reason"),
            "buy_date": pos.get("buy_date"),
            "peak_price": pos.get("peak_price", current_price),
            "sell_conditions": pos.get("sell_conditions", []),
            "condition_summary": self._condition_summary_for_position(pos, current_price, total_value),
        }

    def get_transaction_history(self, limit: int = 50) -> list[dict]:
        """Return the most recent *limit* transactions (newest first)."""
        return list(reversed(self._state["transactions"][-limit:]))

    # ── Portfolio reset ───────────────────────────────────────────────────

    def reset(self, initial_capital: float | None = None) -> None:
        """Wipe all positions and transactions, restore initial cash."""
        cap = initial_capital or self.initial_capital
        self._state = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "initial_capital": cap,
            "cash": cap,
            "positions": {},
            "transactions": [],
            "pending_sells": [],
        }
        self._save()
        log.info("Portfolio reset to $%s.", f"{cap:,.2f}")

    # ── Internal helpers ──────────────────────────────────────────────────

    def _record_transaction(
        self,
        txn_type: str,
        symbol: str,
        shares: int,
        price: float,
        total: float,
        reason: str,
    ) -> dict:
        txn = {
            "id": f"txn_{uuid.uuid4().hex[:8]}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": txn_type,
            "symbol": symbol,
            "shares": shares,
            "price": round(price, 4),
            "total": round(total, 2),
            "reason": reason,
        }
        self._state["transactions"].append(txn)
        return txn
