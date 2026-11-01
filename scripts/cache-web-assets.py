#!/usr/bin/env python3
"""Cache a pinned HLS player locally; never load scripts from a CDN at runtime."""
import argparse
import hashlib
from pathlib import Path
import urllib.request
import zipfile

VERSION = "1.7.4"
ARCHIVE_SHA256 = "933978ce8ec55b80216c2447b22d5c22c388a086612b88ee8382b3f70bd80165"
JS_SHA256 = "cfc70cc9e9fc63d071919e087670abc760c8fd1bc889b88588026e4d22193bbe"
LICENSE_SHA256 = "ca8773cf798c7ed997d4dd7c8e23c348699f8d5b7462636694cc14de6cda12db"

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--apply", action="store_true")
p.add_argument("--cache", type=Path, default=Path.home() / ".cache/yoosee-cam-utils" / f"hls-v{VERSION}")
a = p.parse_args()
if not a.apply:
    print(f"Would verify/cache hls.js {VERSION} under {a.cache}")
else:
    a.cache.mkdir(parents=True, exist_ok=True)
    archive = a.cache / "release.zip"
    if not archive.exists():
        with urllib.request.urlopen(f"https://github.com/video-dev/hls.js/releases/download/v{VERSION}/release.zip", timeout=40) as r:
            data = r.read(12 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
            raise SystemExit("HLS archive digest mismatch")
        archive.write_bytes(data)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise SystemExit("HLS archive digest mismatch")
    with zipfile.ZipFile(archive) as z:
        data = z.read("dist/hls.min.js")
    if hashlib.sha256(data).hexdigest() != JS_SHA256:
        raise SystemExit("HLS JavaScript digest mismatch")
    (a.cache / "hls.min.js").write_bytes(data)
    license_file = a.cache / "LICENSE"
    if not license_file.exists():
        with urllib.request.urlopen(f"https://raw.githubusercontent.com/video-dev/hls.js/v{VERSION}/LICENSE", timeout=15) as r:
            license_file.write_bytes(r.read(100000))
    if hashlib.sha256(license_file.read_bytes()).hexdigest() != LICENSE_SHA256:
        raise SystemExit("HLS license digest mismatch")
    print(f"Verified hls.js {VERSION}: {JS_SHA256}")
