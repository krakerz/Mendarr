"""Just enough RFB 3.8 to click: handshake (security None) and pointer events, over websockets.sync."""

import struct
import time

from websockets.sync.client import connect


class RfbClient:
    def __init__(self, url: str, headers: dict[str, str]) -> None:
        self._ws = connect(
            url, additional_headers=headers, subprotocols=["binary"], max_size=None, open_timeout=10
        )
        self._buf = b""

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            msg = self._ws.recv(timeout=15)
            self._buf += msg if isinstance(msg, bytes) else msg.encode()
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def handshake(self) -> tuple[int, int]:
        banner = self._recv_exact(12)
        if not banner.startswith(b"RFB 003."):
            raise RuntimeError(f"not an RFB server: {banner!r}")
        self._ws.send(b"RFB 003.008\n")
        count = self._recv_exact(1)[0]
        if count == 0:
            raise RuntimeError("server refused the connection")
        if 1 not in self._recv_exact(count):
            raise RuntimeError("server offers no 'None' security type")
        self._ws.send(b"\x01")
        if struct.unpack(">I", self._recv_exact(4))[0] != 0:
            raise RuntimeError("security handshake failed")
        self._ws.send(b"\x01")  # ClientInit: shared
        w, h = struct.unpack(">HH", self._recv_exact(4))
        self._recv_exact(16)  # pixel format
        (name_len,) = struct.unpack(">I", self._recv_exact(4))
        self._recv_exact(name_len)
        return w, h

    def _pointer(self, mask: int, x: int, y: int) -> None:
        self._ws.send(struct.pack(">BBHH", 5, mask, x, y))

    def click(self, x: int, y: int) -> None:
        self._pointer(0, x, y)
        self._pointer(1, x, y)
        time.sleep(0.08)
        self._pointer(0, x, y)

    def close(self) -> None:
        self._ws.close()
