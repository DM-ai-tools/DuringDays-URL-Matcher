"""URL Matcher Studio — FastAPI application."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.excel_io import inspect_workbook, preview_rows, row_bounds
from app.job_store import store
from app.match_runner import run_match_job
from app.cleaned_data import browse_kogan_cleaned, kogan_cleaned_summary
from app.sitemap_custom import crawl_custom_sitemap, list_sources, ingest_bulk_urls
from app.url_bulk import extract_bulk_catalogue, extract_product_urls
from app.workbook_store import has_working_copy, prepare_working, file_job_lock, working_path
from config import ROOT

UPLOADS = ROOT / "uploads"
OUTPUTS = ROOT / "outputs"
UPLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="URL Matcher Studio", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC = Path(__file__).parent / "static"
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/", response_class=HTMLResponse)
def home():
    index = STATIC / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>Frontend missing</h1>", status_code=500)
    return HTMLResponse(index.read_text(encoding="utf-8"))


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/dashboard")
def dashboard():
    jobs = [j.to_public() for j in store.list(40)]
    sources = list_sources()
    uploads = sorted(UPLOADS.glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {
        "jobs": jobs,
        "sources": sources,
        "uploads": [
            {
                "name": p.name,
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
                "id": p.stem,
            }
            for p in uploads[:30]
        ],
        "stats": {
            "total_jobs": len(jobs),
            "done_jobs": sum(1 for j in jobs if j.get("status") == "done"),
            "error_jobs": sum(1 for j in jobs if j.get("status") == "error"),
            "sources": len(sources),
            "product_urls_indexed": sum(s.get("product_urls") or 0 for s in sources),
        },
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Please upload an Excel .xlsx file")
    safe = Path(file.filename).name
    dest = UPLOADS / f"{int(__import__('time').time())}_{safe}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    info = inspect_workbook(dest)
    bounds = row_bounds(dest, info["sheet"])
    info.update(bounds)
    info["file_id"] = dest.name
    info["filename"] = safe
    info["path"] = str(dest)
    info["has_working_copy"] = False
    return info


@app.get("/api/files/{file_id}/inspect")
def inspect_file(file_id: str, sheet: str | None = None):
    path = UPLOADS / file_id
    if not path.exists():
        raise HTTPException(404, "File not found")
    from app.workbook_store import working_path

    inspect_path = working_path(file_id) if has_working_copy(file_id) else path
    info = inspect_workbook(inspect_path, sheet)
    bounds = row_bounds(inspect_path, info["sheet"])
    info.update(bounds)
    info["file_id"] = file_id
    info["has_working_copy"] = has_working_copy(file_id)
    return info


@app.delete("/api/files/{file_id}/working")
def reset_working_copy(file_id: str):
    """Discard accumulated updates and start fresh from the original upload."""
    from app.workbook_store import working_path

    upload = UPLOADS / file_id
    if not upload.exists():
        raise HTTPException(404, "File not found")
    wp = working_path(file_id)
    with file_job_lock(file_id):
        if wp.exists():
            wp.unlink()
    return {"ok": True, "file_id": file_id}


class MatchRequest(BaseModel):
    file_id: str
    start_row: int = Field(..., ge=2)
    end_row: int = Field(..., ge=2)
    site_id: str = "bigw"
    sheet: str | None = None


@app.post("/api/match")
def start_match(req: MatchRequest):
    path = UPLOADS / req.file_id
    if not path.exists():
        raise HTTPException(404, "Upload not found — please upload again")

    info = inspect_workbook(path, req.sheet)
    sheet = req.sheet or info["sheet"]
    bounds_source = working_path(req.file_id) if has_working_copy(req.file_id) else path
    bounds = row_bounds(bounds_source, sheet)

    if bounds["data_rows"] == 0:
        raise HTTPException(400, "No product rows found in this sheet")

    start = max(req.start_row, bounds["min_row"])
    end = min(req.end_row, bounds["max_row"])
    if start > end:
        raise HTTPException(
            400,
            f"Row range out of bounds. Valid Excel rows: {bounds['min_row']}–{bounds['max_row']}",
        )

    out_path = None  # set in worker — one working workbook per upload

    job = store.create(
        "match",
        meta={
            "file_id": req.file_id,
            "filename": Path(path.name).name.split("_", 1)[-1] if "_" in path.name else path.name,
            "original_upload": path.name,
            "sheet": sheet,
            "start_row": start,
            "end_row": end,
            "site_id": req.site_id,
            "clamped": start != req.start_row or end != req.end_row,
            "bounds": bounds,
        },
    )

    def worker():
        store.update(job.id, status="running", total_rows=end - start + 1)
        try:
            with file_job_lock(req.file_id):
                working, resumed = prepare_working(path, req.file_id)
                out_path_local = working

                def on_progress(payload: dict):
                    safe = {
                        k: v
                        for k, v in payload.items()
                        if k
                        in {
                            "progress",
                            "current_row",
                            "total_rows",
                            "counters",
                            "row_result",
                        }
                    }
                    if payload.get("status") == "running":
                        safe["status"] = "running"
                    store.emit(job.id, payload.get("message", ""), **safe)

                if resumed:
                    store.emit(
                        job.id,
                        "Continuing from previous updates on this file",
                        progress=1,
                        status="running",
                    )

                result = run_match_job(
                    input_path=working,
                    output_path=working,
                    sheet=sheet,
                    start_row=start,
                    end_row=end,
                    site_id=req.site_id,
                    progress=on_progress,
                )
                result["resumed_from_previous"] = resumed
                result["working_path"] = str(working)

            current = store.get(job.id)
            merged_meta = {**(current.meta if current else {}), **result}
            store.update(
                job.id,
                result_path=str(out_path_local),
                counters=result["counters"],
                message="Complete — ready to preview & download",
                meta=merged_meta,
                progress=100,
            )
            store.update(job.id, status="done")
            store.emit(
                job.id,
                "Complete — ready to preview & download",
                progress=100,
                status="done",
                counters=result["counters"],
            )
        except Exception as exc:
            store.update(job.id, status="error", error=str(exc), message=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    resumed = has_working_copy(req.file_id)
    return {
        "job_id": job.id,
        "start_row": start,
        "end_row": end,
        "bounds": bounds,
        "clamped": start != req.start_row or end != req.end_row,
        "resumed_from_previous": resumed,
        "accumulates_updates": True,
    }


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": [j.to_public() for j in store.list()]}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job.to_public()


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    """Server-Sent Events stream for live progress."""

    async def gen():
        last_len = 0
        while True:
            job = store.get(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'not found'})}\n\n"
                break
            events = job.events
            if len(events) > last_len:
                for ev in events[last_len:]:
                    payload = {
                        **ev,
                        "status": job.status,
                        "progress": job.progress,
                        "counters": job.counters,
                        "current_row": job.current_row,
                        "total_rows": job.total_rows,
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                last_len = len(events)
            # Match jobs must have result_path before we close the stream.
            ready = job.status == "error" or (
                job.status == "done" and (job.kind != "match" or bool(job.result_path))
            )
            if ready:
                yield f"data: {json.dumps({'final': True, 'status': job.status, 'progress': job.progress, 'counters': job.counters, 'result_path': job.result_path, 'error': job.error, 'job_id': job.id})}\n\n"
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str):
    job = store.get(job_id)
    if not job or not job.result_path:
        raise HTTPException(404, "Result not ready")
    path = Path(job.result_path)
    if not path.exists():
        raise HTTPException(404, "Result file missing")
    meta = job.meta or {}
    download_name = meta.get("filename") or path.name
    return FileResponse(
        path,
        filename=download_name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/jobs/{job_id}/preview")
def preview_job(job_id: str, limit: int = 40):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != "done" or not job.result_path:
        raise HTTPException(400, "Job not finished yet")
    path = Path(job.result_path)
    meta = job.meta or {}
    sheet = meta.get("sheet", "Products")
    start = int(meta.get("start_row", 2))
    end = int(meta.get("end_row", start + 20))
    return preview_rows(path, sheet, start, end, max_rows=limit)


class SitemapRequest(BaseModel):
    name: str
    index_url: str
    product_pattern: str | None = None


class BulkPreviewRequest(BaseModel):
    text: str
    name: str = "kogan"
    product_pattern: str | None = None


class BulkIngestRequest(BaseModel):
    text: str
    name: str = "kogan"
    merge: bool = True
    product_pattern: str | None = None
    index_url: str | None = None


@app.get("/api/sources")
def sources():
    return {"sources": list_sources()}


@app.get("/api/sources/kogan/cleaned-data")
def kogan_cleaned_data_summary():
    """Counts of stored cleaned Kogan sitemap data."""
    return kogan_cleaned_summary()


@app.get("/api/sources/kogan/cleaned-data/{data_type}")
def kogan_cleaned_data_browse(
    data_type: str,
    offset: int = 0,
    limit: int = 50,
    q: str = "",
):
    """Paginated browse of stored cleaned data (products, brands, categories, etc.)."""
    try:
        return browse_kogan_cleaned(data_type, offset=offset, limit=limit, q=q)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/sources/bulk/preview")
def bulk_preview(req: BulkPreviewRequest):
    """Clean a paste dump and return counts/samples without storing."""
    result = extract_product_urls(
        req.text,
        target=req.name,
        product_pattern=req.product_pattern,
    )
    return {
        "unique": result["unique"],
        "extracted": result["extracted"],
        "brand_count": result.get("brand_count", 0),
        "filtered": result.get("filtered", {}),
        "cleaned_counts": {
            "brands": result.get("brand_count", 0),
            "categories": len(result.get("categories") or []),
            "brand_categories": len(result.get("brand_categories") or []),
            "collections": len(result.get("collections") or []),
            "brand_pages": len(result.get("brand_pages") or []),
        },
        "samples": result["samples"],
        "brand_samples": result.get("brand_samples", []),
        "rejected_samples": result["rejected_samples"],
    }


@app.post("/api/sources/bulk")
def bulk_ingest(req: BulkIngestRequest):
    """Clean + store bulk product URLs into a catalogue (builtin or custom)."""
    if not req.text or not req.text.strip():
        raise HTTPException(400, "Paste product URL text (or upload a file)")
    if not req.name.strip():
        raise HTTPException(400, "Catalogue name is required (e.g. kogan)")

    job = store.create(
        "bulk_ingest",
        meta={"name": req.name, "merge": req.merge, "index_url": req.index_url},
    )

    def worker():
        store.update(job.id, status="running")
        try:

            def cb(message: str, progress: float):
                store.emit(job.id, message, progress=progress, status="running")

            result = ingest_bulk_urls(
                req.name,
                req.text,
                merge=req.merge,
                product_pattern=req.product_pattern,
                index_url=req.index_url,
                progress_cb=cb,
            )
            store.update(
                job.id,
                status="done",
                progress=100,
                message=(
                    f"Stored {result.get('product_urls', 0):,} URLs "
                    f"(+{result.get('added', 0):,} new)"
                    + (
                        f", {result.get('brands_total', 0):,} brands"
                        if result.get("brands_from_upload")
                        else ""
                    )
                    + f" as '{result.get('id')}'"
                ),
                meta={**job.meta, **result},
                counters={
                    "product_urls": result.get("product_urls", 0),
                    "added": result.get("added", 0),
                    "from_upload": result.get("unique_from_upload", 0),
                },
            )
            store.emit(
                job.id,
                store.get(job.id).message if store.get(job.id) else "Done",
                progress=100,
                status="done",
                counters={
                    "product_urls": result.get("product_urls", 0),
                    "added": result.get("added", 0),
                },
            )
        except Exception as exc:
            store.update(job.id, status="error", error=str(exc), message=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return {"job_id": job.id}


@app.post("/api/sources/bulk/upload")
async def bulk_upload(
    file: UploadFile = File(...),
    name: str = Form("kogan"),
    merge: str = Form("true"),
    product_pattern: str | None = Form(None),
    index_url: str | None = Form(None),
):
    """Upload a .txt / .xml / .csv dump of competitor product URLs."""
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="ignore")

    if not text.strip():
        raise HTTPException(400, "Uploaded file is empty")

    do_merge = str(merge).lower() not in {"0", "false", "no"}
    job = store.create(
        "bulk_ingest",
        meta={
            "name": name,
            "merge": do_merge,
            "filename": file.filename,
            "index_url": index_url,
        },
    )

    def worker():
        store.update(job.id, status="running")
        try:

            def cb(message: str, progress: float):
                store.emit(job.id, message, progress=progress, status="running")

            result = ingest_bulk_urls(
                name,
                text,
                merge=do_merge,
                product_pattern=product_pattern or None,
                index_url=index_url or None,
                progress_cb=cb,
            )
            store.update(
                job.id,
                status="done",
                progress=100,
                message=(
                    f"Stored {result.get('product_urls', 0):,} URLs "
                    f"(+{result.get('added', 0):,} new)"
                    + (
                        f", {result.get('brands_total', 0):,} brands"
                        if result.get("brands_from_upload")
                        else ""
                    )
                    + f" as '{result.get('id')}'"
                ),
                meta={**job.meta, **result},
                counters={
                    "product_urls": result.get("product_urls", 0),
                    "added": result.get("added", 0),
                    "from_upload": result.get("unique_from_upload", 0),
                },
            )
            store.emit(
                job.id,
                store.get(job.id).message if store.get(job.id) else "Done",
                progress=100,
                status="done",
                counters={
                    "product_urls": result.get("product_urls", 0),
                    "added": result.get("added", 0),
                },
            )
        except Exception as exc:
            store.update(job.id, status="error", error=str(exc), message=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return {"job_id": job.id}


@app.post("/api/sources/crawl")
def crawl_source(req: SitemapRequest):
    if not req.index_url.startswith("http"):
        raise HTTPException(400, "index_url must be an http(s) URL")
    job = store.create(
        "sitemap",
        meta={"name": req.name, "index_url": req.index_url, "product_pattern": req.product_pattern},
    )

    def worker():
        store.update(job.id, status="running")
        try:

            def cb(message: str, progress: float):
                store.emit(job.id, message, progress=progress, status="running")

            result = crawl_custom_sitemap(
                req.index_url,
                req.name,
                product_pattern=req.product_pattern,
                progress_cb=cb,
            )
            store.update(
                job.id,
                status="done",
                progress=100,
                message=f"Stored {result.get('product_urls', 0)} URLs",
                meta={**job.meta, **result},
                counters={"product_urls": result.get("product_urls", 0)},
            )
        except Exception as exc:
            store.update(job.id, status="error", error=str(exc), message=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return {"job_id": job.id}
