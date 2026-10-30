# SPDX-License-Identifier: Apache-2.0
"""Fixtures for unit tests that run without a compiled xStudio.

The pybind module ``xstudio.core`` is replaced by a pure python fake whose
``Link`` mirrors src/python_module/src/py_context.cpp: ``request`` blocks for
the matching response (dropping anything else that arrives meanwhile),
errors and timeouts are raised as RuntimeError, broadcasts are queued in a
mailbox, and ``wait_for_xstudio_exit`` only returns once xStudio exits.
"""
import importlib
import itertools
import os
import queue
import sys
import threading
import time
import types

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class _Atom(object):
    def __eq__(self, other):
        return type(self) is type(other)

    def __hash__(self):
        return hash(type(self))

    def __repr__(self):
        return type(self).__name__


def _atom(name):
    return type(name, (_Atom,), {})


class error(object):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return self.msg


class Actor(object):
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return "actor:" + self.name


class JsonStore(object):
    def __init__(self, data=None):
        self.data = data


class FakeLink(object):
    """Mailbox based stand in for the pybind Link."""

    def __init__(self):
        self.mailbox = queue.Queue()
        self._ids = itertools.count(1)
        self._remote = Actor("remote")
        self.server = None  # func(target, args) -> (reply, delay_s) or None
        self.host_ = "localhost"
        self.port_ = 45500
        self.callbacks = {}
        self.disconnects = 0
        self.exited = threading.Event()

    # connection
    def connect_remote(self, host, port):
        self.host_, self.port_ = host, port
        return True

    def connect_local(self, actor):
        return True

    def disconnect(self):
        self.disconnects += 1

    def remote(self):
        return self._remote

    def set_remote(self, actor):
        self._remote = actor

    def host(self):
        return self.host_

    def port(self):
        return self.port_

    # messaging
    def request(self, timeout_ms, target, *args):
        req_id = next(self._ids)
        reply = self.server(target, args)
        if reply is None:
            time.sleep(timeout_ms / 1000.0)
            raise RuntimeError("Dequeue timeout")
        payload, delay = reply
        if delay * 1000.0 > timeout_ms:
            time.sleep(timeout_ms / 1000.0)
            raise RuntimeError("Dequeue timeout")
        time.sleep(delay)
        if isinstance(payload[0], error):
            raise RuntimeError(str(payload[0]))
        return tuple(payload) + (req_id, target)

    def wait_for_xstudio_exit(self):
        # Like the real Link, disconnect() does not wake this up.
        self.exited.wait()

    def send(self, *args):
        pass

    def broadcast(self, sender, *event):
        self.mailbox.put(tuple(event) + (0, sender))

    def dequeue_message_with_timeout(self, timeout_ms):
        try:
            return self.mailbox.get(timeout=timeout_ms / 1000.0)
        except queue.Empty:
            raise RuntimeError("Dequeue timeout")

    def add_message_callback(self, actor, handler):
        key = object()
        self.callbacks[key] = (actor, handler)
        return key

    def remove_message_callback(self, key):
        self.callbacks.pop(key)


def _make_core():
    core = types.ModuleType("xstudio.core")
    for name in (
            "version_atom", "authenticate_atom", "get_application_mode_atom",
            "broadcast_down_atom", "join_broadcast_atom", "exit_atom",
            "api_exit_atom", "leave_broadcast_atom", "get_event_group_atom"):
        setattr(core, name, _atom(name))
    core.absolute_receive_timeout = lambda ms: ms
    core.error = error
    core.Link = FakeLink
    core.JsonStore = JsonStore
    core.XSTUDIO_LOCAL_PLUGIN_PATH = "/nonexistent"
    core.RemoteSessionManager = None
    core.remote_session_path = lambda: "/tmp/sessions"
    return core


@pytest.fixture
def xs(monkeypatch):
    """Import xstudio.connection against the fake core."""
    pkg = types.ModuleType("xstudio")
    pkg.__path__ = [os.path.abspath(os.path.join(SRC, "xstudio"))]
    api = types.ModuleType("xstudio.api")
    api.API = lambda conn: ("API", conn)
    core = _make_core()
    for name, mod in (("xstudio", pkg), ("xstudio.core", core), ("xstudio.api", api)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, "xstudio.connection", raising=False)
    module = importlib.import_module("xstudio.connection")
    module.core = core
    module.Actor = Actor
    return module


@pytest.fixture
def server():
    """Default server: answers the negotiate handshake and joins."""
    state = {"auth_required": False, "handlers": []}
    remote = Actor("remote")
    group = Actor("remote_events")

    def handle(target, args):
        for h in state["handlers"]:
            r = h(target, args)
            if r is not None:
                return r
        name = type(args[0]).__name__
        if name == "version_atom":
            return (("1.0.0",), 0)
        if name == "get_application_mode_atom":
            return (("XSTUDIO",), 0)
        if name == "authenticate_atom":
            if state["auth_required"] and len(args) == 1:
                return ((error("authentication required"),), 0)
            if state["auth_required"] and args[1:] != state["credentials"]:
                return ((error("bad credentials"),), 0)
            return ((remote,), 0)
        if name == "get_event_group_atom":
            return ((group,), 0)
        if name in ("join_broadcast_atom", "leave_broadcast_atom"):
            return ((True,), 0)
        return None

    state["handle"] = handle
    state["remote"] = remote
    state["group"] = group
    return state


@pytest.fixture
def conn(xs, server):
    c = xs.Connection()
    c.link.server = server["handle"]
    c.default_timeout_ms = 2000
    c.connect_remote("localhost", 45500)
    yield c
    c.disconnect()
