"""Diagnostic-only timing wrapper. No changes to production source or transaction semantics."""

import json
import os
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from time import perf_counter_ns

import fastapi.routing
from starlette.concurrency import run_in_threadpool

from campus_fabric.api.mutations import http as mutation_http
from campus_fabric.api.test_control import faults
from campus_fabric.domain.kernel import Kernel
from campus_fabric.storage.sqlite import SQLiteStore
from campus_fabric.storage.state import StateDict
from campus_fabric.workflows.devhost import create_app as product_app

REQUEST = ContextVar("diagnostic_request", default=None)
TRANSACTION = ContextVar("diagnostic_transaction", default=None)
EVENTS = []


class TimedConnection:
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args, **kwargs):
        transaction = TRANSACTION.get()
        if transaction is None or sql != "BEGIN IMMEDIATE":
            return self.connection.execute(sql, *args, **kwargs)
        transaction["begin_started_ns"] = perf_counter_ns()
        try:
            return self.connection.execute(sql, *args, **kwargs)
        finally:
            transaction["begin_finished_ns"] = perf_counter_ns()

    def commit(self):
        transaction = TRANSACTION.get()
        if transaction is None:
            return self.connection.commit()
        transaction["commit_started_ns"] = perf_counter_ns()
        try:
            return self.connection.commit()
        finally:
            transaction["commit_finished_ns"] = perf_counter_ns()


def install():
    original_connect = SQLiteStore._connect
    original_transaction = SQLiteStore.transaction

    def connect(store):
        return TimedConnection(original_connect(store))

    @contextmanager
    def transaction(store):
        record = {"kind": "transaction", "request_id": REQUEST.get(),
                  "started_ns": perf_counter_ns(), "phases": defaultdict(list)}
        token = TRANSACTION.set(record)
        try:
            with original_transaction(store) as connection:
                record["body_started_ns"] = perf_counter_ns()
                try:
                    yield connection
                finally:
                    record["body_finished_ns"] = perf_counter_ns()
        except BaseException as error:
            record["error_type"] = type(error).__name__
            raise
        finally:
            record["finished_ns"] = perf_counter_ns()
            EVENTS.append(record)
            TRANSACTION.reset(token)

    SQLiteStore._connect = connect
    SQLiteStore.transaction = transaction

    def phase(cls, name, label):
        original = getattr(cls, name)

        def measured(*args, **kwargs):
            before = perf_counter_ns()
            try:
                return original(*args, **kwargs)
            finally:
                record = TRANSACTION.get()
                if record is not None:
                    record["phases"][label].append(perf_counter_ns() - before)
                    if label == "save":
                        state = args[2]
                        record["cached_entities"] = {
                            key: len(value._cache)
                            for key, value in getattr(state, "_containers", {}).items()}
                        record["write_stats"] = dict(getattr(args[0], "last_write_stats", {}))

        setattr(cls, name, measured)

    phase(Kernel, "_inventory", "inventory")
    phase(SQLiteStore, "save", "save")
    phase(StateDict, "changed_units", "changed_units")

    def operation(name, kind):
        original = getattr(Kernel, name)

        def measured(kernel, *args, **kwargs):
            record = {"kind": kind, "request_id": REQUEST.get(),
                      "started_ns": perf_counter_ns()}
            if kind == "command":
                record["operation"] = args[0].operation
            try:
                return original(kernel, *args, **kwargs)
            finally:
                record["finished_ns"] = perf_counter_ns()
                EVENTS.append(record)

        setattr(Kernel, name, measured)

    operation("commit_with_payload", "command")
    operation("take_fault", "fault_check")

    async def timed_pool(function, *args, **kwargs):
        record = {"kind": "thread_dispatch", "request_id": REQUEST.get(),
                  "function": getattr(function, "__qualname__", type(function).__name__),
                  "enqueued_ns": perf_counter_ns()}

        def entered():
            record["entered_ns"] = perf_counter_ns()
            try:
                return function(*args, **kwargs)
            finally:
                record["function_finished_ns"] = perf_counter_ns()

        try:
            return await run_in_threadpool(entered)
        finally:
            record["returned_ns"] = perf_counter_ns()
            EVENTS.append(record)

    mutation_http.run_in_threadpool = timed_pool
    faults.run_in_threadpool = timed_pool
    fastapi.routing.run_in_threadpool = timed_pool


class TimedApp:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            try:
                return await self.app(scope, receive, send)
            finally:
                if scope["type"] == "lifespan":
                    Path(os.environ["PHASE_EVENTS_PATH"]).write_text(json.dumps({
                        "kind": "DIAGNOSTIC_ONLY_NOT_ACCEPTANCE",
                        "clock": "perf_counter_ns within one service process",
                        "collection": "in-memory events, written once after lifespan shutdown",
                        "phases_overlap": "changed_units is inside save; inventory is inside transaction body",
                        "events": EVENTS}, separators=(",", ":")) + "\n")
        request_id = dict(scope.get("headers", ())).get(b"x-request-id", b"").decode()
        token = REQUEST.set(request_id or None)
        record = {"kind": "http", "request_id": request_id or None,
                  "path": scope["path"], "method": scope["method"],
                  "started_ns": perf_counter_ns()}

        async def sent(message):
            if message["type"] == "http.response.start":
                record["status"] = message["status"]
            await send(message)

        try:
            return await self.app(scope, receive, sent)
        finally:
            record["finished_ns"] = perf_counter_ns()
            EVENTS.append(record)
            REQUEST.reset(token)


def create_app():
    install()
    return TimedApp(product_app())
