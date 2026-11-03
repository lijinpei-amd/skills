#!/usr/bin/env python3
"""Split a file into collision-safe chunks at lines matching a regex."""

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path


def _chunk_name_pattern(base: str, extension: str) -> re.Pattern[str]:
    return re.compile(rf"{re.escape(base)}_[1-9][0-9]*{re.escape(extension)}")


def split_file(
    input_file: str | os.PathLike[str],
    regex: str,
    output_dir: str | os.PathLike[str] = ".",
) -> list[Path]:
    """Split *input_file* without replacing or mixing with prior chunks."""
    source = Path(input_file)
    output = Path(output_dir)
    if not source.is_file():
        raise FileNotFoundError(f"input file not found: {source}")
    if output.is_symlink():
        raise ValueError(f"output directory must not be a symlink: {output}")

    pattern = re.compile(regex)
    base, extension = source.stem, source.suffix
    name_pattern = _chunk_name_pattern(base, extension)

    output.mkdir(parents=True, exist_ok=True)
    if not output.is_dir():
        raise NotADirectoryError(f"output path is not a directory: {output}")

    existing = sorted(
        path for path in output.iterdir() if name_pattern.fullmatch(path.name)
    )
    if existing:
        names = ", ".join(path.name for path in existing[:3])
        if len(existing) > 3:
            names += f", and {len(existing) - 3} more"
        raise FileExistsError(
            "refusing to overwrite or mix with existing chunk files: " + names
        )

    staged_paths: list[Path] = []
    published_paths: list[Path] = []
    with tempfile.TemporaryDirectory(prefix=f".{base}-chunks-", dir=output) as temp:
        staging = Path(temp)
        out_file = None

        def open_next_chunk():
            index = len(staged_paths) + 1
            staged = staging / f"{base}_{index}{extension}"
            staged_paths.append(staged)
            return staged.open("x", encoding="utf-8")

        try:
            with source.open(encoding="utf-8") as source_file:
                for line in source_file:
                    if pattern.search(line):
                        if out_file is not None:
                            out_file.close()
                        out_file = open_next_chunk()
                    elif out_file is None:
                        out_file = open_next_chunk()
                    out_file.write(line)
        finally:
            if out_file is not None:
                out_file.close()

        # Hard-link publication is atomic and fails instead of replacing a
        # file created after the preflight check.  Roll back links from this
        # run if any later publication fails.
        try:
            for staged in staged_paths:
                destination = output / staged.name
                os.link(staged, destination)
                published_paths.append(destination)
        except OSError:
            for published in published_paths:
                published.unlink(missing_ok=True)
            raise

    return published_paths


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split a file into multiple files at lines matching a regex."
    )
    parser.add_argument("input_file", help="Path to the input file")
    parser.add_argument("regex", help="Regex pattern to split on")
    parser.add_argument(
        "-o",
        "--output-dir",
        default=".",
        help="Output directory (default: current directory)",
    )
    args = parser.parse_args()

    try:
        outputs = split_file(args.input_file, args.regex, args.output_dir)
    except (OSError, ValueError, re.error) as error:
        parser.error(str(error))

    for output in outputs:
        print(output)
    if outputs:
        print(f"Split into {len(outputs)} file(s).", file=sys.stderr)
    else:
        print("No splits found; nothing written.", file=sys.stderr)


if __name__ == "__main__":
    main()
