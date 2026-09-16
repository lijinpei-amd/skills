#!/usr/bin/env python3
"""Download the public AMD GPU ISA source documents and record their provenance.

Every source is fetched from its canonical public URL. For each one we record the
URL we asked for, the URL we were finally served (AMD redirects a lot), the HTTP
status, the content type, the byte count and the sha256. Anything that does not
come back as a real PDF/zip is recorded as FAILED rather than silently saved --
amd.com now answers some of these with an HTML viewer page instead of the file.

Re-running is cheap: a file whose sha256 already matches the manifest is skipped.

Usage:  python3 scripts/fetch_sources.py [--force]
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SOURCES_DIR = os.path.join(ROOT, "sources")
MANIFEST = os.path.join(SOURCES_DIR, "manifest.json")

# AMD moved much of its GPU documentation behind the Fluid Topics portal at
# docs.amd.com. The canonical amd.com URL still 301s, but to a JavaScript viewer
# page rather than the file, so a plain fetch yields HTML. The portal does expose
# the original file at /api/khub/documents/<id>/content; the ids below were
# resolved by matching filenames against the portal's own document index:
#   curl https://docs.amd.com/api/khub/documents   # 14k entries, id + filename
# Documents under instinct-tech-docs / instinct-business-docs still serve the PDF
# directly and need no fallback.
KHUB = "https://docs.amd.com/api/khub/documents/%s/content"

# llvm/llvm-project commit the AMDGPUUsage.rst snapshot is taken from. Bump this
# to re-pin; the backend source itself is not vendored here, only referenced --
# see sources/README.md for the paths that matter.
LLVM_COMMIT = "4ee9dafceb16232b5c06857551228ee5471dc5cd"

# category, subdir, local filename, canonical URL, khub id (or None)
SOURCES = [
    # --- 1. ISA reference guides, listed at
    # https://gpuopen.com/amd-gpu-architecture-programming-documentation/
    # and cross-checked against https://rocm.docs.amd.com/en/latest/reference/gpu-arch/index.html
    ("isa-pdf", "pdf", "rdna4-instruction-set-architecture.pdf",
     "https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna4-instruction-set-architecture.pdf",
     "uQpkEvk3pv~kfAb2x~j4uw"),
    ("isa-pdf", "pdf", "rdna35_instruction_set_architecture.pdf",
     "https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna35_instruction_set_architecture.pdf",
     "UVVZM22UN7tMUeiW_4ShTQ"),
    ("isa-pdf", "pdf", "rdna3-shader-instruction-set-architecture-feb-2023_0.pdf",
     "https://www.amd.com/system/files/TechDocs/rdna3-shader-instruction-set-architecture-feb-2023_0.pdf",
     "UkT_UPQL21KfKAMUBFnZTw"),
    ("isa-pdf", "pdf", "rdna2-shader-instruction-set-architecture.pdf",
     "https://www.amd.com/system/files/TechDocs/rdna2-shader-instruction-set-architecture.pdf",
     "Et~wpu9g~Ffl7d9q0QZ~Og"),
    ("isa-pdf", "pdf", "rdna-shader-instruction-set-architecture.pdf",
     "https://www.amd.com/system/files/TechDocs/rdna-shader-instruction-set-architecture.pdf",
     "mU0vhV4IgdmIWSgRqlt70g"),
    ("isa-pdf", "pdf", "amd-instinct-cdna5-instruction-set-architecture.pdf",
     "https://www.amd.com/content/dam/amd/en/documents/instinct-tech-docs/instruction-set-architectures/amd-instinct-cdna5-instruction-set-architecture.pdf",
     None),
    ("isa-pdf", "pdf", "amd-instinct-cdna4-instruction-set-architecture.pdf",
     "https://www.amd.com/content/dam/amd/en/documents/instinct-tech-docs/instruction-set-architectures/amd-instinct-cdna4-instruction-set-architecture.pdf",
     None),
    ("isa-pdf", "pdf", "amd-instinct-mi300-cdna3-instruction-set-architecture.pdf",
     "https://www.amd.com/content/dam/amd/en/documents/instinct-tech-docs/instruction-set-architectures/amd-instinct-mi300-cdna3-instruction-set-architecture.pdf",
     None),
    ("isa-pdf", "pdf", "instinct-mi200-cdna2-instruction-set-architecture.pdf",
     "https://www.amd.com/system/files/TechDocs/instinct-mi200-cdna2-instruction-set-architecture.pdf",
     None),
    # The gpuopen page's href for this one contains a non-breaking space (%C2%A0).
    ("isa-pdf", "pdf", "instinct-mi100-cdna1-shader-instruction-set-architecture.pdf",
     "https://www.amd.com/system/files/TechDocs/instinct-mi100-cdna1-shader-instruction-set-architecture%C2%A0.pdf",
     None),
    ("isa-pdf", "pdf", "vega-7nm-shader-instruction-set-architecture.pdf",
     "https://www.amd.com/system/files/TechDocs/vega-7nm-shader-instruction-set-architecture.pdf",
     "QCer_iui4EP~tr~Xlf4VGA"),
    # Vega 1. Not linked distinctly from the gpuopen page (its "Vega ISA" entry
    # points at the GCN3 file); found via the ROCm gpu-arch index.
    ("isa-pdf", "pdf", "vega-shader-instruction-set-architecture.pdf",
     "https://www.amd.com/system/files/TechDocs/vega-shader-instruction-set-architecture.pdf",
     "7wRUzLpZ~JcaXrupC32ZVA"),
    ("isa-pdf", "pdf", "gcn3-instruction-set-architecture.pdf",
     "https://www.amd.com/system/files/TechDocs/gcn3-instruction-set-architecture.pdf",
     "BOZRy1GZ1qQslfQPnq~96Q"),

    # --- other programming documentation from the same gpuopen page
    ("isa-pdf", "pdf", "micro_engine_scheduler.pdf",
     "https://gpuopen.com/download/documentation/micro_engine_scheduler.pdf",
     None),

    # --- 2. architecture white papers
    ("whitepaper", "whitepaper", "amd-cdna-4-architecture-whitepaper.pdf",
     "https://www.amd.com/content/dam/amd/en/documents/instinct-tech-docs/white-papers/amd-cdna-4-architecture-whitepaper.pdf",
     None),
    ("whitepaper", "whitepaper", "amd-cdna-3-white-paper.pdf",
     "https://www.amd.com/content/dam/amd/en/documents/instinct-tech-docs/white-papers/amd-cdna-3-white-paper.pdf",
     None),
    ("whitepaper", "whitepaper", "amd-cdna2-white-paper.pdf",
     "https://www.amd.com/content/dam/amd/en/documents/instinct-business-docs/white-papers/amd-cdna2-white-paper.pdf",
     None),
    ("whitepaper", "whitepaper", "amd-cdna-white-paper.pdf",
     "https://www.amd.com/content/dam/amd/en/documents/instinct-business-docs/white-papers/amd-cdna-white-paper.pdf",
     None),

    # --- 3. machine-readable ISA XML, https://gpuopen.com/machine-readable-isa/
    ("machine-readable-isa", "xml", "AMD_GPU_MR_ISA_XML.zip",
     "https://gpuopen.com/download/machine-readable-isa/latest/",
     None),

    # --- 4. LLVM AMDGPU backend documentation.
    # The .rst is pinned to LLVM_COMMIT below; the rendered .html is whatever
    # llvm.org was serving for trunk on the retrieval date.
    ("llvm-doc", "llvm", "AMDGPUUsage.rst",
     "https://raw.githubusercontent.com/llvm/llvm-project/" + LLVM_COMMIT
     + "/llvm/docs/AMDGPUUsage.rst",
     None),
    ("llvm-doc", "llvm", "AMDGPUUsage.html",
     "https://llvm.org/docs/AMDGPUUsage.html",
     None),
]

OK_TYPES = ("application/pdf", "application/zip", "application/octet-stream")
# The LLVM docs are text, not binaries; everything else must be a real file.
TEXT_OK = ("text/plain", "text/html")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url, dest):
    """curl -L into dest; return (code, content_type, final_url)."""
    fmt = "%{http_code}\\n%{content_type}\\n%{url_effective}"
    out = subprocess.run(
        ["curl", "-sSL", "-m", "300", "--retry", "2", "-o", dest, "-w", fmt, url],
        capture_output=True, text=True,
    )
    parts = (out.stdout or "").strip().split("\n")
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1].split(";")[0].strip(), parts[2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-download even if the local copy already matches")
    args = ap.parse_args()

    old = {}
    if os.path.exists(MANIFEST):
        old = {e["file"]: e for e in json.load(open(MANIFEST))["sources"]}

    entries, failed = [], []
    for category, subdir, name, url, khub_id in SOURCES:
        outdir = os.path.join(SOURCES_DIR, subdir)
        os.makedirs(outdir, exist_ok=True)
        dest = os.path.join(outdir, name)
        rel = os.path.join(subdir, name)

        prev = old.get(rel)
        if prev and not args.force and os.path.exists(dest) and sha256(dest) == prev.get("sha256"):
            print("  skip  %s (unchanged)" % rel)
            entries.append(prev)
            continue

        print("  get   %s" % rel)
        code, ctype, final = fetch(url, dest)
        via = "canonical"

        ok_types = TEXT_OK if category == "llvm-doc" else OK_TYPES
        if (code != "200" or ctype not in ok_types) and khub_id:
            # The canonical URL gave us the portal's viewer page; ask the portal
            # for the underlying file instead.
            print("        canonical URL served %s, retrying via docs.amd.com"
                  % (ctype or code))
            code, ctype, final = fetch(KHUB % khub_id, dest)
            via = "docs.amd.com portal"

        size = os.path.getsize(dest) if os.path.exists(dest) else 0

        if code != "200" or ctype not in ok_types:
            reason = "HTTP %s, content-type %s" % (code, ctype or "?")
            print("        FAILED: %s" % reason)
            if os.path.exists(dest):
                os.remove(dest)
            failed.append({"category": category, "file": rel, "url": url,
                           "final_url": final, "reason": reason})
            continue

        entry = {
            "category": category,
            "file": rel,
            "url": url,
            "retrieved_from": final,
            "retrieved_via": via,
            "content_type": ctype,
            "bytes": size,
            "sha256": sha256(dest),
            "retrieved": time.strftime("%Y-%m-%d"),
        }
        if khub_id:
            entry["docs_amd_com_id"] = khub_id
        if category == "llvm-doc":
            entry["llvm_commit"] = LLVM_COMMIT
        entries.append(entry)
        print("        ok %.1f MB (%s)" % (size / 1e6, via))

    os.makedirs(SOURCES_DIR, exist_ok=True)
    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump({
            "generated": time.strftime("%Y-%m-%d %H:%M:%S %z"),
            "note": "Public AMD GPU documentation. See sources/README.md for the "
                    "licence and redistribution position of each category.",
            "sources": entries,
            "failed": failed,
        }, fh, indent=2)
        fh.write("\n")

    print("\n%d retrieved, %d failed -> %s"
          % (len(entries), len(failed), os.path.relpath(MANIFEST, ROOT)))
    for f in failed:
        print("  FAILED %s (%s)" % (f["file"], f["reason"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
