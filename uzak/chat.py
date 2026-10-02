"""Чат с Узаком. Работает и на компе, и на ноутбуке — везде одна и та же беседа.

    python chat.py              # пересканировать проекты этой машины и начать беседу
    python chat.py --no-scan    # сразу в беседу
    python chat.py "вопрос"     # один вопрос и выход
"""
import argparse
import urllib.error

from common import call_brain, load_config, utf8_console
from scanner import push, scan_config

HELP = "Команды: /scan — обновить индекс этой машины, /status — что знает Узак, /reset — забыть беседу, /exit — выход"


def ask(cfg, text):
    return call_brain(cfg, "/chat", {"message": text, "device": cfg["machine"]})["reply"]


def main():
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="*")
    ap.add_argument("--no-scan", action="store_true")
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = load_config(args.config)

    try:
        call_brain(cfg, "/health", timeout=10)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Узак ответил {e.code} — проверь token в config.json.")
    except OSError as e:
        raise SystemExit(f"Узак недоступен по {cfg['brain_url']} ({e}). Комп включён и brain.py запущен?")

    if not args.no_scan:
        r = push(cfg, scan_config(cfg))
        print(f"Индекс {cfg['machine']} обновлён: {r.get('projects')} проектов.")

    if args.question:
        print(ask(cfg, " ".join(args.question)))
        return

    print(f"Узак на связи. {HELP}")
    while True:
        try:
            text = input("\nты> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in ("/exit", "/quit", "выход"):
            break
        try:
            if text == "/scan":
                print(push(cfg, scan_config(cfg)))
            elif text == "/status":
                print(call_brain(cfg, "/status")["status"])
            elif text == "/reset":
                call_brain(cfg, "/reset", {})
                print("Беседа очищена (заметки-память остались).")
            elif text in ("/help", "?"):
                print(HELP)
            else:
                print("\nУзак> " + ask(cfg, text))
        except OSError as e:
            print(f"[связь с Узаком потеряна: {e}]")


if __name__ == "__main__":
    main()
