"""同一台電腦只執行一份（spec FR-46）。

`QLockFile`（系統暫存資料夾）決定誰是第一份：程序結束或當掉時鎖會失效，不會卡住下次啟動。
第一份另開 `QLocalServer`；後來的程序連上去送 `activate`，第一份收到後發 `activate_requested`。
Windows 限制背景程序搶前景，所以後來的程序（使用者剛啟動、位於前景）先以
`AllowSetForegroundWindow` 把前景權交給第一份。
"""

import logging
import os
import sys
import time

from PySide6.QtCore import QDir, QLockFile, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger("plc_motion.ui")

INSTANCE_KEY = "plc-motion-single-instance"
_ACTIVATE = b"activate\n"


class SingleInstance(QObject):
    activate_requested = Signal()

    def __init__(self, key: str = INSTANCE_KEY, parent: QObject | None = None):
        super().__init__(parent)
        self._key = key
        self._lock = QLockFile(os.path.join(QDir.tempPath(), f"{key}.lock"))
        self._lock.setStaleLockTime(0)  # 只在持有者程序已不存在時視為失效
        self._server: QLocalServer | None = None
        self._sockets: set[QLocalSocket] = set()

    def acquire(self) -> bool:
        """是第一份時回傳 True 並開始等待叫出要求；否則通知第一份並回傳 False。"""
        if self._lock.tryLock(0):
            self._listen()
            return True
        self._activate_primary()
        return False

    def release(self) -> None:
        # QLocalSocket 解構時會發 disconnected：先斷開 signal，免得在解構途中回呼到 _read
        for socket in list(self._sockets):
            socket.readyRead.disconnect()
            socket.disconnected.disconnect()
            socket.abort()
        self._sockets.clear()
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._lock.isLocked():
            self._lock.unlock()

    def _listen(self) -> None:
        QLocalServer.removeServer(self._key)  # 前一份當掉時遺留的 socket（非 Windows）
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        if not self._server.listen(self._key):
            log.warning("單一實例通知通道開啟失敗：%s", self._server.errorString())

    def _on_new_connection(self) -> None:
        while self._server is not None and self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            self._sockets.add(socket)
            socket.readyRead.connect(lambda s=socket: self._read(s))
            # 對方寫完立刻斷線時，資料可能在斷線之後才讀得到
            socket.disconnected.connect(lambda s=socket: self._read(s, done=True))
            self._read(socket)

    def _read(self, socket: QLocalSocket, done: bool = False) -> None:
        try:
            data = bytes(socket.readAll())
        except RuntimeError:  # C++ 物件已隨 server 刪除
            return
        if _ACTIVATE.strip() in data:
            log.info("另一個程序啟動，叫出主視窗")
            self.activate_requested.emit()
        if done:
            self._sockets.discard(socket)
            socket.readyRead.disconnect()
            socket.disconnected.disconnect()
            socket.deleteLater()

    def _activate_primary(self) -> None:
        info = self._lock.getLockInfo()  # PySide6：成功時為 (pid, hostname, appname)
        pid = info[0] if isinstance(info, tuple) and info else None
        if pid and sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.user32.AllowSetForegroundWindow(int(pid))
            except Exception:
                pass
        deadline = time.monotonic() + 5  # 第一份可能還在啟動、尚未開始 listen
        while time.monotonic() < deadline:
            socket = QLocalSocket()
            socket.connectToServer(self._key)
            if socket.waitForConnected(500):
                socket.write(_ACTIVATE)
                socket.flush()
                socket.waitForBytesWritten(1000)
                socket.disconnectFromServer()
                return
            time.sleep(0.1)
