from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal
from urllib.parse import urlparse

from sqlalchemy import String
from sqlalchemy.pool import StaticPool
from sqlmodel import Field, Session, SQLModel, create_engine, select

from stipulate import (
    Explorer,
    action,
    forbid_transition,
    from_entity,
    from_seed,
    invariant,
    postcondition,
    seed,
)
from stipulate.core.external import external
from stipulate.core.invariant import check_invariants
from stipulate.core.schema_check import check_schema
from stipulate.core.transitions import (
    check_forbidden_transitions,
    clear_transition_rules,
    diff_snapshots,
    ignore_transition,
    snapshot,
)
from stipulate.report import exploration_to_dict, mutation_to_dict
from stipulate.report.console import print_explore_result, print_mutation_result


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Order(SQLModel, table=True):
    __tablename__ = "order_demo_order"

    id: str = Field(primary_key=True)
    status: Literal[
        "draft", "placed", "paid", "shipped", "delivered", "cancelled", "refunded"
    ] = Field(default="draft", sa_type=String)
    total_cents: int = 0
    customer_email: str = "test@example.com"
    payment_captured: bool = False


class LineItem(SQLModel, table=True):
    __tablename__ = "order_demo_lineitem"

    id: str = Field(primary_key=True)
    order_id: str = Field(foreign_key="order_demo_order.id")
    product_name: str = ""
    quantity: int = 1
    unit_price_cents: int = 0
    fulfilled: bool = False


# ---------------------------------------------------------------------------
# Buggy business logic
# ---------------------------------------------------------------------------


def place_order(order_id: str, db: Session) -> None:
    """BUG: doesn't check if order has line items."""
    order = db.get(Order, order_id)
    order.status = "placed"
    db.commit()


def capture_payment(order_id: str, db: Session) -> None:
    """BUG: doesn't check if order is in 'placed' status."""
    order = db.get(Order, order_id)
    result = charge_card(order_id, order.total_cents)
    if result["charged"]:
        order.payment_captured = True
        order.status = "paid"
    db.commit()


def ship_order(order_id: str, db: Session) -> None:
    """BUG: doesn't check if payment is captured."""
    order = db.get(Order, order_id)
    order.status = "shipped"
    for item in db.exec(select(LineItem).where(LineItem.order_id == order_id)).all():
        item.fulfilled = True
    db.commit()


def deliver_order(order_id: str, db: Session) -> None:
    """BUG: doesn't check if order is 'shipped'."""
    order = db.get(Order, order_id)
    order.status = "delivered"
    db.commit()


def cancel_order(order_id: str, db: Session) -> None:
    """BUG: doesn't check if already shipped/delivered."""
    order = db.get(Order, order_id)
    order.status = "cancelled"
    db.commit()


def refund_order(order_id: str, db: Session) -> None:
    """BUG: doesn't check if payment was captured."""
    order = db.get(Order, order_id)
    order.status = "refunded"
    db.commit()


def delete_order(order_id: str, db: Session) -> None:
    """BUG: deletes order without cleaning up line items (orphan bug)."""
    order = db.get(Order, order_id)
    db.delete(order)
    db.commit()


# ---------------------------------------------------------------------------
# Fixed business logic
# ---------------------------------------------------------------------------


def place_order_fixed(order_id: str, db: Session) -> None:
    order = db.get(Order, order_id)
    if order is None or order.status != "draft":
        raise ValueError("order must be in draft status to place")
    items = db.exec(select(LineItem).where(LineItem.order_id == order_id)).all()
    if not items:
        raise ValueError("cannot place an order with no line items")
    order.status = "placed"
    db.commit()


def capture_payment_fixed(order_id: str, db: Session) -> None:
    order = db.get(Order, order_id)
    if order is None or order.status != "placed":
        raise ValueError("order must be in placed status to capture payment")
    try:
        result = charge_card(order_id, order.total_cents)
    except (TimeoutError, ConnectionError):
        return
    if result["charged"]:
        order.payment_captured = True
        order.status = "paid"
    db.commit()


def ship_order_fixed(order_id: str, db: Session) -> None:
    order = db.get(Order, order_id)
    if order is None or order.status != "paid":
        raise ValueError("order must be paid to ship")
    if not order.payment_captured:
        raise ValueError("payment must be captured before shipping")
    for item in db.exec(select(LineItem).where(LineItem.order_id == order_id)).all():
        item.fulfilled = True
    order.status = "shipped"
    db.commit()


def deliver_order_fixed(order_id: str, db: Session) -> None:
    order = db.get(Order, order_id)
    if order is None or order.status != "shipped":
        raise ValueError("order must be shipped to deliver")
    order.status = "delivered"
    db.commit()


def cancel_order_fixed(order_id: str, db: Session) -> None:
    order = db.get(Order, order_id)
    if order is None:
        raise ValueError("order not found")
    if order.status in ("shipped", "delivered"):
        raise ValueError("cannot cancel a shipped or delivered order")
    if order.status in ("cancelled", "refunded"):
        raise ValueError("order is already cancelled or refunded")
    order.status = "cancelled"
    db.commit()


def refund_order_fixed(order_id: str, db: Session) -> None:
    order = db.get(Order, order_id)
    if order is None:
        raise ValueError("order not found")
    if not order.payment_captured:
        raise ValueError("cannot refund an order with no captured payment")
    if order.status in ("refunded",):
        raise ValueError("order is already refunded")
    order.status = "refunded"
    db.commit()


def delete_order_fixed(order_id: str, db: Session) -> None:
    for item in db.exec(select(LineItem).where(LineItem.order_id == order_id)).all():
        db.delete(item)
    order = db.get(Order, order_id)
    if order is not None:
        db.delete(order)
    db.commit()


# ---------------------------------------------------------------------------
# External dependency
# ---------------------------------------------------------------------------


@external(
    outcomes={
        "success": {"charged": True, "txn_id": "txn_abc123"},
        "declined": {"charged": False, "reason": "card_declined"},
        "timeout": TimeoutError("payment gateway timeout"),
    }
)
def charge_card(order_id: str, amount_cents: int) -> dict[str, Any]:
    """Charge the customer's card via external payment gateway."""
    return {"charged": True, "txn_id": "txn_live_001"}


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


