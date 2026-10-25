"""The hard total deadline of an outbound request (D-090, D-117, ``docs/SECURITY.md`` section 6).

A socket timeout limits one wait, not the whole answer: a server that sends one byte every few seconds would hold a
synchronous worker for hours. The callers therefore run the request in a helper thread and wait for it with a total
time limit. When the limit is reached they call ``Budget.expire``, which shuts down every socket of the request, so
that a read blocked in the helper thread ends at once. The sockets are found through the connection classes of
``Adapter``. The helper threads do not touch the database.

Used by ``swisstopo`` (the building search) and ``importer`` (the official downloads).
"""

import socket
import threading

from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool


class Budget:
    """The sockets of one request, so that the deadline can shut them down from another thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sockets = []
        self.expired = False

    def add(self, sock):
        with self._lock:
            if not self.expired:
                self._sockets.append(sock)
                return
        self._shut(sock)

    def expire(self):
        with self._lock:
            self.expired = True
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            self._shut(sock)

    @staticmethod
    def _shut(sock):
        # ``shutdown`` ends a read that is blocked in another thread; ``close`` alone may not.
        for action in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
            try:
                action()
            except (OSError, ValueError):
                pass


def connection_classes(budget):
    """Connection pools whose sockets register with ``budget`` (the raw socket and, after TLS, the wrapped one)."""

    class Connection(HTTPConnection):
        def _new_conn(self):
            sock = super()._new_conn()
            budget.add(sock)
            return sock

    class SecureConnection(HTTPSConnection):
        def _new_conn(self):
            sock = super()._new_conn()
            budget.add(sock)
            return sock

        def connect(self):
            super().connect()
            budget.add(self.sock)

    class Pool(HTTPConnectionPool):
        ConnectionCls = Connection

    class SecurePool(HTTPSConnectionPool):
        ConnectionCls = SecureConnection

    return {"http": Pool, "https": SecurePool}


class Adapter(HTTPAdapter):
    """A transport adapter whose sockets belong to ``budget``; mount it on a session before the request."""

    def __init__(self, budget):
        self._budget = budget  # before ``super().__init__``: it builds the pool manager
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = connection_classes(self._budget)
