"""Сканирует папки с проектами на этой машине и отправляет индекс «мозгу».

    python scanner.py            # просканировать и отправить
    python scanner.py --print    # только показать, что найдено
"""
import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from common import call_brain, load_config, utf8_console

# Файл/папка-признак -> технология. Папка с любым из них считается проектом.
MARKERS = {
    ".git": "git",
    "package.json": "node",
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "setup.py": "python",
    "Cargo.toml": "rust",
    "go.mod": "go",
    "pom.xml": "java",
    "build.gradle": "java/kotlin",
    "build.gradle.kts": "kotlin",
    "composer.json": "php",
    "Gemfile": "ruby",
    "pubspec.yaml": "flutter",
    "SKILL.md": "claude-skill",
    "CLAUDE.md": "claude",
    "Dockerfile": "docker",
}
SUFFIX_MARKERS = {".sln": "c#", ".csproj": "c#", ".uproject": "unreal", ".ipynb": "jupyter"}

SKIP_DIRS = {
    "node_modules", "venv", "env", "__pycache__", "dist", "build", "target", "bin", "obj",
    "AppData", "Library", "Windows", "Program Files", "Program Files (x86)", "ProgramData",
    "$Recycle.Bin", "System Volume Information", "site-packages",
}


def _git(path, *args):
    try:
        r = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _read_head(path, limit):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _description(path, names):
    if "package.json" in names:
        try:
            return json.loads(_read_head(path / "package.json", 200_000)).get("description", "")
        except ValueError:
            pass
    if "pyproject.toml" in names:
        for line in _read_head(path / "pyproject.toml", 20_000).splitlines():
            if line.strip().startswith("description"):
                return line.split("=", 1)[-1].strip().strip("\"'")
    return ""


def describe_project(path, names, machine):
    stack = sorted({MARKERS[n] for n in names if n in MARKERS}
                   | {v for n in names for s, v in SUFFIX_MARKERS.items() if n.endswith(s)})
    readme = next((n for n in names if n.lower().startswith("readme")), None)
    mtimes = []
    for n in names:
        try:
            mtimes.append((path / n).stat().st_mtime)
        except OSError:
            pass
    info = {
        "machine": machine,
        "name": path.name,
        "path": str(path),
        "stack": stack,
        "description": _description(path, names),
        "readme": _read_head(path / readme, 1500) if readme else "",
        "top_level": sorted(names)[:40],
        "last_modified": datetime.fromtimestamp(max(mtimes), timezone.utc).isoformat() if mtimes else "",
    }
    if ".git" in names:
        last = _git(path, "log", "-1", "--format=%cI|%s")
        date, _, msg = last.partition("|")
        status = _git(path, "status", "--porcelain")
        info["git"] = {
            "branch": _git(path, "rev-parse", "--abbrev-ref", "HEAD"),
            "last_commit_date": date,
            "last_commit_msg": msg,
            "uncommitted_files": len(status.splitlines()) if status else 0,
            "remote": _git(path, "remote", "get-url", "origin"),
            "commits": _git(path, "rev-list", "--count", "HEAD"),
        }
    return info


def scan(roots, max_depth, machine):
    projects, seen = [], set()
    stack = [(Path(os.path.expanduser(r)), 0) for r in roots]
    while stack:
        path, depth = stack.pop()
        try:
            real = path.resolve()
            if real in seen:
                continue
            seen.add(real)
            entries = list(os.scandir(path))
        except OSError:
            continue
        names = [e.name for e in entries]
        if any(n in MARKERS for n in names) or any(n.endswith(s) for n in names for s in SUFFIX_MARKERS):
            projects.append(describe_project(path, names, machine))
            continue  # внутрь проекта не лезем
        if depth >= max_depth:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False) and not e.name.startswith(".") and e.name not in SKIP_DIRS:
                    stack.append((Path(e.path), depth + 1))
            except OSError:
                pass
    return sorted(projects, key=lambda p: p["path"].lower())


def scan_config(cfg):
    return scan(cfg.get("scan_roots", []), int(cfg.get("scan_max_depth", 4)), cfg["machine"])


def push(cfg, projects):
    return call_brain(cfg, "/index", {
        "machine": cfg["machine"],
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "projects": projects,
    })


def main():
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print", action="store_true", help="только вывести результат, не отправлять")
    ap.add_argument("--config")
    args = ap.parse_args()
    cfg = load_config(args.config)
    projects = scan_config(cfg)
    if args.print:
        print(json.dumps(projects, ensure_ascii=False, indent=2))
        return
    print(f"Найдено проектов: {len(projects)}. Отправляю в мозг {cfg['brain_url']} ...")
    print(push(cfg, projects))


if __name__ == "__main__":
    main()
