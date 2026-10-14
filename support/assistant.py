"""
День 33. Ассистент поддержки: отвечает о продукте, учитывает тикет клиента.

Что происходит на каждый вопрос:
  1) если указан тикет — через MCP берутся get_ticket и get_user (только пользователь этого тикета);
  2) по вопросу и теме тикета ищутся фрагменты FAQ и документации (RAG);
  3) модель gpt-5.6-luna отвечает по этим данным и перечисляет источники.

    python support/assistant.py --once "Почему не работает авторизация?" --ticket T-1001
    python support/assistant.py                       # чат; /ticket T-1001 — выбрать тикет, /tickets — список

Ключ AITUNNEL_API_KEY берётся из окружения; если его нет — спрашивается скрытым вводом.
"""

import argparse
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from kb import build_kb, load_kb  # noqa: E402
from mcp_client import MCPClient, MCPToolError  # noqa: E402

DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_URL = "https://api.aitunnel.ru/v1"
TICKET_RE = re.compile(r"^T-\d{3,6}$")
MAX_QUESTION = 1000
KB_CHUNKS = 5
CHUNK_CHARS = 1200

SYSTEM_PROMPT = (
    "Ты — ассистент поддержки сервиса «Пульт». Отвечай клиенту по-русски, вежливо и коротко. "
    "Опирайся на блоки «Тикет», «Пользователь» и «База знаний». "
    "Если данные тикета объясняют проблему, скажи прямо, что видно в журнале и что сделать. "
    "Не проси и не повторяй пароли, коды из SMS и полные e-mail. "
    "Если ответа нет ни в базе знаний, ни в данных тикета, скажи, что передаёшь вопрос оператору, и ничего не выдумывай. "
    "В конце перечисли источники в квадратных скобках: [путь]. Если использовался тикет, добавь строку «Учтён тикет T-…»."
)


class TicketNotFound(LookupError):
    pass


