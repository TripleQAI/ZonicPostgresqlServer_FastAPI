"""Governed object invocation — compile views/functions/procedures by name.

Callers never write SQL; they name the object + pass params. Tests cover safe
compilation, injection rejection, deterministic (prepared-statement-friendly)
output, and end-to-end execution via the FakeDriver.
"""
from __future__ import annotations

import pytest

from pgfoundation import (
    DataFoundation,
    Filter,
    FilterOp,
    FunctionQuery,
    OrderBy,
    ProcedureCall,
    ResultSet,
    ViewQuery,
)
from pgfoundation._internal.access.invocation import (
    compile_function,
    compile_procedure,
    compile_view,
)
from pgfoundation._internal.config.loader import load_settings
from pgfoundation._internal.drivers.fake import FakeDriver
from pgfoundation.core.errors import QueryError


# --- VIEW ---

def test_view_all_columns():
    q = compile_view(ViewQuery(name="v_paid_orders"))
    assert q.sql == "SELECT * FROM v_paid_orders"
    assert q.params == {}


def test_view_columns_filters_order_limit():
    q = compile_view(ViewQuery(
        name="orders", schema="public", columns=["id", "amount"],
        filters=[Filter("customer", FilterOp.EQ, "bob"),
                 Filter("amount", FilterOp.GE, 10)],
        order_by=[OrderBy("id", descending=True)], limit=50, offset=10,
    ))
    assert q.sql == (
        "SELECT id, amount FROM public.orders "
        "WHERE customer = %(f0)s AND amount >= %(f1)s "
        "ORDER BY id DESC LIMIT 50 OFFSET 10"
    )
    assert q.params == {"f0": "bob", "f1": 10}


def test_view_in_and_between_and_null_filters():
    q = compile_view(ViewQuery(name="t", filters=[
        Filter("status", FilterOp.IN, ["paid", "shipped"]),
        Filter("ts", FilterOp.BETWEEN, ["2026-01", "2026-06"]),
        Filter("deleted_at", FilterOp.IS_NULL),
    ]))
    assert "status IN (%(f0_0)s, %(f0_1)s)" in q.sql
    assert "ts BETWEEN %(f1_lo)s AND %(f1_hi)s" in q.sql
    assert "deleted_at IS NULL" in q.sql
    assert q.params == {"f0_0": "paid", "f0_1": "shipped", "f1_lo": "2026-01", "f1_hi": "2026-06"}


def test_view_rejects_injection_in_name_and_column():
    with pytest.raises(QueryError, match="unsafe SQL identifier"):
        compile_view(ViewQuery(name="orders; DROP TABLE x"))
    with pytest.raises(QueryError, match="unsafe SQL identifier"):
        compile_view(ViewQuery(name="orders", columns=["id", "amount) --"]))


def test_view_empty_in_list_rejected():
    with pytest.raises(QueryError, match="non-empty list"):
        compile_view(ViewQuery(name="t", filters=[Filter("s", FilterOp.IN, [])]))


# --- FUNCTION ---

def test_function_set_returning_named_args():
    q = compile_function(FunctionQuery(name="fn_orders_by_customer", args={"cust": "bob"}))
    assert q.sql == "SELECT * FROM fn_orders_by_customer(cust => %(cust)s)"
    assert q.params == {"cust": "bob"}


def test_function_positional_args_and_scalar():
    q = compile_function(FunctionQuery(name="fn_total", args=[1, 2], scalar=True))
    assert q.sql == "SELECT fn_total(%(a0)s, %(a1)s) AS result"
    assert q.params == {"a0": 1, "a1": 2}


def test_function_no_args():
    assert compile_function(FunctionQuery(name="now_ish")).sql == "SELECT * FROM now_ish()"


def test_function_rejects_injection():
    with pytest.raises(QueryError):
        compile_function(FunctionQuery(name="f(); DROP"))
    with pytest.raises(QueryError):
        compile_function(FunctionQuery(name="f", args={"x); DROP--": 1}))


# --- PROCEDURE ---

def test_procedure_call_positional():
    q = compile_procedure(ProcedureCall(name="sp_stats", args=["bob", None, None]))
    assert q.sql == "CALL sp_stats(%(a0)s, %(a1)s, %(a2)s)"
    assert q.params == {"a0": "bob", "a1": None, "a2": None}


def test_procedure_no_args():
    assert compile_procedure(ProcedureCall(name="sp_run")).sql == "CALL sp_run()"


# --- deterministic output (prepared-statement reuse / high throughput) ---

def test_compilation_is_deterministic_for_same_shape():
    a = compile_view(ViewQuery(name="t", filters=[Filter("c", FilterOp.EQ, 1)]))
    b = compile_view(ViewQuery(name="t", filters=[Filter("c", FilterOp.EQ, 2)]))
    assert a.sql == b.sql  # identical SQL text → psycopg prepares once, reuses


# --- end-to-end through the facade (FakeDriver) ---

async def _foundation(responder=None):
    settings = load_settings({"datasources": [{"name": "db", "dsn": "postgresql://x/db"}]})
    driver = FakeDriver(responder)
    return await DataFoundation.from_settings(settings, driver=driver), driver.pools["postgresql://x/db"]


async def test_view_function_procedure_execute_via_facade():
    def responder(spec):
        return ResultSet(rows=[{"ok": 1}], rowcount=1)

    db, pool = await _foundation(responder)
    assert (await db.view("db", ViewQuery(name="v_paid_orders"))).rows == [{"ok": 1}]
    assert (await db.function("db", FunctionQuery(name="fn_x", args={"a": 1}))).rows == [{"ok": 1}]
    assert (await db.procedure("db", ProcedureCall(name="sp_y", args=[1]))).rows == [{"ok": 1}]
    # the SQL that reached the driver was compiled from names, not client SQL
    assert any("v_paid_orders" in s.sql for s in pool.executed)
    assert all(";" not in s.sql for s in pool.executed)  # no injected multi-statements
