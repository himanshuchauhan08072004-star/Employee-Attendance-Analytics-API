"""Shared fixtures. Integration tests need a real MongoDB (6.0+).

  MONGO_URI=mongodb://localhost:27017 pytest tests          # PowerShell: $env:MONGO_URI="..."; pytest tests
Tests use database  attendance_test  (override with TEST_MONGO_DB). It is WIPED by the tests.
If MongoDB is unreachable the integration tests are skipped, never faked.
"""
import os
import socket
import threading
import time

import pytest

os.environ["MONGO_DB"] = os.getenv("TEST_MONGO_DB", "attendance_test")  # must be set before importing app

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import main  # noqa: E402


def _mongo_up() -> bool:
    try:
        main.client.admin.command("ping")
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def server():
    if not _mongo_up():
        pytest.skip("MongoDB not reachable: integration tests skipped")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    yield base
    srv.should_exit = True
    t.join(timeout=5)


@pytest.fixture
def api(server):
    main.employees.delete_many({})
    main.attendance.delete_many({})
    with httpx.Client(base_url=server, timeout=30) as c:
        yield c


@pytest.fixture
def http(server):
    """Factory for independent clients, used by the concurrency tests (one per thread)."""
    def make():
        return httpx.Client(base_url=server, timeout=30)
    return make
