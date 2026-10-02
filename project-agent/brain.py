"""«Мозг» — запускается ТОЛЬКО на компе. Хранит индекс проектов обеих машин,
общую память и историю беседы, общается с Claude Haiku.

    python brain.py
"""
import hmac
import json
import os
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anthropic

from common import HERE, load_config, utf8_console
from scanner import scan_config

DATA = HERE / "data"
MAX_TOOL_STEPS = 12
MAX_FILE_CHARS = 20_000

SYSTEM_PROMPT = """Ты — личный ассистент пользователя по его проектам. Ты один «мозг», который живёт на его компе (pc),
а говорить с тобой он может и с компа, и с ноутбука (laptop). Беседа общая: что обсуждали с ноутбука, ты помнишь и на компе.

Что ты умеешь:
- знаешь все проекты на обеих машинах (индекс обновляет сканер) — смотри их через инструменты, не выдумывай;
- по проектам на компе можешь читать файлы и git-историю напрямую; по ноутбуку — только данные из индекса
  (README, стек, последний коммит, незакоммиченные файлы);
- запоминаешь важное о пользователе и его планах через remember.

Стиль: говори по-русски, живо и по делу, как толковый напарник. Короткие ответы, если вопрос простой.
Если проект есть на обеих машинах — сравни (где свежее, где незакоммиченные изменения).
Если индекс машины давно не обновлялся — скажи об этом."""

