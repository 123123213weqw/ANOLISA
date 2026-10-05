#!/usr/bin/env python3
"""PDF text extractor (PyMuPDF)."""
import argparse, json, os, sys

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
def _form_field_records(page, fitz):
    recs = []
    for w in page.widgets():
        rec = {
            "name": w.field_name,
            "label": w.field_label,
            "type": w.field_type_string,
            "type_id": w.field_type,
            "value": w.field_value,
            "flags": w.field_flags,
            "rect": [float(v) for v in list(w.rect)],
            "xref": w.xref,
        }
        if w.field_type in (fitz.PDF_WIDGET_TYPE_COMBOBOX, fitz.PDF_WIDGET_TYPE_LISTBOX):
            rec["choices"] = list(w.choice_values or [])
        if w.field_type in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON):
            bs = w.button_states() or {}
            rec["button_states"] = {"normal": list(bs.get("normal") or []),
                                    "down": list(bs.get("down") or [])}
        if w.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE:
            rec["signed"] = bool(w.is_signed)
        recs.append(rec)
    return recs

def collect_form_fields(doc, idx, fitz=None):
    """Export the given pages' AcroForm widgets as detached field records.

    All values are copied into plain Python types while the document is
    open, so records stay valid after close(). Widgets are only read:
    fields are never updated, form JavaScript is never evaluated, and no
    cryptographic signature verification is claimed (unsigned status is
    reported as stored).
    """
    if fitz is None:
        fitz = sys.modules[type(doc).__module__]
    pages = []
    for i in idx:
        page = doc[i]
        pages.append({"page": i+1, "fields": _form_field_records(page, fitz)})
    return pages

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-f","--file",required=True)
    ap.add_argument("-p","--pages",default=None)
    ap.add_argument("-d","--metadata",action="store_true")
    ap.add_argument("-t","--tables",action="store_true",
                    help="report per-page table bboxes and cell rows (JSON output only)")
    ap.add_argument("--format",default="text",choices=["text","json"])
    ap.add_argument("--form-fields",action="store_true",
                    help="export AcroForm field records (requires --format json)")
    ap.add_argument("-m","--max-length",type=int,default=0)
    a = ap.parse_args()
    if a.tables and a.format != "json":
        ap.error("--tables requires --format json")

    if a.form_fields and a.format != "json":
        print("ERROR: --form-fields requires --format json",file=sys.stderr); sys.exit(2)

    fitz = _install()
    if not os.path.exists(a.file):
        print(f"ERROR: {a.file} not found",file=sys.stderr); sys.exit(1)
    doc = fitz.open(a.file)
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
    if a.form_fields:
        pages = collect_form_fields(doc, idx)
    else:
        for i in idx:
            t = doc[i].get_text("text").strip()
            if not t:
                blocks = doc[i].get_text("blocks")
                t = "\n".join(b[4] for b in sorted(blocks,key=lambda b:(b[1],b[0])) if b[-1]==0).strip()
            pages.append({"page":i+1,"text":t})
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
