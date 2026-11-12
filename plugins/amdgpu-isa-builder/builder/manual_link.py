#!/usr/bin/env python3
"""Locate persistent manual storage and read its published architecture index.

The default is $XDG_DATA_HOME/amdgpu-isa/manual. AMDGPU_ISA_MANUAL overrides it.
"""

import csv
import os

_data_home = os.environ.get("XDG_DATA_HOME", "")
MANUAL_ROOT = os.path.abspath(os.path.expanduser(
    os.environ.get("AMDGPU_ISA_MANUAL") or os.path.join(
        _data_home if os.path.isabs(_data_home) else "~/.local/share", "amdgpu-isa", "manual")))


def manual_available(path=None):
    """Return whether the manual's published table of contents is present."""
    root = MANUAL_ROOT if path is None else path
    return os.path.isfile(os.path.join(root, "toc.tsv"))


def manual_archs(path=None):
    """Return the architectures indexed by the published table of contents."""
    root = MANUAL_ROOT if path is None else path
    if not manual_available(root):
        return set()
    try:
        with open(os.path.join(root, "toc.tsv"), encoding="utf-8", newline="") as stream:
            return {row["arch"] for row in csv.DictReader(
                stream, delimiter="\t", quoting=csv.QUOTE_NONE) if row.get("arch")}
    except FileNotFoundError:
        return set()