TOOLS = [
    {
        "name": "list_projects",
        "description": "Краткий список проектов (машина, имя, стек, последний коммит, незакоммиченные файлы). "
                       "Можно отфильтровать по машине и/или подстроке в имени/пути.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine": {"type": "string", "description": "pc, laptop или пусто = все"},
                "query": {"type": "string", "description": "подстрока для фильтра по имени/пути"},
            },
        },
    },
    {
        "name": "get_project",
        "description": "Полная карточка проекта из индекса: путь, стек, описание, README, git, файлы верхнего уровня.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "имя проекта или часть имени"},
                "machine": {"type": "string", "description": "pc или laptop (необязательно)"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "search_projects",
        "description": "Полнотекстовый поиск по индексу (имя, путь, описание, README, сообщение коммита).",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "read_project_file",
        "description": "Прочитать файл или показать содержимое папки в проекте НА КОМПЕ (pc). "
                       "path — относительный путь внутри проекта, пусто = корень.",
        "input_schema": {
            "type": "object",
            "properties": {"project": {"type": "string"}, "path": {"type": "string"}},
            "required": ["project"],
        },
    },
    {
        "name": "git_log",
        "description": "Последние коммиты проекта НА КОМПЕ (pc).",
        "input_schema": {
            "type": "object",
            "properties": {"project": {"type": "string"}, "n": {"type": "integer", "description": "сколько, по умолчанию 15"}},
            "required": ["project"],
        },
    },
    {
        "name": "sync_status",
        "description": "Когда и сколько проектов присылала каждая машина.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "remember",
        "description": "Запомнить надолго факт о пользователе, его целях или проектах.",
        "input_schema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]},
    },
    {
        "name": "forget",
        "description": "Удалить заметку из памяти по номеру.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    },
]


class Store:
    """Всё состояние мозга — простые JSON-файлы в data/."""

    def __init__(self):
        DATA.mkdir(exist_ok=True)
        self.lock = threading.RLock()

    def _load(self, name, default):
        p = DATA / name
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return default

    def _save(self, name, obj):
        tmp = DATA / (name + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, DATA / name)

    def set_index(self, machine, scanned_at, projects):
        with self.lock:
            self._save(f"index_{machine}.json", {"machine": machine, "scanned_at": scanned_at, "projects": projects})

    def indexes(self):
        with self.lock:
            return [self._load(p.name, {}) for p in sorted(DATA.glob("index_*.json"))]

    def projects(self, machine=None):
        out = []
        for idx in self.indexes():
            if not machine or idx.get("machine") == machine:
                out.extend(idx.get("projects", []))
        return out

    def history(self):
        return self._load("history.json", [])

    def save_history(self, h):
        self._save("history.json", h)

    def notes(self):
        return self._load("notes.json", [])

    def save_notes(self, n):
        self._save("notes.json", n)


class Brain:
    def __init__(self, cfg):
        self.cfg = cfg
        bcfg = cfg.get("brain", {})
        self.model = bcfg.get("model", "claude-haiku-4-5")
        self.history_turns = int(bcfg.get("history_turns", 30))
        self.machine = cfg["machine"]
        self.store = Store()
        self.client = anthropic.Anthropic()
        self.chat_lock = threading.Lock()

    # ---------- поиск проектов ----------
    def _find(self, name, machine=None):
        name = (name or "").lower().strip()
        ps = self.store.projects(machine or None)
        exact = [p for p in ps if p["name"].lower() == name]
        return exact or [p for p in ps if name in p["name"].lower() or name in p["path"].lower()]

    def _local_project(self, name):
        found = self._find(name, self.machine)
        if not found:
            raise ValueError(f"Проект '{name}' не найден на {self.machine}. Файлы читать можно только с {self.machine}.")
        return Path(found[0]["path"])

    @staticmethod
    def _short(p):
        g = p.get("git") or {}
        line = f"[{p['machine']}] {p['name']} — {', '.join(p['stack']) or '?'} — {p['path']}"
        if g:
            line += f" | коммит {g.get('last_commit_date', '')[:10]}: {g.get('last_commit_msg', '')[:80]}"
            if g.get("uncommitted_files"):
                line += f" | незакоммичено: {g['uncommitted_files']}"
        elif p.get("last_modified"):
            line += f" | изменён {p['last_modified'][:10]}"
        return line

    # ---------- инструменты ----------
    def tool_list_projects(self, machine="", query=""):
        q = (query or "").lower()
        ps = [p for p in self.store.projects(machine or None)
              if not q or q in p["name"].lower() or q in p["path"].lower()]
        return "\n".join(self._short(p) for p in ps) or "Ничего не найдено. Возможно, сканер ещё не присылал индекс."

    def tool_get_project(self, name, machine=""):
        found = self._find(name, machine)
        if not found:
            return f"Проект '{name}' не найден."
        return json.dumps(found[:5], ensure_ascii=False, indent=1)

    def tool_search_projects(self, text):
        t = text.lower()
        hits = []
        for p in self.store.projects():
            g = p.get("git") or {}
            hay = " ".join([p["name"], p["path"], p.get("description", ""), p.get("readme", ""), g.get("last_commit_msg", "")])
            i = hay.lower().find(t)
            if i >= 0:
                hits.append(f"{self._short(p)}\n   …{hay[max(0, i - 80):i + 120].replace(chr(10), ' ')}…")
        return "\n".join(hits[:30]) or "Совпадений нет."

    def tool_read_project_file(self, project, path=""):
        root = self._local_project(project).resolve()
        target = (root / (path or "")).resolve()
        if target != root and root not in target.parents:
            raise ValueError("Путь выходит за пределы проекта.")
        if target.is_dir():
            items = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            return "\n".join(f"{x.name}{'/' if x.is_dir() else ''}" for x in items[:300])
        data = target.read_text(encoding="utf-8", errors="replace")
        if len(data) > MAX_FILE_CHARS:
            data = data[:MAX_FILE_CHARS] + f"\n…[обрезано, всего {len(data)} символов]"
        return data

    def tool_git_log(self, project, n=15):
        from scanner import _git
        out = _git(self._local_project(project), "log", f"-{int(n or 15)}", "--format=%cs %h %s")
        return out or "Нет git-истории."

    def tool_sync_status(self):
        lines = [f"{i.get('machine')}: {len(i.get('projects', []))} проектов, скан {i.get('scanned_at', '?')}"
                 for i in self.store.indexes()]
        return "\n".join(lines) or "Ни одна машина ещё не присылала индекс."

    def tool_remember(self, note):
        with self.store.lock:
            notes = self.store.notes()
            nid = max([n["id"] for n in notes], default=0) + 1
            notes.append({"id": nid, "note": note, "at": datetime.now().isoformat(timespec="minutes")})
            self.store.save_notes(notes)
        return f"Запомнил (#{nid})."

    def tool_forget(self, id):
        with self.store.lock:
            notes = self.store.notes()
            self.store.save_notes([n for n in notes if n["id"] != id])
        return "Удалил."

    def run_tool(self, name, args):
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            return f"Неизвестный инструмент {name}", True
        try:
            return str(fn(**args)), False
        except Exception as e:  # ошибку отдаём модели, пусть сама объяснит
            return f"Ошибка: {e}", True

    # ---------- беседа ----------
    def _system(self):
        notes = self.store.notes()
        mem = "\n".join(f"#{n['id']}: {n['note']}" for n in notes) or "(пока пусто)"
        return f"{SYSTEM_PROMPT}\n\nЧто ты помнишь о пользователе:\n{mem}"

    def _trim(self, history):
        # Режем только по границе «живого» сообщения пользователя (не tool_result).
        starts = [i for i, m in enumerate(history) if m["role"] == "user" and isinstance(m["content"], str)]
        if len(starts) > self.history_turns:
            return history[starts[-self.history_turns]:]
        return history

    def chat(self, message, device):
        with self.chat_lock:
            history = self._trim(self.store.history())
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            history.append({"role": "user", "content": f"[{stamp}, пишу с {device}]\n{message}"})
            try:
                for _ in range(MAX_TOOL_STEPS):
                    resp = self.client.messages.create(
                        model=self.model,
                        max_tokens=4096,
                        system=self._system(),
                        tools=TOOLS,
                        messages=history,
                        cache_control={"type": "ephemeral"},
                    )
                    history.append({"role": "assistant",
                                    "content": [b.model_dump(mode="json", exclude_none=True) for b in resp.content]})
                    if resp.stop_reason != "tool_use":
                        break
                    results = []
                    for b in resp.content:
                        if b.type == "tool_use":
                            out, err = self.run_tool(b.name, b.input or {})
                            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": err})
                    history.append({"role": "user", "content": results})
                else:
                    history.append({"role": "user", "content": "Хватит инструментов, ответь тем, что уже знаешь."})
                    resp = self.client.messages.create(model=self.model, max_tokens=4096, system=self._system(),
                                                       tools=TOOLS, messages=history)
                    history.append({"role": "assistant",
                                    "content": [b.model_dump(mode="json", exclude_none=True) for b in resp.content]})
            except anthropic.AuthenticationError:
                return "Мозг не может войти в Claude API: проверь ANTHROPIC_API_KEY на компе."
            except anthropic.RateLimitError:
                return "Упёрлись в лимит Claude API, попробуй через минуту."
            except anthropic.APIStatusError as e:
                return f"Claude API вернул ошибку {e.status_code}: {e.message}"
            except anthropic.APIConnectionError:
                return "Нет связи с Claude API (интернет на компе?)."
            # Сохраняем, только если ход завершился без ошибок — без «висящих» tool_use.
            self.store.save_history(history)
            return "".join(b.text for b in resp.content if b.type == "text").strip() or "(пустой ответ)"


def make_handler(brain, token):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            got = self.headers.get("Authorization", "")
            if hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
                return True
            self._send(401, {"error": "bad token"})
            return False

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

        def do_GET(self):
            if not self._authed():
                return
            if self.path == "/health":
                self._send(200, {"ok": True, "machine": brain.machine, "model": brain.model})
            elif self.path == "/status":
                self._send(200, {"status": brain.tool_sync_status()})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                return
            try:
                data = self._body()
                if self.path == "/chat":
                    reply = brain.chat(str(data.get("message", "")), str(data.get("device", "?")))
                    self._send(200, {"reply": reply})
                elif self.path == "/index":
                    projects = data.get("projects", [])
                    brain.store.set_index(str(data["machine"]), data.get("scanned_at", ""), projects)
                    self._send(200, {"ok": True, "machine": data["machine"], "projects": len(projects)})
                elif self.path == "/reset":
                    brain.store.save_history([])
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"error": "not found"})
            except Exception as e:
                traceback.print_exc()
                self._send(500, {"error": str(e)})

        def log_message(self, fmt, *args):
            print(f"[{datetime.now():%H:%M:%S}] {self.client_address[0]} {fmt % args}")

    return Handler


def rescan_loop(brain, minutes):
    """Мозг сам периодически пересканирует проекты своей машины."""
    while True:
        try:
            projects = scan_config(brain.cfg)
            brain.store.set_index(brain.machine, datetime.now().astimezone().isoformat(), projects)
            print(f"[скан] {brain.machine}: {len(projects)} проектов")
        except Exception:
            traceback.print_exc()
        time.sleep(minutes * 60)


def main():
    utf8_console()
    cfg = load_config()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Внимание: ANTHROPIC_API_KEY не задан — мозг не сможет отвечать.")
    brain = Brain(cfg)
    bcfg = cfg.get("brain", {})
    threading.Thread(target=rescan_loop, args=(brain, float(bcfg.get("rescan_minutes", 30))), daemon=True).start()
    host, port = bcfg.get("host", "0.0.0.0"), int(bcfg.get("port", 8765))
    print(f"Мозг запущен на {host}:{port}, модель {brain.model}. Ctrl+C — остановить.")
    ThreadingHTTPServer((host, port), make_handler(brain, cfg["token"])).serve_forever()


if __name__ == "__main__":
    main()