@invariant
def paid_orders_have_payment(db: Session) -> None:
    bad = db.exec(
        select(Order).where(
            Order.status.in_(["paid", "shipped", "delivered"]),
            Order.payment_captured == False,  # noqa: E712
        )
    ).all()
    assert len(bad) == 0, f"Orders in paid/shipped/delivered without payment captured: {[o.id for o in bad]}"


@invariant
def shipped_items_fulfilled(db: Session) -> None:
    for order in db.exec(
        select(Order).where(Order.status.in_(["shipped", "delivered"]))
    ).all():
        unfulfilled = db.exec(
            select(LineItem).where(
                LineItem.order_id == order.id,
                LineItem.fulfilled == False,  # noqa: E712
            )
        ).all()
        assert len(unfulfilled) == 0, (
            f"Order {order.id} is {order.status} but has unfulfilled items: "
            f"{[i.id for i in unfulfilled]}"
        )


@invariant
def order_total_matches_items(db: Session) -> None:
    for order in db.exec(select(Order)).all():
        items = db.exec(
            select(LineItem).where(LineItem.order_id == order.id)
        ).all()
        expected = sum(i.quantity * i.unit_price_cents for i in items)
        assert order.total_cents == expected, (
            f"Order {order.id} total_cents={order.total_cents} "
            f"but line items sum to {expected}"
        )


@invariant
def cancelled_not_fulfilled(db: Session) -> None:
    for order in db.exec(
        select(Order).where(Order.status == "cancelled")
    ).all():
        fulfilled = db.exec(
            select(LineItem).where(
                LineItem.order_id == order.id,
                LineItem.fulfilled == True,  # noqa: E712
            )
        ).all()
        assert len(fulfilled) == 0, (
            f"Cancelled order {order.id} has fulfilled items: {[i.id for i in fulfilled]}"
        )


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


@seed(Order)
def order_seed() -> Order:
    return Order(
        id="ord1",
        status="placed",
        total_cents=3500,
        customer_email="alice@example.com",
        payment_captured=False,
    )


@seed(LineItem)
def lineitem_seeds(order: Order) -> list[LineItem]:
    return [
        LineItem(
            id="li1",
            order_id=order.id,
            product_name="Widget",
            quantity=2,
            unit_price_cents=1000,
            fulfilled=False,
        ),
        LineItem(
            id="li2",
            order_id=order.id,
            product_name="Gadget",
            quantity=1,
            unit_price_cents=1500,
            fulfilled=False,
        ),
    ]


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

ALL_INVARIANTS = [
    paid_orders_have_payment,
    shipped_items_fulfilled,
    order_total_matches_items,
    cancelled_not_fulfilled,
]


def build_actions(*, fixed: bool = False) -> list[Any]:
    clear_transition_rules()

    # Forbidden transitions
    forbid_transition(Order.status, from_="delivered", to="draft")
    forbid_transition(Order.status, from_="delivered", to="placed")
    forbid_transition(Order.status, from_="delivered", to="cancelled")
    forbid_transition(Order.status, from_="shipped", to="draft")
    forbid_transition(Order.status, from_="shipped", to="placed")
    forbid_transition(Order.status, from_="cancelled", to="placed")
    forbid_transition(Order.status, from_="cancelled", to="paid")
    forbid_transition(Order.status, from_="cancelled", to="shipped")
    forbid_transition(Order.status, from_="refunded", to="placed")
    forbid_transition(Order.status, from_="refunded", to="paid")
    forbid_transition(Order.status, from_="refunded", to="shipped")

    # Ignore cleanup transitions
    ignore_transition(Order.status, from_="cancelled", to="draft")
    ignore_transition(Order.status, from_="refunded", to="draft")

    rejects = [ValueError] if fixed else []

    place_fn = place_order_fixed if fixed else place_order
    capture_fn = capture_payment_fixed if fixed else capture_payment
    ship_fn = ship_order_fixed if fixed else ship_order
    deliver_fn = deliver_order_fixed if fixed else deliver_order
    cancel_fn = cancel_order_fixed if fixed else cancel_order
    refund_fn = refund_order_fixed if fixed else refund_order
    delete_fn = delete_order_fixed if fixed else delete_order

    return [
        action(
            fn=place_fn,
            params={"order_id": from_seed(Order)},
            rejects=rejects,
            name="place_order",
        ),
        action(
            fn=capture_fn,
            params={"order_id": from_seed(Order)},
            rejects=rejects,
            name="capture_payment",
        ),
        action(
            fn=ship_fn,
            params={"order_id": from_seed(Order)},
            rejects=rejects,
            name="ship_order",
        ),
        action(
            fn=deliver_fn,
            params={"order_id": from_seed(Order)},
            rejects=rejects,
            name="deliver_order",
        ),
        action(
            fn=cancel_fn,
            params={"order_id": from_seed(Order)},
            rejects=rejects,
            name="cancel_order",
        ),
        action(
            fn=refund_fn,
            params={"order_id": from_seed(Order)},
            rejects=rejects,
            name="refund_order",
        ),
        action(
            fn=delete_fn,
            params={"order_id": from_seed(Order)},
            name="delete_order",
        ),
    ]


# ---------------------------------------------------------------------------
# Explorer helpers
# ---------------------------------------------------------------------------


def demo_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _explorer(
    db: Session,
    *,
    budget: int,
    max_depth: int,
    optimizer: str,
    fixed: bool,
) -> Explorer:
    actions = build_actions(fixed=fixed)
    capture_action = next(a for a in actions if a.name == "capture_payment")

    @postcondition(action=capture_action)
    def payment_captured_after_pay(db: Session, order_id: str) -> None:
        order = db.get(Order, order_id)
        if order is None:
            return
        if order.status == "paid":
            assert order.payment_captured, (
                f"order {order.id} status is 'paid' but payment_captured is False"
            )

    return Explorer(
        models=[Order, LineItem],
        actions=actions,
        invariants=ALL_INVARIANTS,
        postconditions=[payment_captured_after_pay],
        seeds=[order_seed, lineitem_seeds],
        db=db,
        budget=budget,
        max_depth=max_depth,
        optimizer=optimizer,
    )


