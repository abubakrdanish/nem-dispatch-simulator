import duckdb
import pytest

from tests.fixtures.synthetic import build


@pytest.fixture(scope="session")
def warehouse(tmp_path_factory):
    path = build(tmp_path_factory.mktemp("wh") / "synthetic.duckdb")
    con = duckdb.connect(str(path), read_only=True)
    yield con
    con.close()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never touch NEMWEB."""
    import socket

    def guard(*args, **kwargs):
        raise RuntimeError("network access in tests")
    monkeypatch.setattr(socket.socket, "connect", guard)
