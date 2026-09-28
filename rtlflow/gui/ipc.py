"""Shell ⇄ GUI channel: newline-delimited JSON over a socketpair.

The shell creates a socketpair, keeps one end and passes the other fd to the
GUI subprocess (subprocess.Popen(pass_fds=...)) — no multiprocessing, so it
works on Python 3.14's forkserver default.
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Callable


class Channel:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._wlock = threading.Lock()
        self._rbuf = b""
        self.closed = False

    def send(self, **msg) -> bool:
        if self.closed:
            return False
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        try:
            with self._wlock:
                self.sock.sendall(data)
            return True
        except OSError:
            self.closed = True
            return False

    def recv_loop(self, handler: Callable[[dict], None], on_close: Callable[[], None] | None = None) -> None:
        try:
            while True:
                chunk = self.sock.recv(65536)
                if not chunk:
                    break
                self._rbuf += chunk
                while b"\n" in self._rbuf:
                    line, self._rbuf = self._rbuf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    try:
                        handler(msg)
                    except Exception:
                        pass
        except OSError:
            pass
        self.closed = True
        if on_close:
            on_close()

    def start_reader(self, handler, on_close=None, name="ipc-reader") -> threading.Thread:
        t = threading.Thread(target=self.recv_loop, args=(handler, on_close), name=name, daemon=True)
        t.start()
        return t

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
