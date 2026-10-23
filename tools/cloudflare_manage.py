"""Build and run/deploy the Cloudflare backend with the project's pinned tools."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("dev", "deploy", "build"))
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--persist-to", default=".wrangler/state")
    args = parser.parse_args()
    subprocess.run([sys.executable, str(ROOT / "tools/build_cloudflare.py")], cwd=ROOT, check=True)
    if args.action == "build":
        return
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    uv = ROOT / ".venv/Scripts/uv.exe" if os.name == "nt" else ROOT / ".venv/bin/uv"
    if not uv.is_file():
        raise SystemExit("Run npm.cmd install and uv sync first")
    config = json.loads((ROOT / "wrangler.jsonc").read_text(encoding="utf-8"))
    if args.action == "deploy" and config["d1_databases"][0]["database_id"].startswith("00000000"):
        raise SystemExit("Create a D1 database and configure its ID before deployment")
    location = ["--remote"] if args.action == "deploy" else ["--local", "--persist-to", args.persist_to]
    subprocess.run([npm, "exec", "wrangler", "--", "d1", "migrations", "apply", "DB", *location], cwd=ROOT, check=True)
    command = [str(uv), "run", "pywrangler", args.action]
    if args.action == "dev":
        command += ["--port", str(args.port), "--ip", "127.0.0.1", "--persist-to", args.persist_to]
    raise SystemExit(subprocess.call(command, cwd=ROOT))


if __name__ == "__main__":
    main()
