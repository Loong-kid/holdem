"""Bundle the sibling range-trainer into this repo for standalone Render deploys.

Run: py sync_trainer.py [--source ../range-trainer] [--check]
Only browser runtime files are bundled; PDFs, histories, tests and tooling stay out.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

FILES = (
    "index.html", "style.css", "app.js", "core.js", "ev.js", "library.js",
    "lib/ranges.js", "data/ranges.js", "data/equity.js",
)
HERE = Path(__file__).resolve().parent


def bundle(source):
    # Canonical LF keeps hashes identical on Windows and Render's Linux checkout.
    payload = {name: (source / name).read_bytes().replace(b"\r\n", b"\n") for name in FILES}
    version = hashlib.sha256(b"".join(name.encode() + payload[name] for name in FILES)).hexdigest()[:12]
    html = payload["index.html"].decode("utf-8")
    html = re.sub(r'((?:src|href)=")([^"?]+\.(?:js|css))"',
                  lambda m: f'{m[1]}{m[2]}?v={version}"', html)
    payload["index.html"] = html.encode("utf-8")
    manifest = {"version": version, "files": {name: hashlib.sha256(data).hexdigest()
                                              for name, data in payload.items()}}
    payload["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=HERE.parent / "range-trainer")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    payload = bundle(args.source.resolve())
    destination = HERE / "trainer"
    if args.check:
        mismatch = [name for name, data in payload.items()
                    if not (destination / name).is_file() or (destination / name).read_bytes() != data]
        if mismatch:
            parser.exit(1, "Trainer bundle out of sync: " + ", ".join(mismatch) + "\n")
        print("Trainer bundle matches source (9 runtime files + manifest)")
        return
    for name, data in payload.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print(f"Bundled {len(FILES)} trainer runtime files into {destination}")


if __name__ == "__main__":
    main()
