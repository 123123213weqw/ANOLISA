#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
xlsx_pack.py — Pack a working directory back into a valid xlsx file.

Usage:
    python3 xlsx_pack.py <source_dir> <output.xlsx>

Requirements:
    - source_dir must contain [Content_Types].xml at its root
    - All XML files are re-validated for well-formedness before packing

The resulting xlsx is a valid ZIP archive with correct OOXML structure.
"""

import sys
import os
import tempfile
import zipfile
import xml.etree.ElementTree as ET


def validate_xml_files(source_dir: str) -> list[str]:
    """Return list of XML files that fail to parse."""
    bad = []
    for dirpath, _, filenames in os.walk(source_dir):
        for fname in filenames:
            if fname.endswith(".xml") or fname.endswith(".rels"):
                fpath = os.path.join(dirpath, fname)
                try:
                    ET.parse(fpath)
                except ET.ParseError as e:
                    rel = os.path.relpath(fpath, source_dir)
                    bad.append(f"{rel}: {e}")
    return bad


def pack(source_dir: str, xlsx_path: str) -> None:
    if not os.path.isdir(source_dir):
        print(f"ERROR: Directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)

    content_types = os.path.join(source_dir, "[Content_Types].xml")
    if not os.path.isfile(content_types):
        print(
            f"ERROR: Missing [Content_Types].xml in {source_dir}\n"
            "  This file is required at the root of every valid xlsx package.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate XML well-formedness before packing
    print("Validating XML files...")
    bad_files = validate_xml_files(source_dir)
    if bad_files:
        print("ERROR: The following files have XML parse errors:", file=sys.stderr)
        for b in bad_files:
            print(f"  {b}", file=sys.stderr)
        print(
            "\nFix all XML errors before packing. "
            "A malformed xlsx cannot be opened by Excel or LibreOffice.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("✓ All XML files are well-formed")

    # Collect every member before creating any output artifact: the walk
    # snapshot below is what gets packed, so neither the destination nor a
    # temporary archive living inside the source directory can be picked
    # up and stored into the package.
    members = []
    for dirpath, _, filenames in os.walk(source_dir):
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            # Zip member names are OPC part names: always forward slashes.
            arcname = os.path.relpath(fpath, source_dir).replace(os.sep, "/")
            members.append((fpath, arcname))
    file_count = len(members)

    # Build the complete archive in a temporary file next to the real
    # destination, then publish it with a same-filesystem replace. Writing
    # members directly to the destination would truncate a previously
    # packed workbook and leave a partial archive whenever a member
    # read/write fails midway.
    dest = os.path.realpath(xlsx_path)  # write through output symlinks
    out_dir = os.path.dirname(dest) or "."
    fd, tmp_path = tempfile.mkstemp(
        dir=out_dir, prefix=os.path.basename(dest) + ".", suffix=".tmp"
    )
    os.close(fd)
    try:
        # Preserve the permissions of an existing output (realpath above
        # resolves symlinks, so this is the mode of the file we replace).
        previous_mode = None
        if os.path.exists(dest):
            previous_mode = os.stat(dest).st_mode & 0o7777
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for fpath, arcname in members:
                z.write(fpath, arcname)
        if previous_mode is not None:
            os.chmod(tmp_path, previous_mode)
        os.replace(tmp_path, dest)
    finally:
        # On success the temporary file is gone (it was renamed); on any
        # failure it is removed so no partial archive survives.
        if os.path.isfile(tmp_path):
            os.remove(tmp_path)

    size = os.path.getsize(dest)
    print(f"Packed {file_count} files → '{xlsx_path}' ({size:,} bytes)")
    print("\nNext step: run formula_check.py to validate formulas:")
    print(f"  python3 formula_check.py {xlsx_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: xlsx_pack.py <source_dir> <output.xlsx>")
        sys.exit(1)
    pack(sys.argv[1], sys.argv[2])