def run_explore(*, budget: int, max_depth: int, optimizer: str, fixed: bool = False) -> Any:
    with demo_session() as db:
        return _explorer(
            db, budget=budget, max_depth=max_depth, optimizer=optimizer, fixed=fixed
        ).run()


def run_mutate(*, budget: int, max_depth: int, optimizer: str, fixed: bool = True) -> Any:
    with demo_session() as db:
        return _explorer(
            db, budget=budget, max_depth=max_depth, optimizer=optimizer, fixed=fixed
        ).mutate()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_demo() -> None:
    result = run_explore(budget=500, max_depth=4, optimizer="deterministic")

    # Should find forbidden transitions
    _require_transition(result, "Order.status", "delivered", "cancelled")
    _require_transition(result, "Order.status", "shipped", "placed")

    # Should find orphan (schema) violation
    assert any(
        violation.kind == "schema" and violation.name == "orphan_detection"
        for violation in result.violations
    ), "expected orphan_detection schema violation"

    # Should find invariant violations (cancelled_not_fulfilled or shipped_items_fulfilled)
    assert any(
        violation.kind == "custom"
        and violation.name in ("cancelled_not_fulfilled", "shipped_items_fulfilled")
        for violation in result.violations
    ), "expected at least one invariant violation"

    # External outcome coverage
    assert "charge_card" in result.external_coverage, "missing external coverage for charge_card"

    # Mutation testing
    mutation = run_mutate(budget=60, max_depth=4, optimizer="deterministic", fixed=True)
    assert mutation.score[1] > 0
    assert mutation.killed


def _require_transition(result: Any, name: str, from_: str, to: str) -> None:
    assert any(
        violation.kind == "forbidden"
        and violation.name == name
        and violation.details["from"] == from_
        and violation.details["to"] == to
        for violation in result.violations
    ), f"missing {name} {from_!r} -> {to!r}"


# ---------------------------------------------------------------------------
# Browser demo
# ---------------------------------------------------------------------------

_PLAY_ENGINE: Any = None
_PLAY_EVENTS: list[str] = []
_PLAY_FINDINGS: list[dict[str, Any]] = []


