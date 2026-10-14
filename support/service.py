"""
День 33. Мини-сервис поддержки: HTTP API над ассистентом.

    POST /api/ask      {"question": "...", "ticket_id": "T-1001"}   → ответ, источники, вызовы MCP
    GET  /health       → {"status": "ok"}

Безопасность:
  • нужен заголовок X-Api-Key, он сравнивается с переменной SUPPORT_API_KEY (без неё сервис не запустится);
  • слушает только 127.0.0.1 по умолчанию; наружу выставляйте через свой прокси с HTTPS;
  • тело запроса не больше 16 КБ, вопрос не больше 1000 символов.

    $env:SUPPORT_API_KEY = "<придумайте длинный ключ>"   # только в этом окне PowerShell
    python support/service.py --port 8080
"""

import argparse
import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from assistant import SupportAssistant, TicketNotFound, make_llm  # noqa: E402
from kb import build_kb, load_kb  # noqa: E402
from mcp_client import MCPClient  # noqa: E402

MAX_BODY = 16 * 1024


class Service:
    def __init__(self, data: str, index: str, model: str = None):
        os.makedirs(os.path.dirname(index), exist_ok=True)
        build_kb(ROOT, index)
        self.mcp = MCPClient(data)
        self.assistant = SupportAssistant(load_kb(index), make_llm(model), self.mcp)
        self.lock = threading.Lock()  # один MCP-процесс на все запросы: вызовы идут по очереди

    def ask(self, question: str, ticket_id: str = None) -> dict:
        with self.lock:
            return self.assistant.answer(question, ticket_id)

    def close(self) -> None:
        self.mcp.close()


def make_handler(service: Service, api_key: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # в консоль не печатаем тексты вопросов
            pass

        def _json(self, code: int, obj: dict) -> None:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                return self._json(200, {"status": "ok"})
            return self._json(404, {"error": "нет такого маршрута"})

        def do_POST(self):
            if self.path != "/api/ask":
                return self._json(404, {"error": "нет такого маршрута"})
            given = self.headers.get("X-Api-Key", "")
            if not hmac.compare_digest(given.encode("utf-8"), api_key.encode("utf-8")):
                return self._json(401, {"error": "неверный или отсутствующий ключ X-Api-Key"})
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > MAX_BODY:
                self.close_connection = True
                return self._json(413, {"error": "запрос больше 16 КБ"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "тело должно быть JSON"})
            if not isinstance(body, dict):
                return self._json(400, {"error": "тело должно быть JSON-объектом"})
            question = body.get("question")
            ticket_id = body.get("ticket_id")
            if ticket_id is not None and not isinstance(ticket_id, str):
                return self._json(400, {"error": "ticket_id должен быть строкой"})
            try:
                res = service.ask(question if isinstance(question, str) else "", ticket_id)
            except TicketNotFound:
                return self._json(404, {"error": "тикет не найден"})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except RuntimeError:
                return self._json(502, {"error": "ассистент временно недоступен, передайте вопрос оператору"})
            return self._json(200, res)

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Мини-сервис поддержки")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--data", default=os.path.join(ROOT, "data", "crm.json"))
    ap.add_argument("--index", default=os.path.join(ROOT, "output", "kb-index.sqlite"))
    ap.add_argument("--model", default=None)
    a = ap.parse_args(argv)

    api_key = os.environ.get("SUPPORT_API_KEY", "")
    if len(api_key) < 16:
        print("Сервис не запущен: задайте SUPPORT_API_KEY длиной не меньше 16 символов.")
        return 2
    if not os.environ.get("AITUNNEL_API_KEY"):
        print("Сервис не запущен: задайте AITUNNEL_API_KEY (ключ модели) в этом окне.")
        return 2
    service = Service(a.data, a.index, a.model)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(service, api_key))
    print(f"Сервис поддержки слушает http://{a.host}:{a.port} (остановить: Ctrl+C)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        service.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
