"""
Onshape Pack and Go
Exports all released parts (STEP) and their linked drawings (PDF) from an assembly.
Usage: python pack_and_go.py <assembly_url>
"""

import io
import os
import re
import sys
import time
import zipfile

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

ACCESS_KEY = os.environ.get("ONSHAPE_ACCESS_KEY", "")
SECRET_KEY = os.environ.get("ONSHAPE_SECRET_KEY", "")

if not ACCESS_KEY or not SECRET_KEY:
    raise ValueError("ONSHAPE_ACCESS_KEY and ONSHAPE_SECRET_KEY must be set in .env")

BASE_URL = "https://cad.onshape.com/api/v6"
AUTH = (ACCESS_KEY, SECRET_KEY)
HEADERS = {"Accept": "application/json"}
POLL_INTERVAL = 3
POLL_TIMEOUT = 300


# ── API helpers ───────────────────────────────────────────────────────────────

api_call_count = 0


def api_get(path, query=None):
    global api_call_count
    api_call_count += 1
    resp = requests.get(BASE_URL + path, auth=AUTH, headers=HEADERS, params=query)
    resp.raise_for_status()
    return resp.json()


def api_post(path, body=None, query=None):
    global api_call_count
    api_call_count += 1
    resp = requests.post(BASE_URL + path, auth=AUTH,
                         headers={**HEADERS, "Content-Type": "application/json"},
                         params=query, json=body)
    resp.raise_for_status()
    return resp.json()


def api_get_binary(path, query=None):
    global api_call_count
    api_call_count += 1
    resp = requests.get(BASE_URL + path, auth=AUTH,
                        headers={"Accept": "application/octet-stream"},
                        params=query)
    resp.raise_for_status()
    return resp.content


def poll_all_translations(pending):
    """
    Poll a batch of in-flight translations to completion.

    pending: dict of label -> {"id": translation_id, "doc_id": doc_id}
    Returns: dict of label -> file bytes (failures are omitted; errors printed inline)
    """
    results = {}
    deadline = time.time() + POLL_TIMEOUT
    while pending and time.time() < deadline:
        still_pending = {}
        for label, job in pending.items():
            status = api_get(f"/translations/{job['id']}")
            state = status.get("requestState", "")
            if state == "DONE":
                ext_ids = status.get("resultExternalDataIds") or []
                if not ext_ids:
                    print(f"  ✗ {label} (translation done but no files returned)")
                else:
                    results[label] = api_get_binary(
                        f"/documents/d/{job['doc_id']}/externaldata/{ext_ids[0]}")
                    print(f"  ✓ {label}")
            elif state == "FAILED":
                print(f"  ✗ {label} (translation failed: {status.get('failureReason')})")
            else:
                still_pending[label] = job
        pending = still_pending
        if pending:
            time.sleep(POLL_INTERVAL)
    for label in pending:
        print(f"  ✗ {label} (timed out after {POLL_TIMEOUT}s)")
    return results


# ── Step 1: Parse URL ─────────────────────────────────────────────────────────

def parse_url(url):
    m = re.search(r"documents/([a-f0-9]+)/(w|v|m)/([a-f0-9]+)/e/([a-f0-9]+)", url)
    if not m:
        raise ValueError(f"Could not parse Onshape URL: {url}")
    return m.group(1), m.group(2), m.group(3), m.group(4)


# ── Step 2: Get released parts from BOM ──────────────────────────────────────

def get_released_parts(did, wvm, wvmid, eid):
    data = api_get(f"/assemblies/d/{did}/{wvm}/{wvmid}/e/{eid}/bom",
                   query={"bomType": "flattened", "indented": "false", "multiLevel": "false"})

    headers = {h["propertyName"]: h["id"] for h in data.get("headers", [])}
    name_col  = headers.get("name")
    state_col = headers.get("state")
    pn_col    = headers.get("partNumber")
    rev_col   = headers.get("revision")

    if not state_col:
        print("  ⚠ No 'State' column in BOM — release filtering skipped.")

    seen, parts, skipped = set(), [], []

    for row in data.get("rows", []):
        vals  = row.get("headerIdToValue", {})
        name  = vals.get(name_col) or "unnamed"
        state = vals.get(state_col) or ""

        if state_col and state.lower() != "released":
            skipped.append(f"{name} ({state or 'no state'})")
            continue

        src = row.get("itemSource", {})
        if not src:
            continue

        part_id    = src.get("partId")
        element_id = src.get("elementId")
        doc_id     = src.get("documentId") or did
        key        = (doc_id, element_id, part_id)

        if key in seen:
            continue
        seen.add(key)
        parts.append({
            "name":        name,
            "partId":      part_id,
            "elementId":   element_id,
            "documentId":  doc_id,
            "wvmId":       src.get("wvmId", wvmid),
            "wvmType":     src.get("wvmType", wvm),
            "partNumber":  vals.get(pn_col) or "",
            "revision":    vals.get(rev_col) or "",
        })

    if skipped:
        print(f"  Skipped {len(skipped)} non-released part(s):")
        for s in skipped:
            print(f"    ✗ {s}")

    return parts


