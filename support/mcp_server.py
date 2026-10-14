"""
День 33. MCP-сервер CRM: тикеты и пользователи из JSON (вместо настоящей CRM).

Транспорт stdio, JSON-RPC 2.0, одна строка на сообщение. Только стандартная библиотека.
Инструменты (только чтение):
  get_ticket   {ticket_id}  — тикет + последние строки журнала
  get_user     {user_id}    — пользователь без секретов
  list_tickets {status?}    — список тикетов (id, тема, статус)

Что НЕ отдаётся модели никогда:
  • хеш пароля и любые поля с password/hash/token/secret;
  • полный e-mail: показывается маска a***@example.com;
  • чужие данные: сервер отдаёт то, что запросили; ассистент запрашивает get_user только для
    пользователя из того тикета, который открыт в запросе.

    python support/mcp_server.py --data data/crm.json
"""

import argparse
import json
import os
import re
import sys

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "support-crm", "version": "1.0.0"}
TICKET_RE = re.compile(r"^T-\d{3,6}$")
USER_RE = re.compile(r"^U-\d{2,6}$")
LOG_LINES = 6


class ToolError(Exception):
    pass


class CRM:
    def __init__(self, path: str):
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        self.users = {u["id"]: u for u in data.get("users", [])}
        self.tickets = {t["id"]: t for t in data.get("tickets", [])}


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    if not domain:
        return "***"
    return f"{local[:1]}***@{domain}"


def ticket_view(t: dict) -> dict:
    return {"id": t["id"], "user_id": t["user_id"], "subject": t["subject"], "status": t["status"],
            "product": t.get("product", ""), "created": t.get("created", ""),
            "description": t.get("description", ""), "log": t.get("log", [])[-LOG_LINES:]}


def user_view(u: dict) -> dict:
    return {"id": u["id"], "name": u.get("name", ""), "email": mask_email(u.get("email", "")),
            "plan": u.get("plan", ""), "status": u.get("status", ""), "locked_until": u.get("locked_until"),
            "failed_logins": u.get("failed_logins", 0), "device": u.get("device", "")}


def get_ticket(crm: CRM, args: dict) -> str:
    tid = str(args.get("ticket_id", ""))
    if not TICKET_RE.match(tid):
        raise ToolError("неверный формат номера тикета, нужен вид T-1001")
    t = crm.tickets.get(tid)
    if t is None:
        raise ToolError(f"тикет {tid} не найден")
    return json.dumps(ticket_view(t), ensure_ascii=False, indent=2)


def get_user(crm: CRM, args: dict) -> str:
    uid = str(args.get("user_id", ""))
    if not USER_RE.match(uid):
        raise ToolError("неверный формат номера пользователя, нужен вид U-17")
    u = crm.users.get(uid)
    if u is None:
        raise ToolError(f"пользователь {uid} не найден")
    return json.dumps(user_view(u), ensure_ascii=False, indent=2)


def list_tickets(crm: CRM, args: dict) -> str:
    status = args.get("status")
    rows = [t for t in crm.tickets.values() if not status or t["status"] == status]
    if not rows:
        return "Тикетов нет."
    return "\n".join(f"{t['id']} [{t['status']}] {t['subject']}" for t in rows)


TOOLS = {
    "get_ticket": {
        "description": "Тикет поддержки по номеру: тема, статус, описание, последние строки журнала.",
        "inputSchema": {"type": "object", "properties": {"ticket_id": {"type": "string", "description": "например T-1001"}},
                        "required": ["ticket_id"], "additionalProperties": False},
        "fn": get_ticket,
    },
    "get_user": {
        "description": "Пользователь по номеру: тариф, статус аккаунта, блокировка, устройство. Без секретов.",
        "inputSchema": {"type": "object", "properties": {"user_id": {"type": "string", "description": "например U-17"}},
                        "required": ["user_id"], "additionalProperties": False},
        "fn": get_user,
    },
    "list_tickets": {
        "description": "Список тикетов, можно отфильтровать по статусу (open, closed).",
        "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}, "additionalProperties": False},
        "fn": list_tickets,
    },
}


def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg, crm: CRM):
    if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    if "id" not in msg:
        return None
    mid, method = msg["id"], msg["method"]
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        return _result(mid, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                             "serverInfo": SERVER_INFO})
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": [{"name": n, "description": t["description"], "inputSchema": t["inputSchema"]}
                                       for n, t in TOOLS.items()]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOLS:
            return _error(mid, -32602, f"неизвестный инструмент: {name}")
        if not isinstance(args, dict):
            return _error(mid, -32602, "arguments должен быть объектом")
        try:
            text, is_error = TOOLS[name]["fn"](crm, args), False
        except ToolError as e:
            text, is_error = str(e), True
        except Exception as e:  # noqa: BLE001 — один плохой запрос не должен ронять сервер
            text, is_error = f"внутренняя ошибка: {e}", True
        return _result(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})
    return _error(mid, -32601, f"метод не поддерживается: {method}")


def serve(crm: CRM, lines, out) -> None:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = _error(None, -32700, "Parse error")
        else:
            resp = handle(msg, crm)
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False) + "\n")
            out.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description="MCP-сервер CRM (JSON): тикеты и пользователи, только чтение")
    ap.add_argument("--data", required=True, help="путь к crm.json")
    a = ap.parse_args()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    if not os.path.exists(a.data):
        print(f"mcp_server: файл не найден: {a.data}", file=sys.stderr)
        return 2
    serve(CRM(a.data), sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
