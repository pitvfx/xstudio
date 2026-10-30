# SPDX-License-Identifier: Apache-2.0
import sys
import time
import types

import pytest


def _ping_server(delay=0):
    def handler(target, args):
        if args and args[0] == "ping":
            return (("pong",), delay)
    return handler


def test_connect_negotiates(conn):
    assert conn.connected
    assert conn.app_version == "1.0.0"
    assert conn.app_type == "XSTUDIO"
    assert "XSTUDIO" in repr(conn)


def test_request_receive(conn, server):
    server["handlers"].append(_ping_server())
    assert conn.request_receive(conn.remote(), "ping")[0] == "pong"


def test_request_receive_raises_on_timeout(conn, xs):
    with pytest.raises(xs.XStudioTimeoutError):
        conn.request_receive_timeout(50, conn.remote(), "unanswered")
    # Still catchable as before, and as the builtin.
    with pytest.raises(RuntimeError):
        conn.request_receive_timeout(50, conn.remote(), "unanswered")
    with pytest.raises(TimeoutError):
        conn.request_receive_timeout(50, conn.remote(), "unanswered")


def test_error_reply_raises(conn, server, xs):
    server["handlers"].append(
        lambda target, args: ((xs.core.error("boom"),), 0) if args[0] == "fail" else None)
    with pytest.raises(RuntimeError, match="boom") as info:
        conn.request_receive(conn.remote(), "fail")
    assert not isinstance(info.value, TimeoutError)


def test_process_events_honours_timeout(conn):
    # Previously each dequeue waited a fixed 5s whatever timeout was given.
    start = time.monotonic()
    conn.process_events(20)
    assert time.monotonic() - start < 0.5


def test_broadcast_handlers(conn, server):
    events = []
    hid = conn.add_handler(server["remote"], lambda s, r, e: events.append(e))
    conn.link.broadcast(server["remote"], "changed")
    conn.process_events(20)
    assert events == [("changed",)]

    assert conn.remove_handler(hid)
    conn.link.broadcast(server["remote"], "changed")
    conn.process_events(20)
    assert events == [("changed",)]


def test_failing_handler_does_not_block_others(conn, server):
    events = []

    def bad(s, r, e):
        raise ValueError("handler bug")

    conn.add_handler(server["remote"], bad)
    conn.add_handler(server["remote"], lambda s, r, e: events.append(e))
    conn.link.broadcast(server["remote"], "changed")
    conn.process_events(20)
    assert events == [("changed",)]


def test_handler_can_remove_itself(conn, server):
    calls = []

    def once(s, r, e):
        calls.append(e)
        conn.remove_handler(hid)

    hid = conn.add_handler(server["remote"], once)
    conn.link.broadcast(server["remote"], "a")
    conn.link.broadcast(server["remote"], "b")
    conn.process_events(20)
    assert calls == [("a",)]


def test_exit_broadcast_disconnects(conn, xs):
    conn.link.broadcast(conn.link.remote(), xs.core.exit_atom())
    conn.process_events(20)
    assert not conn.connected


def test_disconnect_with_background_processing(conn):
    # The event thread blocks until xStudio exits; disconnecting must not
    # wait for that.
    conn.enable_background_processing()
    thread = conn.queue_thread
    assert thread.daemon
    start = time.monotonic()
    conn.disconnect()
    assert time.monotonic() - start < 1
    assert conn.queue_thread is None

    conn.link.exited.set()
    thread.join(1)
    assert not thread.is_alive()


def test_event_callbacks(conn, server):
    hid = conn.add_event_callback(server["remote"], lambda *a: None)
    assert hid in conn.link.callbacks
    assert conn.remove_event_callback(hid)
    assert hid not in conn.link.callbacks


def test_authentication(xs, server):
    server["auth_required"] = True
    server["credentials"] = ("user", "pass word")
    c = xs.Connection()
    c.link.server = server["handle"]
    c.connect_remote("localhost", 45500, lambda: "user pass word")
    assert c.connected
    c.disconnect()


def test_authentication_failure(xs, server):
    server["auth_required"] = True
    server["credentials"] = ("key",)
    c = xs.Connection()
    c.link.server = server["handle"]
    with pytest.raises(xs.AuthenticationError):
        c.connect_remote("localhost", 45500, lambda: "wrong")
    with pytest.raises(RuntimeError):
        c.connect_remote("localhost", 45500, lambda: "")
    assert not c.connected


def test_authentication_timeout_is_not_auth_error(xs, server):
    server["handlers"].append(
        lambda target, args: (("late",), 10) if type(args[0]).__name__ == "authenticate_atom" else None)
    c = xs.Connection()
    c.link.server = server["handle"]
    c.default_timeout_ms = 50
    prompted = []
    with pytest.raises(xs.XStudioTimeoutError):
        c.connect_remote("localhost", 45500, lambda: prompted.append(1) or "key")
    assert not prompted


def test_connect_failure(xs):
    c = xs.Connection()
    c.link.connect_remote = lambda host, port: False
    with pytest.raises(xs.XStudioConnectionError):
        c.connect_remote("nowhere", 1)
    with pytest.raises(ConnectionError):
        c.connect_remote("nowhere", 1)


def test_connect_remote_auto_no_session(xs, monkeypatch):
    class Manager(object):
        def __init__(self, path):
            pass

        def first_api(self):
            return None

        def find(self, name):
            return None

    monkeypatch.setattr(xs, "RemoteSessionManager", Manager)
    c = xs.Connection()
    with pytest.raises(xs.XStudioConnectionError, match="No remote"):
        c.connect_remote_auto()
    with pytest.raises(xs.XStudioConnectionError, match="'foo' not found"):
        c.connect_remote_auto("foo")


def test_context_manager(xs, server):
    with xs.Connection() as c:
        c.link.server = server["handle"]
        c.connect_remote("localhost", 45500)
        assert c.connected
    assert not c.connected


def test_disconnect_is_idempotent(conn):
    conn.disconnect()
    conn.disconnect()
    assert not conn.connected


def test_plugin_loading(xs, conn, tmp_path, monkeypatch):
    plugin = tmp_path / "my_unit_test_plugin"
    plugin.mkdir()
    (plugin / "__init__.py").write_text(
        "class P(object):\n"
        "    name = 'mine'\n"
        "    uuid = 'abc'\n"
        "    cleaned = False\n"
        "    def cleanup(self):\n"
        "        P.cleaned = True\n"
        "def create_plugin_instance(connection):\n"
        "    return P()\n")
    (tmp_path / "__pycache__").mkdir()

    hud = types.ModuleType("xstudio.plugin.hud_plugin")
    hud.HUDPlugin = type("HUDPlugin", (), {})
    monkeypatch.setitem(sys.modules, "xstudio.plugin.hud_plugin", hud)
    monkeypatch.setattr(sys, "path", list(sys.path))

    conn.load_plugins_in_path(str(tmp_path))
    conn.load_plugins_in_path(str(tmp_path))
    assert len(conn.plugins) == 1
    assert sys.path.count(str(tmp_path)) == 1
    assert conn.get_named_python_plugin_instance("mine").uuid == "abc"
    assert conn.get_plugin_instance("abc").name == "mine"
    with pytest.raises(KeyError):
        conn.get_plugin_instance("missing")

    p = conn.get_plugin_instance("abc")
    conn.disconnect()
    assert type(p).cleaned