# ── Step 3: Export parts as STEP ─────────────────────────────────────────────

def start_step_translation(part):
    """Start a STEP translation and return {id, doc_id} without polling."""
    result = api_post(
        f"/partstudios/d/{part['documentId']}/{part['wvmType']}/{part['wvmId']}"
        f"/e/{part['elementId']}/translations",
        body={"formatName": "STEP", "partIds": part["partId"], "storeInDocument": False},
    )
    return {"id": result["id"], "doc_id": part["documentId"]}


# ── Step 4: Find linked drawings ──────────────────────────────────────────────

def get_workspace(doc_id):
    try:
        return api_get(f"/documents/{doc_id}").get("defaultWorkspace", {}).get("id", "")
    except Exception:
        return ""


def get_drawing_metadata(did, wvm, wvmid, eid):
    """Fetch part number, revision, and state from a drawing element's metadata."""
    try:
        data = api_get(f"/metadata/d/{did}/{wvm}/{wvmid}/e/{eid}")
        props = {p["name"]: p.get("value") for p in data.get("properties", [])}
        return (
            props.get("Part number") or "",
            props.get("Revision") or "",
            props.get("State") or "",
        )
    except Exception:
        return "", "", ""


def _normalize_drawing_name(name):
    """Strip trailing drawing suffixes (e.g. 'Drawing 1', 'DWG 2') for name matching."""
    name = re.sub(r'[\s\-_]+(drawing|dwg|drw)[\s\-_]*\d*\s*$', '', name, flags=re.IGNORECASE)
    return name.strip().lower()


