"""
День 33. MCP-клиент: запускает mcp_server.py (CRM) как дочерний процесс и общается с ним по stdio.
Только стандартная библиотека.

    with MCPClient("data/crm.json") as mcp:
        print(mcp.call("list_tickets"))
"""

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "mcp_server.py")
DEFAULT_DATA = os.path.join(HERE, "..", "data", "crm.json")
PROTOCOL_VERSION = "2024-11-05"


class MCPError(RuntimeError):
    """Сервер не ответил, закрыл соединение или вернул ошибку протокола."""


class MCPToolError(RuntimeError):
    """Инструмент вернул isError: true. Текст ошибки понятен пользователю."""


class MCPClient:
    def __init__(self, data_path: str = DEFAULT_DATA):
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        self.proc = subprocess.Popen([sys.executable, SERVER, "--data", os.path.abspath(data_path)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        self._next_id = 0
        self.server_info = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "day33-support", "version": "1.0.0"},
        }).get("serverInfo", {})
        self._notify("notifications/initialized")

    def _send(self, msg: dict) -> None:
        if self.proc.poll() is not None:
            raise MCPError("MCP-сервер уже остановлен")
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict = None) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        while True:
            raw = self.proc.stdout.readline()
            if not raw:
                raise MCPError(f"MCP-сервер закрыл соединение (код {self.proc.poll()})")
            try:
                msg = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as e:
                raise MCPError(f"сервер прислал не JSON: {raw[:120]!r}") from e
            if msg.get("id") != rid:
                continue  # чужое или устаревшее сообщение — пропускаем
            if "error" in msg:
                raise MCPError(f"{method}: {msg['error'].get('message', 'ошибка')}")
            return msg.get("result", {})

    def _notify(self, method: str, params: dict = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def list_tools(self) -> list:
        return self._request("tools/list").get("tools", [])

    def call(self, name: str, arguments: dict = None) -> str:
        res = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            raise MCPToolError(text)
        return text

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
