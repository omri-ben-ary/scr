#!/usr/bin/env python3
"""Lean build cache for the cloud lane (docs/proposals/cloud-pool.md §5, revised 21 Oct).

GitHub releases are not available to cloud sessions, so the cache store is Omri's PC: the folder
`<quantum-insertion>/.lake/cloud-cache/master/` holds a zstd tar of `.lake/build` (QuantumInsertion) split into
25 MB parts, a tar of QuantumOptimization's `.lake/build`, MASTER_SHA and SHA256SUMS. `.olean` files are
platform-independent (Mathlib's own cache serves one set to Windows/macOS/Linux; verified 21 Oct: Lake reused
PC-built artifacts unchanged in a Linux container), so either lane can produce the pack and either can restore it.

  pack    <repo>  <out-dir>       tar+zstd the two build dirs, split, write MASTER_SHA + SHA256SUMS
  restore <repo>  <cache-dir>     verify checksums, extract into the repo's .lake (packages must already exist:
                                  run `lake exe cache get` first), print the cache's MASTER_SHA

A cloud session gets the parts with device_stage_files (≤400 MB each, one call) and sends a fresh pack back with
device_commit_files (≤30 MB per file, ≤100 MB per call — hence the 25 MB parts, 4 parts per call).
Needs the `zstandard` wheel (`pip install --break-system-packages zstandard`); no zstd binary required.
"""
from __future__ import annotations

import hashlib
import io
import subprocess
import sys
import tarfile
from pathlib import Path

PART = 25 * 2**20
QI = Path(".lake/build")
QO = Path(".lake/packages/QuantumOptimization/.lake/build")


def _zstd():
    try:
        import zstandard  # noqa: F401
        return __import__("zstandard")
    except ImportError:
        sys.exit("pip install --break-system-packages zstandard")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pack(repo: Path, out: Path) -> None:
    z = _zstd()
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*"):
        old.unlink()
    for name, src in (("qi-build", repo / QI), ("qo-build", repo / QO)):
        buf = io.BytesIO()
        with z.ZstdCompressor(level=3, threads=2).stream_writer(buf, closefd=False) as w, tarfile.open(fileobj=w, mode="w|") as tar:
            tar.add(src, arcname=".")
        data = buf.getvalue()
        if name == "qi-build":
            for i in range(0, len(data), PART):
                (out / f"{name}.tar.zst.part{i // PART:02d}").write_bytes(data[i:i + PART])
        else:
            (out / f"{name}.tar.zst").write_bytes(data)
        print(f"{name}: {len(data) // 2**20} MB")
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    (out / "MASTER_SHA").write_text(sha + "\n")
    files = sorted(p for p in out.iterdir() if p.name not in {"SHA256SUMS", "MASTER_SHA"})
    (out / "SHA256SUMS").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in files))
    print("packed at", sha[:8], "->", out)


def restore(repo: Path, cache: Path) -> None:
    z = _zstd()
    sums = {ln.split()[1]: ln.split()[0] for ln in (cache / "SHA256SUMS").read_text().splitlines() if ln.strip()}
    for name, want in sums.items():
        got = sha256(cache / name)
        if got != want:
            sys.exit(f"checksum mismatch: {name}")
    parts = sorted(cache.glob("qi-build.tar.zst.part*"))
    qi = b"".join(p.read_bytes() for p in parts)
    for data, dst in ((qi, repo / QI), ((cache / "qo-build.tar.zst").read_bytes(), repo / QO)):
        dst.mkdir(parents=True, exist_ok=True)
        with z.ZstdDecompressor().stream_reader(io.BytesIO(data)) as r, tarfile.open(fileobj=r, mode="r|") as tar:
            tar.extractall(dst)
        print(f"restored {dst}")
    print("cache MASTER_SHA", (cache / "MASTER_SHA").read_text().strip()[:12], "- run `lake build` for the delta")


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] not in {"pack", "restore"}:
        sys.exit(__doc__)
    (pack if sys.argv[1] == "pack" else restore)(Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve())