def find_released_drawings(parts, did):
    """
    Scan all document versions (newest first) to find released drawings matching
    the given parts. Released state only exists in the version where the release
    occurred, so a single-version scan misses drawings released at other times.

    Drawings are pre-filtered by name (stripping suffixes like 'Drawing 1') to
    avoid scanning unrelated drawings in documents with many elements.
    """
    ws_id = get_workspace(did)
    if not ws_id:
        return []

    # Get complete drawing element list from the workspace
    try:
        elements = api_get(f"/documents/d/{did}/w/{ws_id}/elements")
    except Exception:
        return []

    all_drawing_els = {
        el["id"]: el.get("name", el["id"])
        for el in elements
        if el.get("elementType") == "APPLICATION"
        and el.get("dataType") == "onshape-app/drawing"
    }
    print(f"  Found {len(all_drawing_els)} drawing(s) in document.")

    # Pre-filter by name: only scan drawings whose name (minus drawing suffix)
    # matches a part name. This avoids expensive version scans for unrelated drawings.
    part_names = {p["name"].strip().lower() for p in parts}
    drawing_els = {
        eid: name for eid, name in all_drawing_els.items()
        if _normalize_drawing_name(name) in part_names
    }

    # If any part has no name-matched candidate, fall back to scanning all drawings
    # so we don't miss drawings with non-standard naming conventions.
    matched_names = {_normalize_drawing_name(name) for name in drawing_els.values()}
    unmatched_parts = [p for p in parts if p["name"].strip().lower() not in matched_names]
    if unmatched_parts:
        print(f"  ⚠ No name-matched drawing for: {', '.join(p['name'] for p in unmatched_parts)}")
        print(f"  Falling back to scanning all {len(all_drawing_els)} drawing(s).")
        drawing_els = all_drawing_els
    else:
        skipped_count = len(all_drawing_els) - len(drawing_els)
        if skipped_count:
            print(f"  Pre-filtered to {len(drawing_els)} candidate(s) by name "
                  f"({skipped_count} drawing(s) don't match any part name).")

    # Lookup by (partNumber, revision) for exact matching
    pn_rev_lookup = {(p["partNumber"], p["revision"]): p for p in parts if p["partNumber"]}
    pn_only_lookup = {p["partNumber"] for p in parts if p["partNumber"]}

    # Workspace pre-filter: fetch metadata once per drawing from workspace.
    # Drawings whose part number doesn't appear in the BOM at all can be
    # skipped entirely — no need to scan any versions for them.
    print(f"  Pre-checking {len(drawing_els)} drawing(s) via workspace metadata...")
    version_candidates = {}  # eid -> name, for drawings worth scanning versions
    for eid, name in drawing_els.items():
        pn, _, _ = get_drawing_metadata(did, "w", ws_id, eid)
        if pn and pn not in pn_only_lookup:
            print(f"    ✗ '{name}' skipped (pn={pn!r} not in released BOM)")
        else:
            version_candidates[eid] = name
    drawing_els = version_candidates
    print(f"  {len(drawing_els)} drawing(s) need version scanning.")

    try:
        versions = api_get(f"/documents/{did}/versions")
    except Exception:
        versions = []

    found = {}     # eid -> drawing dict (pn + rev matched a BOM part)
    done = set()   # eids to stop scanning: matched or pn not in BOM at all
    wrong_rev = {} # eid -> (pn, rev) of the most recent release with wrong revision

    for ver in versions:
        vid = ver["id"]
        unresolved = [eid for eid in drawing_els if eid not in done]
        if not unresolved:
            break
        for eid in unresolved:
            pn, rev, state = get_drawing_metadata(did, "v", vid, eid)
            if state != "2":
                continue
            name = drawing_els[eid]
            if pn not in pn_only_lookup:
                # pn doesn't match any BOM part — no point scanning older versions
                done.add(eid)
                if pn:
                    print(f"    ✗ '{name}' skipped (pn={pn!r} not in released BOM)")
            elif (pn, rev) in pn_rev_lookup:
                part = pn_rev_lookup[(pn, rev)]
                print(f"    ✓ '{name}' (pn={pn}, rev={rev}) → '{part['name']}'")
                found[eid] = {"id": eid, "name": name, "documentId": did,
                              "wvm": "v", "wvmid": vid, "partNumber": pn, "revision": rev,
                              "partName": part["name"]}
                done.add(eid)
            else:
                # pn matches but revision doesn't — keep scanning older versions
                if eid not in wrong_rev:
                    wrong_rev[eid] = (pn, rev)

    for eid, name in drawing_els.items():
        if eid not in found:
            if eid in wrong_rev:
                pn, rev = wrong_rev[eid]
                print(f"    ✗ '{name}' skipped (pn={pn!r} released at rev={rev!r}, not in BOM)")
            elif eid not in done:
                print(f"    ✗ '{name}' skipped (not released in any version)")

    matched_pn_revs = {(d["partNumber"], d["revision"]) for d in found.values()}
    for p in parts:
        if p["partNumber"] and (p["partNumber"], p["revision"]) not in matched_pn_revs:
            print(f"  ⚠ No drawing found for '{p['name']}' (pn={p['partNumber']}, rev={p['revision']})")

    return list(found.values())


# ── Step 5: Export drawings as PDF ───────────────────────────────────────────

def start_pdf_translation(drawing):
    """Start a PDF translation and return {id, doc_id} without polling."""
    did = drawing["documentId"]
    result = api_post(
        f"/drawings/d/{did}/{drawing['wvm']}/{drawing['wvmid']}/e/{drawing['id']}/translations",
        body={"formatName": "PDF", "storeInDocument": False},
    )
    return {"id": result["id"], "doc_id": did}


# ── Main ──────────────────────────────────────────────────────────────────────

def get_assembly_name(did, wvm, wvmid, eid):
    """Fetch the assembly element name."""
    try:
        elements = api_get(f"/documents/d/{did}/{wvm}/{wvmid}/elements",
                           query={"elementId": eid})
        return elements[0].get("name", "onshape_assembly") if elements else "onshape_assembly"
    except Exception:
        return "onshape_assembly"