class AITunnelLLM:
    def __init__(self, model: str = DEFAULT_MODEL, api_key: str = None, base_url: str = None, timeout: float = 120):
        self.model = model
        self.api_key = api_key if api_key is not None else os.environ.get("AITUNNEL_API_KEY", "")
        self.base_url = (base_url or os.environ.get("AITUNNEL_BASE_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout

    def chat(self, system: str, user: str) -> tuple:
        if not self.api_key:
            raise RuntimeError("Не задан ключ AITunnel: переменная AITUNNEL_API_KEY пуста.")
        body = json.dumps({"model": self.model, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user}]},
            ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base_url + "/chat/completions", data=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError("AITunnel отклонил ключ (401).") from e
            raise RuntimeError(f"AITunnel: HTTP {e.code} {e.read().decode('utf-8', 'replace')[:300]}") from e
        except OSError as e:
            raise RuntimeError(f"AITunnel недоступен: {e}") from e
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError("AITunnel вернул ответ неожиданного вида") from e
        return text.strip(), data.get("usage") or {}


def make_llm(model: str = None) -> AITunnelLLM:
    llm = AITunnelLLM(model or DEFAULT_MODEL)
    if not llm.api_key and sys.stdin.isatty():
        try:
            llm.api_key = getpass.getpass("Ключ AITunnel (ввод скрыт, в файлы не сохраняется): ").strip()
        except (EOFError, KeyboardInterrupt):
            raise RuntimeError("Ключ не введён.")
    return llm


def _fmt_ticket(t: dict) -> str:
    log = "\n".join(f"  {line}" for line in t.get("log", [])) or "  (журнал пуст)"
    return (f"ID: {t['id']}\nТема: {t['subject']}\nСтатус: {t['status']}\nПродукт: {t.get('product', '')}\n"
            f"Создан: {t.get('created', '')}\nОписание клиента: {t.get('description', '')}\nЖурнал:\n{log}")


def _fmt_user(u: dict) -> str:
    lock = u.get("locked_until") or "нет"
    return (f"Имя: {u.get('name', '')}\nE-mail (маска): {u.get('email', '')}\nТариф: {u.get('plan', '')}\n"
            f"Статус аккаунта: {u.get('status', '')}\nЗаблокирован до: {lock}\n"
            f"Неудачных входов: {u.get('failed_logins', 0)}\nУстройство: {u.get('device', '')}")


class SupportAssistant:
    def __init__(self, kb, llm, mcp: MCPClient):
        self.kb = kb
        self.llm = llm
        self.mcp = mcp

    def tickets(self, status: str = None) -> str:
        args = {"status": status} if status else {}
        return self.mcp.call("list_tickets", args)

    def answer(self, question: str, ticket_id: str = None) -> dict:
        question = (question or "").strip()
        if not question:
            raise ValueError("Пустой вопрос.")
        if len(question) > MAX_QUESTION:
            raise ValueError(f"Вопрос длиннее {MAX_QUESTION} символов.")
        if ticket_id is not None and not TICKET_RE.match(ticket_id):
            raise ValueError("Неверный формат тикета, нужен вид T-1001.")

        calls, blocks, ticket, query = [], [], None, question
        if ticket_id:
            try:
                ticket = json.loads(self.mcp.call("get_ticket", {"ticket_id": ticket_id}))
            except MCPToolError as e:
                raise TicketNotFound(str(e)) from e
            calls.append(f"get_ticket({ticket_id})")
            user = json.loads(self.mcp.call("get_user", {"user_id": ticket["user_id"]}))
            calls.append(f"get_user({ticket['user_id']})")
            blocks.append("=== Тикет (CRM через MCP get_ticket) ===\n" + _fmt_ticket(ticket))
            blocks.append("=== Пользователь (CRM через MCP get_user) ===\n" + _fmt_user(user))
            query = f"{question} {ticket['subject']}"

        hits = self.kb.search(query, k=KB_CHUNKS)
        blocks.append("=== База знаний (FAQ и документация) ===")
        sources = []
        for h in hits:
            blocks.append(f"[{h['source']} — {h['section']}]\n{h['text'][:CHUNK_CHARS]}")
            if h["source"] not in sources:
                sources.append(h["source"])

        user_msg = f"Вопрос клиента: {question}\n\n" + "\n\n".join(blocks)
        text, usage = self.llm.chat(SYSTEM_PROMPT, user_msg)
        return {"answer": text, "sources": sources, "ticket_id": ticket_id if ticket else None,
                "mcp_calls": calls, "usage": usage}


def show(res: dict) -> None:
    print(f"\n{res['answer']}\n")
    if res["sources"]:
        print("Источники: " + "; ".join(res["sources"]))
    if res["mcp_calls"]:
        print("MCP: " + ", ".join(res["mcp_calls"]))
    print()


def run_chat(asst: SupportAssistant) -> None:
    print("Ассистент поддержки «Пульт». /ticket T-1001 — выбрать тикет, /tickets — список, /exit — выход.\n")
    ticket = None
    while True:
        try:
            line = input(f"[{ticket or 'без тикета'}] Вы: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        try:
            if line in ("/exit", "/quit"):
                break
            if line.startswith("/ticket"):
                arg = line[7:].strip()
                if arg in ("", "-"):
                    ticket = None
                    print("Тикет сброшен.\n")
                elif TICKET_RE.match(arg):
                    ticket = arg
                    print(f"Выбран тикет {ticket}.\n")
                else:
                    print("Нужен номер вида T-1001.\n")
            elif line == "/tickets":
                print(asst.tickets() + "\n")
            elif line.startswith("/"):
                print("Неизвестная команда. /exit — выход.\n")
            else:
                show(asst.answer(line, ticket))
        except (RuntimeError, ValueError, LookupError) as e:
            print(f"[ошибка] {e}\n")


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Ассистент поддержки: FAQ + контекст тикета из CRM через MCP")
    ap.add_argument("--data", default=os.path.join(ROOT, "data", "crm.json"), help="JSON CRM")
    ap.add_argument("--index", default=os.path.join(ROOT, "output", "kb-index.sqlite"), help="индекс FAQ")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--once", metavar="ВОПРОС", help="ответить один раз и выйти")
    ap.add_argument("--ticket", help="тикет для --once, например T-1001")
    a = ap.parse_args(argv)
    try:
        os.makedirs(os.path.dirname(a.index), exist_ok=True)
        info = build_kb(ROOT, a.index)
        print(f"База знаний: документов {info['documents']}, фрагментов {info['chunks']}")
        llm = make_llm(a.model)
        with MCPClient(a.data) as mcp:
            asst = SupportAssistant(load_kb(a.index), llm, mcp)
            if a.once is not None:
                show(asst.answer(a.once, a.ticket))
                return 0
            run_chat(asst)
    except (RuntimeError, ValueError, LookupError) as e:
        print(f"[ошибка] {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