def serve_demo(host: str, port: int) -> None:
    global _PLAY_ENGINE
    _PLAY_ENGINE = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _reset_play_db()
    server = ThreadingHTTPServer((host, port), _DemoHandler)
    print(f"Order demo running at http://{host}:{port}")
    print("Use Ctrl-C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class _DemoHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._html(_INDEX_HTML)
            return
        if path == "/api/state":
            self._json(_state_payload())
            return
        if path == "/api/stipulate/explore":
            result = run_explore(budget=500, max_depth=4, optimizer="deterministic")
            self._json(_explore_summary(result))
            return
        if path == "/api/stipulate/mutate":
            result = run_mutate(budget=60, max_depth=4, optimizer="deterministic", fixed=True)
            self._json(_mutation_summary(result))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        handlers = {
            "/api/reset": lambda: _reset_play_db() or _state_payload(),
            "/api/place": lambda: _perform("place_order()", lambda db: place_order("ord1", db)),
            "/api/capture": lambda: _perform("capture_payment()", lambda db: capture_payment("ord1", db)),
            "/api/ship": lambda: _perform("ship_order()", lambda db: ship_order("ord1", db)),
            "/api/deliver": lambda: _perform("deliver_order()", lambda db: deliver_order("ord1", db)),
            "/api/cancel": lambda: _perform("cancel_order()", lambda db: cancel_order("ord1", db)),
            "/api/refund": lambda: _perform("refund_order()", lambda db: refund_order("ord1", db)),
            "/api/delete": lambda: _perform("delete_order()", lambda db: delete_order("ord1", db)),
        }
        handler = handlers.get(path)
        if handler:
            self._json(handler())
            return
        self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _html(self, body: str) -> None:
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _json(self, data: Any, status: int = 200) -> None:
        encoded = json.dumps(data, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _reset_play_db() -> None:
    global _PLAY_EVENTS, _PLAY_FINDINGS
    build_actions()
    SQLModel.metadata.drop_all(_PLAY_ENGINE)
    SQLModel.metadata.create_all(_PLAY_ENGINE)
    with Session(_PLAY_ENGINE) as db:
        order = order_seed()
        db.add(order)
        db.flush()
        for item in lineitem_seeds(order):
            db.add(item)
        db.commit()
    _PLAY_EVENTS = ["seeded order with 2 line items"]
    _PLAY_FINDINGS = []


def _perform(label: str, fn: Any) -> dict[str, Any]:
    with Session(_PLAY_ENGINE) as db:
        before = snapshot(db, [Order, LineItem])
        try:
            fn(db)
            _PLAY_EVENTS.append(label)
        except Exception as exc:
            _PLAY_EVENTS.append(f"{label} raised {type(exc).__name__}: {exc}")
            db.rollback()
        after = snapshot(db, [Order, LineItem])
        events = diff_snapshots(before, after)
        failures = check_forbidden_transitions(events)
        failures.extend(check_schema(db, [Order, LineItem]))
        failures.extend(check_invariants(db, ALL_INVARIANTS))
        for failure in failures:
            item = _failure_payload(failure, label)
            if item not in _PLAY_FINDINGS:
                _PLAY_FINDINGS.append(item)
    return _state_payload()


def _state_payload() -> dict[str, Any]:
    with Session(_PLAY_ENGINE) as db:
        order = db.get(Order, "ord1")
        items = db.exec(select(LineItem).order_by(LineItem.id)).all()
        orphan_count = sum(1 for i in items if db.get(Order, i.order_id) is None)
        return {
            "order": (
                {
                    "id": order.id,
                    "status": order.status,
                    "total_cents": order.total_cents,
                    "customer_email": order.customer_email,
                    "payment_captured": order.payment_captured,
                }
                if order is not None
                else None
            ),
            "items": [
                {
                    "id": i.id,
                    "product_name": i.product_name,
                    "quantity": i.quantity,
                    "unit_price_cents": i.unit_price_cents,
                    "fulfilled": i.fulfilled,
                }
                for i in items
            ],
            "orphan_count": orphan_count,
            "events": _PLAY_EVENTS[-8:],
            "findings": _PLAY_FINDINGS[-8:],
        }


def _failure_payload(failure: Any, label: str) -> dict[str, Any]:
    return {
        "kind": failure.kind,
        "name": failure.name,
        "message": failure.message,
        "after": label,
        "shrunk": getattr(failure, "shrunk", False),
    }


def _explore_summary(result: Any) -> dict[str, Any]:
    violated_keys: set[tuple[str, str, str]] = set()
    for v in result.violations:
        if v.kind == "forbidden":
            violated_keys.add((v.name, v.details.get("from", ""), v.details.get("to", "")))

    transition_coverage: dict[str, Any] = {}
    for field_name, cov in result.coverage.items():
        pairs: list[dict[str, str]] = []
        for pair in cov.get("observed", []):
            pairs.append({"from": pair[0], "to": pair[1], "status": "observed"})
        for pair in cov.get("unseen", []):
            pairs.append({"from": pair[0], "to": pair[1], "status": "unseen"})
        for pair in cov.get("forbidden", []):
            status = "violated" if (field_name, pair[0], pair[1]) in violated_keys else "forbidden"
            pairs.append({"from": pair[0], "to": pair[1], "status": status})
        for pair in cov.get("ignored", []):
            pairs.append({"from": pair[0], "to": pair[1], "status": "ignored"})
        transition_coverage[field_name] = {
            "pairs": pairs,
            "observed_count": cov.get("observed_count", 0),
            "denominator": cov.get("denominator", 0),
        }

    invariant_exercise: dict[str, Any] = {}
    for name, count in result.invariant_coverage.items():
        violations = sum(1 for v in result.violations if v.kind == "invariant" and v.name == name)
        invariant_exercise[name] = {"checked": count, "violations": violations}

    external_coverage: dict[str, Any] = {}
    for name, counts in result.external_coverage.items():
        external_coverage[name] = {
            "outcomes": {outcome: count for outcome, count in sorted(counts.items())},
        }
    external_cross: dict[str, Any] = {}
    for name, counts in result.external_cross_coverage.items():
        external_cross[name] = {key: count for key, count in sorted(counts.items())}

    return {
        "steps": result.steps_executed,
        "violations": [
            {
                "kind": v.kind,
                "name": v.name,
                "message": v.message,
                "sequence": list(v.sequence),
                "shrunk": v.shrunk,
            }
            for v in result.violations
        ],
        "coverage": result.coverage,
        "mode_coverage": result.mode_coverage,
        "action_writes": result.action_writes,
        "transition_coverage": transition_coverage,
        "invariant_exercise": invariant_exercise,
        "external_coverage": external_coverage,
        "external_cross_coverage": external_cross,
    }


def _mutation_summary(result: Any) -> dict[str, Any]:
    return {
        "score": {
            "killed": result.score[0],
            "total": result.score[1],
            "percent": result.score_percent,
        },
        "killed": [
            {
                "description": item.mutant.description,
                "caught_by": ", ".join(sorted({v.name for v in item.violations})) or "violation",
            }
            for item in result.killed
        ],
        "survived": [
            {
                "description": item.mutant.description,
                "suggestion": item.suggestion,
            }
            for item in result.survived[:8]
        ],
    }


# ---------------------------------------------------------------------------
# Inline HTML
# ---------------------------------------------------------------------------

_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stipulate &mdash; Order Demo</title>
<style>
*,*::before,*::after{box-sizing:border-box}
body{
  margin:0;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Oxygen,Ubuntu,sans-serif;
  background:#f7f8fa;color:#1a1d26;line-height:1.5;-webkit-font-smoothing:antialiased;
}
.container{max-width:1120px;margin:0 auto;padding:0 24px 64px}
.header{padding:32px 0 20px;margin-bottom:0}
.header h1{font-size:22px;font-weight:700;letter-spacing:-0.02em;margin:0}
.header h1 span{color:#4f46e5}
.header p{color:#6b7280;font-size:14px;margin:4px 0 0}
.tab-bar{display:flex;gap:0;border-bottom:1px solid #e5e7eb;margin-bottom:24px}
.tab-btn{
  padding:10px 20px;font-size:14px;font-weight:500;color:#6b7280;
  background:none;border:none;border-bottom:2px solid transparent;
  cursor:pointer;transition:color 0.15s,border-color 0.15s;
}
.tab-btn:hover{color:#374151}
.tab-btn.active{color:#4f46e5;border-bottom-color:#4f46e5}
.tab-pane{display:none}
.tab-pane.active{display:block;animation:fadeIn 0.25s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.play-grid{display:grid;grid-template-columns:1fr 1fr;gap:32px;align-items:start}
@media(max-width:800px){.play-grid{grid-template-columns:1fr}}
.order-card{
  background:#fff;border:1px solid #e5e7eb;border-radius:12px;
  padding:20px;margin-bottom:16px;
}
.order-card h2{margin:0 0 12px;font-size:16px;font-weight:600}
.status-row{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.status-chip{
  display:inline-flex;align-items:center;gap:4px;
  padding:4px 12px;border-radius:20px;
  font-size:13px;font-weight:600;
  background:#f3f4f6;color:#374151;border:1px solid #e5e7eb;
}
.status-chip.red{background:#fef2f2;color:#b91c1c;border-color:#fecaca}
.status-chip.green{background:#f0fdf4;color:#15803d;border-color:#bbf7d0}
.status-chip.blue{background:#eff6ff;color:#1e40af;border-color:#bfdbfe}
.status-chip.yellow{background:#fffbeb;color:#92400e;border-color:#fde68a}
.item-table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
.item-table th{text-align:left;padding:6px 8px;border-bottom:2px solid #e5e7eb;font-weight:600;color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:0.05em}
.item-table td{padding:6px 8px;border-bottom:1px solid #f3f4f6}
.btn-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
.btn{
  padding:7px 14px;font-size:13px;font-weight:500;
  border-radius:6px;border:1px solid #d1d5db;
  background:#fff;color:#374151;cursor:pointer;transition:all 0.12s;
}
.btn:hover{background:#f9fafb;border-color:#9ca3af}
.btn.danger{color:#b91c1c;border-color:#fca5a5}
.btn.danger:hover{background:#fef2f2}
.btn.primary{background:#4f46e5;color:#fff;border-color:#4338ca}
.btn.primary:hover{background:#4338ca}
.btn.primary:disabled{opacity:0.55;cursor:not-allowed}
.btn.outline{background:transparent}
.panel{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:20px;margin-bottom:16px}
.panel h2{font-size:15px;font-weight:600;margin:0 0 12px;color:#111827}
.item-list{display:grid;gap:6px}
.item{
  padding:10px 12px;border-radius:6px;font-size:13px;
  line-height:1.4;border:1px solid #e5e7eb;background:#fafbfc;
}
.item strong{display:block;margin-bottom:2px}
.item.bad{border-left:3px solid #dc2626;background:#fef2f2}
.item.bad strong{color:#b91c1c}
.item.neutral{border-left:3px solid #94a3b8;background:#f9fafb}
.empty-state{text-align:center;padding:32px 16px;color:#9ca3af;font-size:14px}
.section-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;gap:16px}
.section-header div{flex:1}
.section-header h2{margin:0;font-size:18px;font-weight:600}
.section-header .panel-desc{margin:4px 0 0;color:#6b7280;font-size:13px}
.stats-bar{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}
.stat-card{flex:1;min-width:130px;padding:16px;background:#fff;border:1px solid #e5e7eb;border-radius:10px}
.stat-card .label{font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:0.06em}
.stat-card .value{font-size:28px;font-weight:700;margin-top:4px;letter-spacing:-0.02em}
.stat-card .value.red{color:#dc2626}
.stat-card .value.green{color:#16a34a}
.stat-card .value.blue{color:#2563eb}
.violation-card{padding:16px;border:1px solid #fecaca;border-radius:8px;background:#fff;margin-bottom:10px}
.violation-card .v-header{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.badge{padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.04em}
.badge.forbidden{background:#fef2f2;color:#b91c1c}
.badge.schema{background:#fffbeb;color:#92400e}
.badge.invariant{background:#eff6ff;color:#1e40af}
.badge.postcondition{background:#f5f3ff;color:#5b21b6}
.badge.external{background:#fdf4ff;color:#86198f}
.v-name{font-weight:600;font-size:14px;color:#111827}
.v-msg{font-size:13px;color:#4b5563;margin-bottom:8px}
.v-seq{font-size:12px;font-family:'SF Mono',SFMono-Regular,Consolas,monospace;background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;padding:10px 12px;color:#374151}
.v-seq .step{padding:2px 0}
.v-seq .step-num{color:#9ca3af;margin-right:8px;font-size:11px}
.shrunk-tag{font-size:11px;color:#6b7280;font-style:italic;margin-top:6px}
.transition-section{margin-bottom:16px}
.transition-section h3{font-size:14px;font-weight:600;margin:0 0 6px}
.transition-section .summary{font-size:13px;color:#6b7280;margin-bottom:8px}
.pair-list{display:grid;gap:3px}
.pair-row{display:flex;align-items:center;gap:8px;padding:5px 10px;border-radius:4px;font-size:13px;font-family:'SF Mono',SFMono-Regular,Consolas,monospace}
.pair-row.observed{background:#f0fdf4;color:#166534}
.pair-row.unseen{background:#f9fafb;color:#9ca3af}
.pair-row.violated{background:#fef2f2;color:#b91c1c;font-weight:600}
.pair-row.forbidden{background:#f9fafb;color:#9ca3af;text-decoration:line-through}
.pair-row.ignored{background:#f9fafb;color:#d1d5db;font-style:italic}
.pair-row .arrow{margin:0 2px;color:#9ca3af;text-decoration:none !important}
.pair-row .status-label{margin-left:auto;font-size:11px;font-weight:500;font-family:-apple-system,sans-serif;text-decoration:none !important}
.score-hero{text-align:center;padding:32px 24px;background:#fff;border:1px solid #e5e7eb;border-radius:12px;margin-bottom:20px}
.score-hero .pct{font-size:64px;font-weight:800;letter-spacing:-0.04em;line-height:1}
.score-hero .pct.high{color:#16a34a}
.score-hero .pct.mid{color:#d97706}
.score-hero .pct.low{color:#dc2626}
.score-hero .detail{font-size:15px;color:#6b7280;margin-top:8px}
.mutant-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media(max-width:700px){.mutant-grid{grid-template-columns:1fr}}
.mutant-section h3{font-size:14px;font-weight:600;margin:0 0 10px;display:flex;align-items:center;gap:6px}
.mutant-card{padding:12px;border-radius:6px;margin-bottom:8px;font-size:13px;line-height:1.4}
.mutant-card.killed{background:#f0fdf4;border:1px solid #bbf7d0}
.mutant-card .mc-label{font-weight:600}
.mutant-card.killed .mc-label{color:#166534}
.mutant-card .caught{color:#15803d;font-size:12px;margin-top:4px}
.mutant-card.survived{background:#fffbeb;border:1px solid #fde68a}
.mutant-card.survived .mc-label{color:#92400e}
.mutant-card .suggestion{color:#78716c;font-size:12px;margin-top:4px}
.loading{display:flex;flex-direction:column;align-items:center;gap:12px;padding:48px 16px;color:#6b7280;font-size:14px}
.spinner{width:24px;height:24px;border:3px solid #e5e7eb;border-top-color:#4f46e5;border-radius:50%;animation:spin 0.7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.inv-table{width:100%;border-collapse:collapse;font-size:13px}
.inv-table th{text-align:left;padding:8px 10px;border-bottom:2px solid #e5e7eb;font-weight:600;color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:0.05em}
.inv-table td{padding:8px 10px;border-bottom:1px solid #f3f4f6}
.detail-block{margin-bottom:12px}
.detail-block .detail-label{font-size:11px;font-weight:600;color:#6b7280;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:4px}
.detail-block .detail-row{font-size:13px;color:#374151;padding:1px 0}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1><span>stipulate</span> order system</h1>
    <p>Order &rarr; Payment &rarr; Fulfillment lifecycle exploration</p>
  </div>

  <nav class="tab-bar" id="tabBar">
    <button class="tab-btn active" data-tab="play">Play</button>
    <button class="tab-btn" data-tab="explore">Explore</button>
    <button class="tab-btn" data-tab="mutate">Mutate</button>
  </nav>

  <div class="tab-pane active" id="pane-play">
    <div class="play-grid">
      <div>
        <div class="order-card" id="orderCard">
          <h2>Order <code id="orderId">---</code></h2>
          <div class="status-row">
            <span class="status-chip" id="orderStatus">loading</span>
            <span class="status-chip" id="paymentChip" style="display:none">payment: ---</span>
            <span class="status-chip" id="orphanChip" style="display:none">orphans: 0</span>
          </div>
          <div id="totalLine" style="font-size:14px;font-weight:600;margin-bottom:8px"></div>
          <table class="item-table">
            <thead><tr><th>Product</th><th>Qty</th><th>Price</th><th>Fulfilled</th></tr></thead>
            <tbody id="itemRows"></tbody>
          </table>
        </div>

        <div class="btn-row">
          <button class="btn" onclick="post('/api/place')">Place Order</button>
          <button class="btn" onclick="post('/api/capture')">Capture Payment</button>
          <button class="btn" onclick="post('/api/ship')">Ship</button>
          <button class="btn" onclick="post('/api/deliver')">Deliver</button>
          <button class="btn" onclick="post('/api/cancel')">Cancel</button>
          <button class="btn" onclick="post('/api/refund')">Refund</button>
          <button class="btn danger" onclick="post('/api/delete')">Delete Order</button>
          <button class="btn" onclick="post('/api/reset')">Reset</button>
        </div>
        <div class="btn-row" style="margin-top:6px">
          <button class="btn outline" onclick="scenario('payDraft')">Pay a draft</button>
          <button class="btn outline" onclick="scenario('shipUnpaid')">Ship unpaid</button>
          <button class="btn outline" onclick="scenario('cancelDelivered')">Cancel delivered</button>
          <button class="btn outline" onclick="scenario('refundUnpaid')">Refund unpaid</button>
          <button class="btn outline" onclick="scenario('orphan')">Orphan rows</button>
        </div>
      </div>
      <div>
        <div class="panel">
          <h2>Live Findings <span class="status-chip" id="findingCount" style="font-size:11px;padding:2px 8px">0</span></h2>
          <div class="item-list" id="liveFindings">
            <div class="empty-state">Interact with the order to trigger invariant checks</div>
          </div>
        </div>
        <div class="panel">
          <h2>Event Log</h2>
          <div class="item-list" id="events"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="tab-pane" id="pane-explore">
    <div class="section-header">
      <div>
        <h2>Exploration</h2>
        <p class="panel-desc">Automatically discover invariant violations and measure transition coverage.</p>
      </div>
      <button class="btn primary" id="exploreBtn" onclick="runExplore()">Run Exploration</button>
    </div>
    <div id="exploreResults"><div class="empty-state">Click &ldquo;Run Exploration&rdquo; to start</div></div>
  </div>

  <div class="tab-pane" id="pane-mutate">
    <div class="section-header">
      <div>
        <h2>Mutation Testing</h2>
        <p class="panel-desc">Test invariant strength against automatically generated code mutations.</p>
      </div>
      <button class="btn primary" id="mutateBtn" onclick="runMutate()">Run Mutation Testing</button>
    </div>
    <div id="mutateResults"><div class="empty-state">Click &ldquo;Run Mutation Testing&rdquo; to start</div></div>
  </div>
</div>

<script>
document.getElementById('tabBar').addEventListener('click', function(e) {
  var btn = e.target.closest('.tab-btn');
  if (!btn) return;
  var tab = btn.dataset.tab;
  document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.toggle('active', b === btn); });
  document.querySelectorAll('.tab-pane').forEach(function(p) { p.classList.toggle('active', p.id === 'pane-' + tab); });
});

async function loadState() {
  var r = await fetch('/api/state');
  render(await r.json());
}

async function post(path) {
  var r = await fetch(path, {method: 'POST'});
  render(await r.json());
}

async function scenario(name) {
  await post('/api/reset');
  var steps = {
    payDraft: ['/api/capture'],
    shipUnpaid: ['/api/ship'],
    cancelDelivered: ['/api/capture', '/api/ship', '/api/deliver', '/api/cancel'],
    refundUnpaid: ['/api/refund'],
    orphan: ['/api/delete']
  }[name];
  for (var i = 0; i < steps.length; i++) await post(steps[i]);
}

function render(data) {
  var order = data.order;
  document.getElementById('orderId').textContent = order ? order.id : 'DELETED';

  var gs = document.getElementById('orderStatus');
  var status = order ? order.status : 'deleted';
  gs.textContent = status;
  var colorMap = {draft:'',placed:'blue',paid:'blue',shipped:'yellow',delivered:'green',cancelled:'red',refunded:'red',deleted:'red'};
  gs.className = 'status-chip' + (colorMap[status] ? ' ' + colorMap[status] : '');

  var pc = document.getElementById('paymentChip');
  if (order) {
    pc.style.display = '';
    pc.textContent = 'payment: ' + (order.payment_captured ? 'captured' : 'none');
    pc.className = 'status-chip' + (order.payment_captured ? ' green' : '');
  } else {
    pc.style.display = 'none';
  }

  var oc = document.getElementById('orphanChip');
  if (data.orphan_count > 0) {
    oc.style.display = '';
    oc.textContent = 'orphans: ' + data.orphan_count;
    oc.className = 'status-chip red';
  } else {
    oc.style.display = 'none';
  }

  document.getElementById('totalLine').textContent = order
    ? 'Total: $' + (order.total_cents / 100).toFixed(2)
    : '';

  var tbody = document.getElementById('itemRows');
  tbody.innerHTML = '';
  data.items.forEach(function(item) {
    var tr = document.createElement('tr');
    tr.innerHTML = '<td>' + esc(item.product_name) + '</td>'
      + '<td>' + item.quantity + '</td>'
      + '<td>$' + (item.unit_price_cents / 100).toFixed(2) + '</td>'
      + '<td>' + (item.fulfilled ? 'Yes' : 'No') + '</td>';
    tbody.appendChild(tr);
  });

  var fc = document.getElementById('findingCount');
  fc.textContent = data.findings.length;
  fc.className = 'status-chip' + (data.findings.length > 0 ? ' red' : '');

  var lf = document.getElementById('liveFindings');
  if (data.findings.length) {
    lf.innerHTML = data.findings.map(function(f) {
      return '<div class="item bad"><strong>' + esc(f.kind) + ': ' + esc(f.name) + '</strong>'
        + esc(f.message) + '<br><span style="color:#9ca3af;font-size:12px">after ' + esc(f.after) + '</span>'
        + (f.shrunk ? '<br><span style="color:#9ca3af;font-size:11px;font-style:italic">sequence was shrunk</span>' : '')
        + '</div>';
    }).join('');
  } else {
    lf.innerHTML = '<div class="empty-state">No violations detected yet</div>';
  }

  var ev = document.getElementById('events');
  ev.innerHTML = data.events.map(function(e) {
    return '<div class="item neutral"><strong>' + esc(e) + '</strong></div>';
  }).join('');
}

async function runExplore() {
  var btn = document.getElementById('exploreBtn');
  btn.disabled = true; btn.textContent = 'Running\u2026';
  document.getElementById('exploreResults').innerHTML =
    '<div class="loading"><div class="spinner"></div>Running exploration\u2026</div>';
  try {
    var r = await fetch('/api/stipulate/explore');
    renderExploreResults(await r.json());
  } catch(e) {
    document.getElementById('exploreResults').innerHTML =
      '<div class="item bad"><strong>Error</strong>' + esc(e.message) + '</div>';
  }
  btn.disabled = false; btn.textContent = 'Run Exploration';
}

function renderExploreResults(data) {
  var html = '';
  var vc = data.violations.length;
  var totalObs = 0, totalDenom = 0;
  if (data.transition_coverage) {
    for (var k in data.transition_coverage) {
      totalObs += data.transition_coverage[k].observed_count;
      totalDenom += data.transition_coverage[k].denominator;
    }
  }
  var covPct = totalDenom > 0 ? Math.round(totalObs / totalDenom * 100) : 0;

  html += '<div class="stats-bar">'
    + '<div class="stat-card"><div class="label">Steps Executed</div><div class="value blue">' + data.steps + '</div></div>'
    + '<div class="stat-card"><div class="label">Violations</div><div class="value ' + (vc > 0 ? 'red' : 'green') + '">' + vc + '</div></div>'
    + '<div class="stat-card"><div class="label">Transition Coverage</div><div class="value">' + totalObs + '<span style="color:#6b7280;font-weight:400">/' + totalDenom + '</span> <span style="font-size:15px;color:#6b7280;font-weight:400">(' + covPct + '%)</span></div></div>'
    + '</div>';

  if (data.violations.length > 0) {
    html += '<div class="panel"><h2>Violations</h2>';
    data.violations.forEach(function(v) {
      html += '<div class="violation-card">'
        + '<div class="v-header"><span class="badge ' + v.kind + '">' + v.kind + '</span>'
        + '<span class="v-name">' + esc(v.name) + '</span></div>'
        + '<div class="v-msg">' + esc(v.message) + '</div>';
      if (v.sequence && v.sequence.length > 0) {
        html += '<div class="v-seq">';
        v.sequence.forEach(function(s, i) {
          html += '<div class="step"><span class="step-num">' + (i + 1) + '.</span>' + esc(s) + '</div>';
        });
        html += '</div>';
      }
      if (v.shrunk) html += '<div class="shrunk-tag">sequence was shrunk to minimal reproducer</div>';
      html += '</div>';
    });
    html += '</div>';
  }

  if (data.transition_coverage) {
    html += '<div class="panel"><h2>Transition Coverage</h2>';
    for (var field in data.transition_coverage) {
      var tc = data.transition_coverage[field];
      html += '<div class="transition-section">'
        + '<h3>' + esc(field) + '</h3>'
        + '<div class="summary">' + tc.observed_count + ' observed / ' + tc.denominator + ' reportable pairs</div>'
        + '<div class="pair-list">';
      var order = {observed:0, violated:1, unseen:2, forbidden:3, ignored:4};
      var sorted = tc.pairs.slice().sort(function(a, b) { return (order[a.status]||5) - (order[b.status]||5); });
      var labels = {observed:'\u2713 observed', unseen:'\u00b7 unseen', violated:'\u2717 VIOLATED', forbidden:'\u2298 forbidden', ignored:'~ ignored'};
      sorted.forEach(function(p) {
        html += '<div class="pair-row ' + p.status + '">'
          + esc(p.from) + ' <span class="arrow">\u2192</span> ' + esc(p.to)
          + '<span class="status-label">' + (labels[p.status] || p.status) + '</span></div>';
      });
      html += '</div></div>';
    }
    html += '</div>';
  }

  if (data.invariant_exercise) {
    var invKeys = Object.keys(data.invariant_exercise);
    if (invKeys.length > 0) {
      html += '<div class="panel"><h2>Invariant Exercise</h2>'
        + '<table class="inv-table"><thead><tr><th>Invariant</th><th>Scenarios</th><th>Violations</th></tr></thead><tbody>';
      invKeys.forEach(function(name) {
        var inv = data.invariant_exercise[name];
        var style = inv.violations > 0 ? ' style="color:#b91c1c;font-weight:600"' : '';
        html += '<tr><td>' + esc(name) + '</td><td>' + inv.checked + '</td><td' + style + '>' + inv.violations + '</td></tr>';
      });
      html += '</tbody></table></div>';
    }
  }

  if (data.external_coverage && Object.keys(data.external_coverage).length > 0) {
    html += '<div class="panel"><h2>External Outcome Coverage</h2>';
    for (var extName in data.external_coverage) {
      var ext = data.external_coverage[extName];
      html += '<div class="transition-section"><h3>' + esc(extName) + '</h3>';
      html += '<div class="pair-list">';
      for (var outcome in ext.outcomes) {
        var cnt = ext.outcomes[outcome];
        html += '<div class="pair-row observed">' + esc(outcome) + '<span class="status-label">' + cnt + 'x</span></div>';
      }
      html += '</div></div>';
    }
    if (data.external_cross_coverage && Object.keys(data.external_cross_coverage).length > 0) {
      for (var crossName in data.external_cross_coverage) {
        var cross = data.external_cross_coverage[crossName];
        html += '<div class="transition-section"><h3>' + esc(crossName) + ' cross coverage (state \u00d7 outcome)</h3>';
        html += '<div class="pair-list">';
        for (var key in cross) {
          html += '<div class="pair-row observed">' + esc(key) + '<span class="status-label">' + cross[key] + 'x</span></div>';
        }
        html += '</div></div>';
      }
    }
    html += '</div>';
  }

  html += '<div class="panel"><h2>Exploration Details</h2>';
  if (data.mode_coverage) {
    html += '<div class="detail-block"><div class="detail-label">Mode Coverage</div>';
    for (var m in data.mode_coverage) html += '<div class="detail-row">' + esc(m) + ': ' + data.mode_coverage[m] + 'x</div>';
    html += '</div>';
  }
  if (data.action_writes) {
    html += '<div class="detail-block"><div class="detail-label">Action Writes</div>';
    for (var a in data.action_writes) {
      var w = data.action_writes[a];
      var fields = Object.keys(w).map(function(k) { return k + ': ' + w[k] + 'x'; }).join(', ');
      html += '<div class="detail-row">' + esc(a) + ' \u2014 ' + (fields || 'no writes') + '</div>';
    }
    html += '</div>';
  }
  html += '</div>';

  document.getElementById('exploreResults').innerHTML = html;
}

async function runMutate() {
  var btn = document.getElementById('mutateBtn');
  btn.disabled = true; btn.textContent = 'Running\u2026';
  document.getElementById('mutateResults').innerHTML =
    '<div class="loading"><div class="spinner"></div>Running mutation testing\u2026</div>';
  try {
    var r = await fetch('/api/stipulate/mutate');
    renderMutateResults(await r.json());
  } catch(e) {
    document.getElementById('mutateResults').innerHTML =
      '<div class="item bad"><strong>Error</strong>' + esc(e.message) + '</div>';
  }
  btn.disabled = false; btn.textContent = 'Run Mutation Testing';
}

function renderMutateResults(data) {
  var html = '';
  var pct = Math.round(data.score.percent);
  var cls = pct >= 80 ? 'high' : pct >= 50 ? 'mid' : 'low';

  html += '<div class="score-hero">'
    + '<div class="pct ' + cls + '">' + pct + '%</div>'
    + '<div class="detail">' + data.score.killed + ' of ' + data.score.total + ' mutants killed</div>'
    + '</div>';

  html += '<div class="mutant-grid">';
  html += '<div class="mutant-section"><h3><span style="color:#16a34a">\u2713</span> Killed (' + (data.killed ? data.killed.length : 0) + ')</h3>';
  if (data.killed && data.killed.length > 0) {
    data.killed.forEach(function(k) {
      html += '<div class="mutant-card killed">'
        + '<div class="mc-label">' + esc(k.description) + '</div>'
        + '<div class="caught">caught by ' + esc(k.caught_by) + '</div></div>';
    });
  } else {
    html += '<div class="empty-state">No mutants killed</div>';
  }
  html += '</div>';

  html += '<div class="mutant-section"><h3><span style="color:#dc2626">\u2717</span> Survived (' + data.survived.length + ')</h3>';
  if (data.survived.length > 0) {
    data.survived.forEach(function(s) {
      html += '<div class="mutant-card survived">'
        + '<div class="mc-label">' + esc(s.description) + '</div>'
        + '<div class="suggestion">' + esc(s.suggestion) + '</div></div>';
    });
  } else {
    html += '<div class="empty-state" style="color:#16a34a">All mutants killed!</div>';
  }
  html += '</div></div>';

  document.getElementById('mutateResults').innerHTML = html;
}

function esc(s) {
  if (s == null) return '';
  var d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

loadState();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Stipulate Order demo.")
    parser.add_argument("command", choices=("explore", "mutate", "validate", "serve"))
    parser.add_argument("--budget", type=int, default=500)
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument(
        "--optimizer",
        choices=("deterministic", "hypothesis", "hybrid"),
        default="deterministic",
    )
    parser.add_argument(
        "--buggy",
        action="store_true",
        help="Run mutation against the intentionally buggy implementation.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.command == "explore":
        result = run_explore(
            budget=args.budget,
            max_depth=args.max_depth,
            optimizer=args.optimizer,
            fixed=False,
        )
        if args.json:
            print(json.dumps(exploration_to_dict(result), indent=2, sort_keys=True))
        else:
            print_explore_result(result)
        return 1 if result.violations else 0

    if args.command == "mutate":
        result = run_mutate(
            budget=args.budget,
            max_depth=args.max_depth,
            optimizer=args.optimizer,
            fixed=not args.buggy,
        )
        if args.json:
            print(json.dumps(mutation_to_dict(result), indent=2, sort_keys=True))
        else:
            print_mutation_result(result)
        return 1 if result.unexpected_survivors else 0

    if args.command == "serve":
        serve_demo(args.host, args.port)
        return 0

    validate_demo()
    print("Order demo validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
