"""
День 33. Проверки ассистента поддержки на имитации: без интернета, без ключа AITunnel.

  1) MCP-сервер CRM: протокол и инструменты, маскирование, секреты не отдаются
  2) База знаний: FAQ и документация попадают в индекс, поиск находит нужный раздел
  3) Ответ с учётом тикета: данные тикета и пользователя попадают в запрос модели
  4) Приватность: чужой пользователь не попадает в запрос, почта маскируется
  5) HTTP-сервис: ключ, маршруты, коды ответов, ограничения тела
  6) Сквозной запуск CLI с тикетом

    python test_day33.py      # отчёт: output/test_report.md
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SUPPORT = os.path.join(HERE, "support")
sys.path.insert(0, SUPPORT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from assistant import AITunnelLLM, SupportAssistant, TicketNotFound  # noqa: E402
from kb import build_kb, load_kb  # noqa: E402
from mcp_client import MCPClient, MCPToolError  # noqa: E402

STUB_KEY = "stub-only-key-0003"
SVC_KEY = "service-stub-key-0003-long"
DATA = os.path.join(HERE, "data", "crm.json")
RESULTS = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("ok    " if ok else "FAIL  ") + name + ("" if ok or not detail else f"  — {detail}"))


class RawServer:
    def __init__(self):
        env = {**os.environ, "PYTHONUTF8": "1"}
        self.p = subprocess.Popen([sys.executable, os.path.join(SUPPORT, "mcp_server.py"), "--data", DATA],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)

    def send(self, msg) -> None:
        data = msg if isinstance(msg, bytes) else (json.dumps(msg) + "\n").encode("utf-8")
        self.p.stdin.write(data)
        self.p.stdin.flush()

    def recv(self) -> dict:
        return json.loads(self.p.stdout.readline().decode("utf-8"))

    def close(self) -> None:
        self.p.stdin.close()
        self.p.wait(timeout=5)


def test_mcp() -> None:
    s = RawServer()
    try:
        s.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
        r = s.recv()
        check("MCP initialize: версия протокола и имя сервера",
              r.get("result", {}).get("serverInfo", {}).get("name") == "support-crm", str(r)[:150])
        s.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        s.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in s.recv()["result"]["tools"]}
        check("MCP tools/list: get_ticket, get_user, list_tickets", {"get_ticket", "get_user", "list_tickets"} <= names,
              str(names))
        s.send("это не json\n".encode("utf-8"))
        check("MCP битый JSON → -32700", s.recv().get("error", {}).get("code") == -32700, "")
    finally:
        s.close()

    with MCPClient(DATA) as m:
        t = json.loads(m.call("get_ticket", {"ticket_id": "T-1001"}))
        check("get_ticket T-1001: тема и последние строки журнала",
              t["subject"] == "Не работает авторизация" and len(t["log"]) >= 1, str(t)[:150])
        u = json.loads(m.call("get_user", {"user_id": "U-17"}))
        check("get_user U-17: статус заблокирован и дата разблокировки",
              u["status"] == "заблокирован" and u["locked_until"] == "2026-10-14T15:30:00+03:00", str(u)[:150])
        check("get_user: полный e-mail не отдаётся, есть маска",
              "anna.petrova@" not in json.dumps(u) and u["email"] == "a***@example.com", u.get("email", ""))
        raw_all = m.call("get_user", {"user_id": "U-17"}) + m.call("get_user", {"user_id": "U-42"}) \
            + m.call("get_ticket", {"ticket_id": "T-1002"})
        check("секреты: хеши паролей нигде не отдаются", "$2b$12$" not in raw_all and "password" not in raw_all,
              "")
        try:
            m.call("get_ticket", {"ticket_id": "T-9999"})
            check("неизвестный тикет → понятная ошибка", False, "ошибки не было")
        except MCPToolError as e:
            check("неизвестный тикет → понятная ошибка", "не найден" in str(e), str(e))
        for bad in ["../../etc/passwd", "T-1; rm", ""]:
            try:
                m.call("get_ticket", {"ticket_id": bad})
                check(f"неверный номер «{bad}» отклоняется", False, "ошибки не было")
            except MCPToolError as e:
                check(f"неверный номер «{bad or 'пустой'}» отклоняется", "неверный формат" in str(e), str(e))
        lst = m.call("list_tickets", {"status": "open"})
        check("list_tickets: только открытые тикеты", "T-1001" in lst and "T-1004" not in lst, lst[:150])


def test_kb(tmp: str) -> str:
    idx = os.path.join(tmp, "kb.sqlite")
    info = build_kb(HERE, idx)
    check("база знаний: FAQ и документация в индексе",
          {"docs/auth.md", "docs/faq.md", "docs/billing.md"} <= set(info["sources"]), str(info["sources"]))
    check("база знаний: данные CRM и код в индекс не попадают",
          not any(s.startswith(("data/", "support/")) for s in info["sources"]), str(info["sources"]))
    kb = load_kb(idx)
    cases = [
        ("как сбросить пароль, письмо не приходит", "docs/auth.md"),
        ("код из SMS не приходит, аккаунт заблокирован", "docs/auth.md"),
        ("двойное списание за подписку", "docs/billing.md"),
        ("чем Basic отличается от Pro, тариф", "docs/faq.md"),
    ]
    for q, want in cases:
        top = [h["source"] for h in kb.search(q, k=5)]
        check(f"поиск «{q}» находит {want}", want in top, f"топ-5: {top}")
    return idx


class StubHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        StubHandler.requests.append({"auth": self.headers.get("Authorization", ""), "body": body})
        if self.headers.get("Authorization", "") != f"Bearer {STUB_KEY}":
            return self._send(401, {"error": {"message": "invalid api key"}})
        user = body["messages"][-1]["content"]
        if "Статус аккаунта: заблокирован" in user:
            text = ("[имитация] Аккаунт заблокирован после 5 неверных попыток до 15:30 по данным тикета. "
                    "Подождите или передайте вопрос оператору. [docs/auth.md]\nУчтён тикет T-1001")
        elif "=== Тикет" in user:
            text = "[имитация] Тикет учтён, но подробностей в данных нет. [docs/faq.md]\nУчтён тикет"
        else:
            text = "[имитация] Укажите номер тикета, чтобы я посмотрел данные вашего аккаунта. [docs/faq.md]"
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": text}}],
                         "usage": {"prompt_tokens": 50, "completion_tokens": 20}})


def start_stub():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


def test_assistant(idx: str, base: str) -> None:
    StubHandler.requests.clear()
    kb = load_kb(idx)
    with MCPClient(DATA) as mcp:
        llm = AITunnelLLM(api_key=STUB_KEY, base_url=base)
        asst = SupportAssistant(kb, llm, mcp)

        res = asst.answer("Почему не работает авторизация?", "T-1001")
        req = StubHandler.requests[-1]
        user = req["body"]["messages"][-1]["content"]
        check("запрос уходит с заголовком Bearer и моделью gpt-5.6-luna",
              req["auth"] == f"Bearer {STUB_KEY}" and req["body"]["model"] == "gpt-5.6-luna", "")
        check("контекст: данные тикета T-1001 попадают в запрос модели",
              "ID: T-1001" in user and "Журнал:" in user and "account_locked" in user, "")
        check("контекст: данные пользователя (статус, блокировка) попадают в запрос",
              "Статус аккаунта: заблокирован" in user and "2026-10-14T15:30" in user, "")
        check("ответ учитывает тикет: назван статус блокировки и время",
              "заблокирован" in res["answer"] and "15:30" in res["answer"], res["answer"][:200])
        check("ответ содержит строку «Учтён тикет T-1001»", "Учтён тикет T-1001" in res["answer"], "")
        check("вызовы MCP перечислены: get_ticket и get_user",
              res["mcp_calls"] == ["get_ticket(T-1001)", "get_user(U-17)"], str(res["mcp_calls"]))
        check("источники: раздел базы знаний про блокировку", "docs/auth.md" in res["sources"], str(res["sources"]))

        check("приватность: чужой пользователь (Дмитрий, U-42) не попадает в запрос",
              "Дмитрий" not in user and "dmitry.sokolov" not in user and "U-42" not in user, "")
        check("приватность: полный e-mail клиента не попадает в запрос",
              "anna.petrova@example.com" not in user, "")
        check("приватность: хеш пароля не попадает в запрос", "$2b$12$" not in user, "")

        res2 = asst.answer("Почему не работает авторизация?", None)
        user2 = StubHandler.requests[-1]["body"]["messages"][-1]["content"]
        check("без тикета: блока тикета в запросе нет, MCP не вызывается",
              "=== Тикет" not in user2 and res2["mcp_calls"] == [], str(res2["mcp_calls"]))
        check("без тикета: ассистент просит номер тикета", "Укажите номер тикета" in res2["answer"], "")

        try:
            asst.answer("Что у меня?", "T-9999")
            check("неизвестный тикет в ассистенте → TicketNotFound", False, "ошибки не было")
        except TicketNotFound:
            check("неизвестный тикет в ассистенте → TicketNotFound", True)

        for q, t, label in [("", None, "пустой вопрос"), ("а" * 1001, None, "длинный вопрос"),
                            ("вопрос", "T-1; DROP", "неверный номер тикета")]:
            try:
                asst.answer(q, t)
                check(f"проверка ввода: {label} отклоняется", False, "ошибки не было")
            except ValueError:
                check(f"проверка ввода: {label} отклоняется", True)

        tickets = asst.tickets("open")
        check("/tickets через MCP: открытые тикеты", "T-1003" in tickets, tickets[:120])


def http(method: str, url: str, body: bytes = None, headers: dict = None):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return e.code, {}


def test_service(idx: str, base: str, tmp: str) -> None:
    env_patch = {"AITUNNEL_BASE_URL": base, "AITUNNEL_API_KEY": STUB_KEY, "SUPPORT_API_KEY": SVC_KEY}
    old = {k: os.environ.get(k) for k in env_patch}
    os.environ.update(env_patch)
    try:
        import service  # noqa: WPS433 — импорт после переменных окружения
        svc = service.Service(DATA, idx)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), service.make_handler(svc, SVC_KEY))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            code, body = http("GET", url + "/health")
            check("HTTP GET /health → 200", code == 200 and body.get("status") == "ok", str(code))

            payload = json.dumps({"question": "Почему не работает авторизация?", "ticket_id": "T-1001"}).encode()
            code, body = http("POST", url + "/api/ask", payload, {"Content-Type": "application/json"})
            check("HTTP POST без ключа → 401", code == 401, str(code))
            code, body = http("POST", url + "/api/ask", payload,
                              {"Content-Type": "application/json", "X-Api-Key": "wrong-key-0000000000"})
            check("HTTP POST с неверным ключом → 401", code == 401, str(code))
            code, body = http("POST", url + "/api/ask", payload,
                              {"Content-Type": "application/json", "X-Api-Key": SVC_KEY})
            check("HTTP POST с ключом и тикетом → 200 и ответ по тикету",
                  code == 200 and "Учтён тикет T-1001" in body.get("answer", "") and body.get("ticket_id") == "T-1001",
                  f"{code} {str(body)[:150]}")
            code, body = http("POST", url + "/api/ask", json.dumps({"question": "Как вернуть деньги?", "ticket_id": "T-9999"}).encode(),
                              {"Content-Type": "application/json", "X-Api-Key": SVC_KEY})
            check("HTTP неизвестный тикет → 404", code == 404, str(code))
            code, body = http("POST", url + "/api/ask", "не json".encode("utf-8"), {"X-Api-Key": SVC_KEY})
            check("HTTP тело не JSON → 400", code == 400, str(code))
            code, body = http("POST", url + "/api/ask", json.dumps({"question": ""}).encode(), {"X-Api-Key": SVC_KEY})
            check("HTTP пустой вопрос → 400", code == 400, str(code))
            code, body = http("POST", url + "/api/ask", b"x" * (17 * 1024), {"X-Api-Key": SVC_KEY})
            check("HTTP тело больше 16 КБ → 413", code == 413, str(code))
            code, body = http("GET", url + "/nope")
            check("HTTP неизвестный маршрут → 404", code == 404, str(code))
            check("HTTP ответ сервиса не содержит ключей",
                  SVC_KEY not in json.dumps(body) and STUB_KEY not in json.dumps(body), "")
        finally:
            srv.shutdown()
            svc.close()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    env = {k: v for k, v in os.environ.items() if k != "SUPPORT_API_KEY"}
    p = subprocess.run([sys.executable, os.path.join(SUPPORT, "service.py"), "--port", "0"], capture_output=True,
                       env=env, timeout=60, stdin=subprocess.DEVNULL)
    out = p.stdout.decode("utf-8", "replace")
    check("сервис без SUPPORT_API_KEY не запускается (код 2)", p.returncode == 2 and "SUPPORT_API_KEY" in out, out[:150])


def test_cli(base: str, tmp: str) -> None:
    env = {**os.environ, "PYTHONUTF8": "1", "AITUNNEL_BASE_URL": base, "AITUNNEL_API_KEY": STUB_KEY}
    idx = os.path.join(tmp, "cli-kb.sqlite")
    p = subprocess.run([sys.executable, os.path.join(SUPPORT, "assistant.py"), "--index", idx,
                        "--data", DATA, "--once", "Почему не работает авторизация?", "--ticket", "T-1001"],
                       capture_output=True, env=env, timeout=120, stdin=subprocess.DEVNULL)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    check("CLI --once --ticket: код 0", p.returncode == 0, out[-300:])
    check("CLI: ответ с учётом тикета и строка MCP-вызовов",
          "Учтён тикет T-1001" in out and "get_ticket(T-1001)" in out, out[-300:])
    check("CLI: ключ модели не виден в выводе", STUB_KEY not in out, "")


def write_report(started: str) -> None:
    os.makedirs(os.path.join(HERE, "output"), exist_ok=True)
    ok = sum(1 for _, good, _ in RESULTS if good)
    lines = [
        "# Отчёт проверок дня 33 (имитация)",
        "",
        f"Запуск: {started}. Результат: **{ok} из {len(RESULTS)}** проверок пройдено.",
        "",
        "На имитации проверено: MCP-сервер CRM (протокол, инструменты, маскирование, секреты), база знаний,",
        "ответ с учётом тикета (запрос модели), приватность, HTTP-сервис (ключ, коды, лимиты), CLI.",
        "",
        "Не проверено здесь: настоящий AITunnel и ответ gpt-5.6-luna, настоящая CRM (данные здесь вымышленные),",
        "развёртывание за HTTPS-прокси, работа на ПК пользователя.",
        "",
        "| № | Проверка | Результат |",
        "|---|---|---|",
    ]
    for i, (name, good, _) in enumerate(RESULTS, 1):
        lines.append(f"| {i} | {name} | {'пройдено' if good else '**НЕ пройдено**'} |")
    with open(os.path.join(HERE, "output", "test_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    started = datetime.now().strftime("%Y-%m-%d %H:%M")
    tmp = tempfile.mkdtemp(prefix="day33-test-")
    srv, base = start_stub()
    try:
        print("== 1. MCP-сервер CRM")
        test_mcp()
        print("\n== 2. База знаний")
        idx = test_kb(tmp)
        print("\n== 3–4. Ответ с учётом тикета и приватность")
        test_assistant(idx, base)
        print("\n== 5. HTTP-сервис")
        test_service(idx, base, tmp)
        print("\n== 6. CLI")
        test_cli(base, tmp)
    finally:
        srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    write_report(started)
    failed = [n for n, good, _ in RESULTS if not good]
    print(f"\nИтог: {len(RESULTS) - len(failed)} из {len(RESULTS)}. Отчёт: output\\test_report.md")
    if failed:
        print("Не пройдено:\n  " + "\n  ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
