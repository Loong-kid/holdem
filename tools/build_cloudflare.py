"""Prepare an allowlisted Worker package and static assets; never copy secrets."""
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent.parent


def write_if_changed(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.read_bytes() == data:
        return
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(target)  # Watchers see the complete module, never half a Python string.


def copy_if_changed(source, target):
    write_if_changed(target, source.read_bytes())


def main():
    for package in ("poker", "gto"):
        for source in (ROOT / package).glob("*.py"):
            target = ROOT / "cloudflare" / package / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_if_changed(source, target)
    data = (ROOT / "chart_db.json").read_text(encoding="utf-8")
    write_if_changed(ROOT / "cloudflare/chart_data.py",
                     ("import json\nCHART_DB = json.loads(" + repr(data) + ")\n").encode())
    public = ROOT / ".cloudflare-assets"
    public.mkdir(exist_ok=True)
    for source in (ROOT / "static").glob("*"):
        if source.suffix in (".html", ".js", ".css"):
            target = public / "static" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_if_changed(source, target)
    copy_if_changed(ROOT / "static/index.html", public / "index.html")
    (public / "stats").mkdir(exist_ok=True)
    copy_if_changed(ROOT / "static/stats.html", public / "stats/index.html")
    # A rewrite from /stats to stats.html loops with native clean-URL redirects.
    old_stats = public / "stats.html"
    if old_stats.is_file():
        old_stats.unlink()
    manifest = json.loads((ROOT / "trainer/manifest.json").read_text(encoding="utf-8"))
    for name in [*manifest["files"], "manifest.json"]:
        target = public / "trainer" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        copy_if_changed(ROOT / "trainer" / name, target)
    (public / "_redirects").write_text("/trainer /trainer/ 301\n", encoding="utf-8")
    (public / "_headers").write_text(
        "/*\n  Cache-Control: no-cache, must-revalidate\n  X-Content-Type-Options: nosniff\n",
        encoding="utf-8")
    print("Prepared Python packages, GTO data, and static/trainer assets")


if __name__ == "__main__":
    main()
