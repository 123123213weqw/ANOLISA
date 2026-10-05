#!/usr/bin/env python3
"""PDF text extractor (PyMuPDF)."""
import argparse, json, os, sys, tempfile

def _install():
    try:
        import pymupdf; return pymupdf
    except ImportError:
        pass
    try: import fitz; return fitz
    except ImportError:
        import subprocess; subprocess.check_call([sys.executable,"-m","pip","install","-q","PyMuPDF"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        import pymupdf; return pymupdf

def _pages(spec, total):
    ps = set()
    for p in spec.split(","):
        p = p.strip()
        if "-" in p:
            a, b = p.split("-",1); [ps.add(i) for i in range(max(0,int(a)-1), min(total,int(b)))]
        else:
            i = int(p)-1
            if 0 <= i < total: ps.add(i)
    return sorted(ps)

def _page_tables(page):
    out = []
    for t in page.find_tables().tables:
        rows = [[("" if c is None else str(c)).strip() for c in row]
                for row in t.extract()]
        out.append({"bbox": [float(v) for v in t.bbox], "rows": rows})
    return out
def _discard(path):
    try: os.unlink(path)
    except OSError: pass

def collect_attachment_records(doc):
    """List document-level embedded files as detached metadata records.

    Records use the physical zero-based index (so duplicate names stay
    unambiguous) and are copied into plain Python data while the document
    is open. Metadata-only: no payload is ever loaded here.
    """
    records = []
    for i in range(doc.embfile_count()):
        info = doc.embfile_info(i)
        records.append({
            "index": i,
            "name": info.get("name"),
            "filename": info.get("filename"),
            "ufilename": info.get("ufilename"),
            "description": info.get("description", info.get("desc")),
            "size": info.get("size"),
        })
    return records

def extract_attachment(doc, index, output_path):
    """Copy one embedded payload out by physical index into a new file.

    The exact bytes (binary, UTF-8 or empty) are written to the
    user-supplied destination, which is published atomically: an existing
    file (including the source PDF) is never replaced, the attachment's
    own name is never used as a filesystem path, content is never
    executed, the document is never modified, and no partial artifact
    survives a failed publication.
    """
    count = doc.embfile_count()
    if not isinstance(index, int) or isinstance(index, bool):
        raise ValueError(f"attachment index must be an integer, got {index!r}")
    if index < 0 or index >= count:
        raise ValueError(f"invalid attachment index {index}: document has {count} embedded files (zero-based indices 0..{count - 1})")
    dest = os.path.abspath(output_path)
    parent = os.path.dirname(dest) or "."
    if not os.path.isdir(parent):
        raise ValueError(f"output directory does not exist: {parent}")
    if os.path.exists(dest):
        raise ValueError(f"destination already exists, refusing to replace: {dest}")
    info = doc.embfile_info(index)
    payload = doc.embfile_get(index)
    try:
        fd, tmp = tempfile.mkstemp(prefix=".read_pdf_attachment_", dir=parent)
    except OSError as exc:
        raise ValueError(f"failed to publish {dest}: {exc}")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, dest)  # atomic publish; cannot replace an existing file
    except FileExistsError:
        _discard(tmp)
        raise ValueError(f"destination already exists, refusing to replace: {dest}")
    except OSError as exc:
        _discard(tmp)
        raise ValueError(f"failed to publish {dest}: {exc}")
    _discard(tmp)
    return {"index": index, "name": info.get("name"), "filename": info.get("filename"),
            "output": dest, "bytes": len(payload)}

def _emit(text, max_length):
    if max_length > 0 and len(text) > max_length:
        text = text[:max_length] + "\n...[truncated]"
    print(text)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-f","--file",required=True)
    ap.add_argument("-p","--pages",default=None)
    ap.add_argument("-d","--metadata",action="store_true")
    ap.add_argument("-t","--tables",action="store_true",
                    help="report per-page table bboxes and cell rows (JSON output only)")
    ap.add_argument("--format",default="text",choices=["text","json"])
    ap.add_argument("-m","--max-length",type=int,default=0)
    ap.add_argument("--attachments",action="store_true")
    ap.add_argument("--extract-attachment",type=int,default=None,metavar="INDEX")
    ap.add_argument("--output",default=None,metavar="PATH")
    a = ap.parse_args()
    if a.tables and a.format != "json":
        ap.error("--tables requires --format json")

    if a.attachments and a.extract_attachment is not None:
        print("ERROR: --attachments and --extract-attachment are mutually exclusive; pick one attachment mode",file=sys.stderr); sys.exit(2)
    if a.extract_attachment is None and a.output is not None:
        print("ERROR: --output is only valid together with --extract-attachment INDEX",file=sys.stderr); sys.exit(2)
    if a.extract_attachment is not None and a.output is None:
        print("ERROR: --extract-attachment requires --output PATH naming the new file",file=sys.stderr); sys.exit(2)
    if (a.attachments or a.extract_attachment is not None) and a.format != "json":
        print("ERROR: attachment modes require --format json",file=sys.stderr); sys.exit(2)

    fitz = _install()
    if not os.path.exists(a.file):
        print(f"ERROR: {a.file} not found",file=sys.stderr); sys.exit(1)
    doc = fitz.open(a.file)
    if a.attachments:
        records = collect_attachment_records(doc)
        doc.close()
        _emit(json.dumps({"total_attachments": len(records), "attachments": records}, ensure_ascii=False, indent=2), a.max_length)
        return
    if a.extract_attachment is not None:
        try:
            summary = extract_attachment(doc, a.extract_attachment, a.output)
        except (ValueError, OSError) as exc:
            doc.close()
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        doc.close()
        _emit(json.dumps(summary, ensure_ascii=False, indent=2), a.max_length)
        return
    n = len(doc)
    idx = _pages(a.pages, n) if a.pages else list(range(n))

    meta = {}
    if a.metadata and doc.metadata:
        meta = {k:v for k,v in doc.metadata.items() if v}

    pages = []
    for i in idx:
        t = doc[i].get_text("text").strip()
        if not t:
            blocks = doc[i].get_text("blocks")
            t = "\n".join(b[4] for b in sorted(blocks,key=lambda b:(b[1],b[0])) if b[-1]==0).strip()
        entry = {"page":i+1,"text":t}
        if a.tables:
            entry["tables"] = _page_tables(doc[i])
        pages.append(entry)
    doc.close()

    if a.format == "json":
        out = {"total_pages":n,"pages":pages}
        if meta: out["metadata"] = meta
        r = json.dumps(out,ensure_ascii=False,indent=2)
    else:
        parts = []
        if meta:
            parts.append("=== Metadata ===")
            parts.extend(f"  {k}: {v}" for k,v in meta.items())
            parts.append(f"  total_pages: {n}\n")
        for p in pages:
            parts.append(f"--- Page {p['page']} ---")
            parts.append(p["text"]); parts.append("")
        r = "\n".join(parts)

    if a.max_length > 0 and len(r) > a.max_length:
        r = r[:a.max_length] + "\n...[truncated]"
    print(r)

if __name__ == "__main__":
    main()