def main(assembly_url):
    print(f"\nParsing URL...")
    did, wvm, wvmid, eid = parse_url(assembly_url)
    print(f"  Document:  {did}")
    print(f"  Workspace: {wvmid} ({wvm})")
    print(f"  Element:   {eid}")

    assembly_name = get_assembly_name(did, wvm, wvmid, eid)
    safe_name = re.sub(r'[^\w\-.]', '_', assembly_name)
    exports_dir = os.path.join(os.path.dirname(__file__), "Exports")
    folder_name = os.path.join(exports_dir, safe_name)
    os.makedirs(folder_name, exist_ok=True)
    print(f"  Assembly:  {assembly_name}")
    print(f"  Output folder: Exports/{safe_name}/")

    print(f"\nFetching released parts from BOM...")
    parts = get_released_parts(did, wvm, wvmid, eid)
    print(f"  Found {len(parts)} released part(s):")
    for p in parts:
        print(f"    • {p['name']} (partId={p['partId']})")

    if not parts:
        print("No released parts found. Exiting.")
        return

    print(f"\nExporting STEP files...")
    step_base = {}  # (partNumber, revision) -> base filename without extension
    part_by_label = {}  # label -> part
    pending_steps = {}  # label -> {id, doc_id}
    for part in parts:
        part_safe = re.sub(r'[^\w\-. ]', '_', part['name'])
        label = f"{part['partNumber']}-{part['revision']}-{part_safe}.step"
        part_by_label[label] = part
        try:
            pending_steps[label] = start_step_translation(part)
            print(f"  Started: {part['name']}")
        except Exception as e:
            print(f"  ✗ {part['name']} ({e})")

    print(f"  Waiting for {len(pending_steps)} translation(s)...")
    step_results = poll_all_translations(pending_steps)

    step_files = {}
    for fname, data in step_results.items():
        part = part_by_label[fname]
        path = os.path.join(folder_name, fname)
        with open(path, "wb") as f:
            f.write(data)
        step_files[fname] = data
        step_base[(part['partNumber'], part['revision'])] = fname[:-5]

    print(f"\nFinding linked drawings...")
    docs = {}
    for p in parts:
        p_did = p["documentId"]
        if p_did not in docs:
            docs[p_did] = {"wvm": p["wvmType"], "wvmid": p["wvmId"], "parts": []}
        docs[p_did]["parts"].append(p)

    linked_drawings = []
    for doc_did, info in docs.items():
        label = "assembly document" if doc_did == did else f"external document {doc_did}"
        print(f"  Scanning {label}...")
        linked_drawings += find_released_drawings(info["parts"], doc_did)

    print(f"\nExporting PDF drawings...")
    drawing_by_label = {}  # label -> drawing
    pending_pdfs = {}      # label -> {id, doc_id}
    for drawing in linked_drawings:
        base = step_base.get((drawing['partNumber'], drawing['revision']))
        if base:
            label = base + ".pdf"
        else:
            part_safe = re.sub(r'[^\w\-. ]', '_', drawing['partName'])
            label = f"{drawing['partNumber']}-{drawing['revision']}-{part_safe}.pdf"
        drawing_by_label[label] = drawing
        try:
            pending_pdfs[label] = start_pdf_translation(drawing)
            print(f"  Started: {drawing['name']}")
        except Exception as e:
            print(f"  ✗ {drawing['name']} ({e})")

    print(f"  Waiting for {len(pending_pdfs)} translation(s)...")
    pdf_results = poll_all_translations(pending_pdfs)

    pdf_files = {}
    for fname, data in pdf_results.items():
        path = os.path.join(folder_name, fname)
        with open(path, "wb") as f:
            f.write(data)
        pdf_files[fname] = data

    print(f"\nPackaging ZIP...")
    zip_name = os.path.join(exports_dir, f"{safe_name}.zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname, data in step_files.items():
            zf.writestr(f"{safe_name}/{fname}", data)
        for fname, data in pdf_files.items():
            zf.writestr(f"{safe_name}/{fname}", data)

    with open(zip_name, "wb") as f:
        f.write(buf.getvalue())

    print(f"  ✓ Folder: Exports/{safe_name}/  ({len(step_files)} STEP, {len(pdf_files)} PDF)")
    print(f"  ✓ ZIP: Exports/{safe_name}.zip ({len(buf.getvalue()) / 1024:.1f} KB)")
    print(f"\nTotal Onshape API calls: {api_call_count}")


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        url = sys.argv[1]
    else:
        url = input("Paste assembly URL: ").strip()
    main(url)
