#!/usr/bin/env python3
"""Split LLVM/MLIR IR dump logs into one file per pass."""

import argparse
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

# LLVM new PM:    ; *** IR Dump After PassName on [module] ***
# LLVM legacy PM:   *** IR Dump After Human Name (slug) ***
# MachineIR:      # *** IR Dump After Human Name (slug) ***:
# MLIR:           // -----// IR Dump Before PassName (slug) ('builtin.module' operation) //----- //
# Newer MLIR:     // -----// IR Dump Before PassName: slug{options} //----- //
LLVM_HEADER_RE = re.compile(r"^[;#]? ?\*\*\* IR Dump (After|Before) (.+?) \*\*\*:?")
MLIR_HEADER_RE = re.compile(r"^// -+// IR Dump (After|Before) (.+?) //-+ //")
TEMPLATE_RE = re.compile(r"<.*?>")
OPTIONS_RE = re.compile(r"\{.*\}")
NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")
SLUG_RE = re.compile(r"\(([^)]+)\)")
ON_SPLIT_RE = re.compile(r" on ")


def sanitize(name: str) -> str:
    """Turn a pass name into a safe, short filename component."""
    # Newer MLIR options may contain parentheses, so drop them first.
    name = OPTIONS_RE.sub("", name)
    # Legacy/MLIR: "Human Name (slug) ..." -> use first parenthesized slug
    slug = SLUG_RE.search(name)
    if slug:
        name = slug.group(1)
    elif ": " in name:
        # Newer MLIR: "PassName: slug" -> slug
        name = name.split(": ", 1)[1]
    else:
        # New PM: "PassName on [module]" -> strip "on ..." suffix
        name = ON_SPLIT_RE.split(name, maxsplit=1)[0]
    name = TEMPLATE_RE.sub("", name)
    name = NON_ALNUM_RE.sub("_", name).strip("_")
    return name or "unknown"


def split_stream(inp, output: Path) -> int:
    """Write one split dump to the already-created staging directory."""
    index_lines = []
    cur_file = None
    # For "Before" dumps: the content under "Before pass-N" is the state
    # after pass-(N-1) completed, so we shift names by one and rewrite
    # the header from "Before pass-N" to "After pass-(N-1)".
    pending_name = None
    prev_header = None

    try:
        for line in inp:
            if "IR Dump " in line:
                m = LLVM_HEADER_RE.match(line) or MLIR_HEADER_RE.match(line)
            else:
                m = None
            if m:
                if cur_file is not None:
                    cur_file.close()

                direction = m.group(1)
                pass_info = m.group(2)
                if line.startswith("//"):
                    ext = ".mlir"
                elif line.startswith("#"):
                    ext = ".mir"
                else:
                    ext = ".ll"

                if direction == "Before":
                    if pending_name is None:
                        pending_name = "initial"
                        index_header = "Initial"
                    else:
                        index_header = prev_header.replace("Before", "After", 1)
                    short = pending_name
                    pending_name = sanitize(pass_info)
                    prev_header = line.rstrip()
                    write_header = False
                else:
                    short = sanitize(pass_info)
                    index_header = line.rstrip()
                    write_header = True

                n = len(index_lines)
                fname = f"{n:04d}_{short}{ext}"
                index_lines.append((fname, index_header))
                cur_file = (output / fname).open("w", encoding="utf-8")
                if write_header:
                    cur_file.write(line)
            elif cur_file is not None:
                cur_file.write(line)
    finally:
        if cur_file is not None:
            cur_file.close()

    with (output / "index.txt").open("w", encoding="utf-8") as f:
        for num, (fname, header) in enumerate(index_lines):
            f.write(f"{num:4d}  {fname}  {header}\n")
    return len(index_lines)


def _publish_into_empty_directory(stage: Path, output: Path) -> None:
    """Publish staged regular files without replacing caller-owned metadata."""
    if any(path != stage for path in output.iterdir()):
        raise FileExistsError(f"output directory became non-empty: {output}")

    published = []
    try:
        for source in sorted(stage.iterdir()):
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"unexpected staged output: {source}")
            destination = output / source.name
            # The staging directory is a sibling of output, so hard links stay
            # on one filesystem and publish each complete file atomically.
            os.link(source, destination)
            published.append(destination)

        expected = {path.name for path in published}
        actual = {path.name for path in output.iterdir() if path != stage}
        if actual != expected:
            raise FileExistsError(
                f"output directory changed concurrently during publication: {output}"
            )
    except (OSError, ValueError):
        for path in published:
            path.unlink(missing_ok=True)
        raise


def split_file(input_name: str, output_name: str) -> int:
    """Split input into a newly published output tree without overwriting files."""
    output = Path(output_name).absolute()
    if output.is_symlink():
        raise ValueError(f"output directory may not be a symlink: {output}")
    output_existed = output.exists()
    if output_existed:
        if not output.is_dir():
            raise ValueError(f"output path is not a directory: {output}")
        if any(output.iterdir()):
            raise ValueError(
                f"output directory is not empty; refusing to overwrite existing files: {output}"
            )

    input_path = None
    if input_name != "-":
        input_path = Path(input_name).resolve(strict=True)
        output_resolved = output.resolve(strict=False)
        try:
            input_path.relative_to(output_resolved)
        except ValueError:
            pass
        else:
            raise ValueError(
                f"input file is inside the output directory and could be overwritten: {input_path}"
            )

    output.parent.mkdir(parents=True, exist_ok=True)
    # For a caller-created output directory, stage inside that directory so a
    # mount point on a different filesystem still supports atomic hard links.
    stage_parent = output if output_existed else output.parent
    stage_root = Path(tempfile.mkdtemp(
        prefix=f".{output.name or 'ir-split'}.tmp-", dir=stage_parent
    ))
    if output_existed:
        stage = stage_root
    else:
        # mkdir applies the process umask without temporarily changing that
        # process-global setting (split_file is also safe for threaded callers).
        stage = stage_root / "output"
        stage.mkdir(mode=0o777)
    try:
        if input_path is None:
            count = split_stream(sys.stdin, stage)
        else:
            with input_path.open("r", encoding="utf-8") as inp:
                count = split_stream(inp, stage)

        if output_existed:
            # Keep the caller's directory inode, mode, ownership, and ACLs.
            _publish_into_empty_directory(stage, output)
        else:
            # POSIX rename refuses a non-empty destination directory.  The
            # earlier checks give a useful error and protect generated files.
            os.replace(stage, output)
        return count
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Split LLVM/MLIR IR dump logs into per-pass files.")
    ap.add_argument("input", help="Input IR dump log file (use - for stdin)")
    ap.add_argument("-o", "--output", required=True, help="New or empty output directory")
    args = ap.parse_args()

    try:
        count = split_file(args.input, args.output)
    except (OSError, ValueError) as exc:
        ap.error(str(exc))

    print(f"Split into {count} files in {args.output}/", file=sys.stderr)


if __name__ == "__main__":
    main()
