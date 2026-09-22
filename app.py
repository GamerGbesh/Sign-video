#!/usr/bin/env python3
"""
Flask Web Application for interactive Sign Language Video Downloader.
Provides live UI, copy-paste inputs, bulk file uploads, real-time SSE progress,
and video library preview.
"""

import argparse
import json
import logging
import os
import queue
import threading
import time
from typing import Any, Dict, List, Optional
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_from_directory,
)

from downloader import (
    CustomBatchDownloader,
    is_valid_video,
    parse_bulk_input,
    parse_url_entry,
    sanitize_filename,
)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32MB max upload
OUTPUT_DIR = os.path.abspath("videos")

# Thread-safe event bus for SSE
sse_subscribers: List[queue.Queue] = []
sse_lock = threading.Lock()

# App State
app_state = {
    "is_downloading": False,
    "current_batch": None,
    "recent_logs": [],
    "last_report": None,
    "active_downloader": None,
}


def broadcast_event(event: Dict[str, Any]):
    """Broadcast an event to all connected SSE clients."""
    with sse_lock:
        # Cache in recent logs if it's a log or progress event
        if event.get("type") in ["log", "link_result", "word_start", "word_complete", "complete"]:
            app_state["recent_logs"].append(event)
            if len(app_state["recent_logs"]) > 300:
                app_state["recent_logs"] = app_state["recent_logs"][-300:]

        dead_subscribers = []
        for q in sse_subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead_subscribers.append(q)

        for d in dead_subscribers:
            if d in sse_subscribers:
                sse_subscribers.remove(d)


def run_batch_in_background(batch_items: List[Dict[str, Any]], output_dir: str):
    """Background worker executing the batch download."""
    app_state["is_downloading"] = True
    downloader = CustomBatchDownloader(
        output_dir=output_dir,
        callback=broadcast_event,
    )
    app_state["active_downloader"] = downloader

    try:
        report = downloader.download_batch(batch_items)
        app_state["last_report"] = report
    except Exception as e:
        broadcast_event({
            "type": "log",
            "level": "error",
            "message": f"Fatal error during download: {e}",
        })
    finally:
        app_state["is_downloading"] = False
        app_state["active_downloader"] = None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/progress/stream")
def progress_stream():
    """Server-Sent Events endpoint streaming live download events."""
    def event_stream():
        q = queue.Queue(maxsize=100)
        with sse_lock:
            sse_subscribers.append(q)

        # Send initial status
        initial_data = {
            "type": "init",
            "is_downloading": app_state["is_downloading"],
            "recent_logs": app_state["recent_logs"][-50:],
        }
        yield f"data: {json.dumps(initial_data)}\n\n"

        try:
            while True:
                try:
                    event = q.get(timeout=25)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    # Keep-alive heartbeat
                    yield f": heartbeat\n\n"
        finally:
            with sse_lock:
                if q in sse_subscribers:
                    sse_subscribers.remove(q)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/status", methods=["GET"])
def get_status():
    """Get current status and recent logs."""
    return jsonify({
        "is_downloading": app_state["is_downloading"],
        "recent_logs_count": len(app_state["recent_logs"]),
        "last_report": app_state["last_report"],
    })


@app.route("/api/download", methods=["POST"])
def trigger_download():
    """
    Accepts JSON or Multipart Form data containing words and links.
    """
    if app_state["is_downloading"]:
        return jsonify({
            "status": "error",
            "message": "A download task is already in progress. Please wait or stop it.",
        }), 409

    batch_items: List[Dict[str, Any]] = []

    # 1. Check if JSON payload
    if request.is_json:
        data = request.get_json() or {}
        items = data.get("items", [])
        raw_text = data.get("text", "")

        if items:
            for it in items:
                word = it.get("word", "").strip()
                if not word:
                    continue
                entries = []
                raw_entries = it.get("entries") or it.get("urls") or []
                for entry in raw_entries:
                    if isinstance(entry, str):
                        p = parse_url_entry(entry)
                        if p and p.get("url"):
                            entries.append(p)
                    elif isinstance(entry, dict) and entry.get("url"):
                        entries.append(entry)
                if entries:
                    batch_items.append({"word": word, "entries": entries})
        elif raw_text:
            batch_items = parse_bulk_input(raw_text)

    # 2. Check if file upload or form data
    elif "file" in request.files or "text" in request.form:
        if "file" in request.files:
            file = request.files["file"]
            if file and file.filename:
                content = file.read().decode("utf-8", errors="replace")
                batch_items = parse_bulk_input(content)

        if not batch_items and "text" in request.form:
            raw_text = request.form.get("text", "")
            batch_items = parse_bulk_input(raw_text)

    if not batch_items:
        return jsonify({
            "status": "error",
            "message": "No valid words or links detected in request.",
        }), 400

    # Start background download thread
    app_state["recent_logs"] = []
    t = threading.Thread(
        target=run_batch_in_background,
        args=(batch_items, OUTPUT_DIR),
        daemon=True,
    )
    t.start()

    total_links = sum(len(item["entries"]) for item in batch_items)
    return jsonify({
        "status": "started",
        "total_words": len(batch_items),
        "total_links": total_links,
        "words": [item["word"] for item in batch_items],
    })


@app.route("/api/stop", methods=["POST"])
def stop_download():
    """Cancel currently running download."""
    if not app_state["is_downloading"] or not app_state["active_downloader"]:
        return jsonify({"status": "noop", "message": "No download is currently active."})

    app_state["active_downloader"].stop()
    broadcast_event({
        "type": "log",
        "level": "warning",
        "message": "Cancellation request received. Stopping gracefully...",
    })
    return jsonify({"status": "stopping", "message": "Cancellation requested."})


@app.route("/api/folders", methods=["GET"])
def get_folders():
    """
    List all word folders and their downloaded videos.
    """
    if not os.path.exists(OUTPUT_DIR):
        return jsonify({"folders": [], "total_videos": 0, "total_size_mb": 0})

    folders = []
    total_videos = 0
    total_bytes = 0

    entries = os.listdir(OUTPUT_DIR)
    entries.sort()

    for item in entries:
        item_path = os.path.join(OUTPUT_DIR, item)
        if os.path.isdir(item_path):
            files = []
            dir_bytes = 0
            for f in sorted(os.listdir(item_path)):
                if f.endswith(".mp4"):
                    f_path = os.path.join(item_path, f)
                    f_size = os.path.getsize(f_path)
                    dir_bytes += f_size
                    files.append({
                        "filename": f,
                        "size_kb": round(f_size / 1024, 1),
                        "url": f"/api/video/{item}/{f}",
                    })

            total_videos += len(files)
            total_bytes += dir_bytes
            folders.append({
                "name": item,
                "video_count": len(files),
                "size_mb": round(dir_bytes / (1024 * 1024), 2),
                "videos": files,
            })

    return jsonify({
        "output_dir": OUTPUT_DIR,
        "total_folders": len(folders),
        "total_videos": total_videos,
        "total_size_mb": round(total_bytes / (1024 * 1024), 2),
        "folders": folders,
    })


@app.route("/api/video/<word>/<filename>")
def serve_video(word: str, filename: str):
    """Serve downloaded video for inline preview playback."""
    word_dir = os.path.join(OUTPUT_DIR, sanitize_filename(word))
    return send_from_directory(word_dir, filename, mimetype="video/mp4")


def main():
    parser = argparse.ArgumentParser(description="Run the Sign Video Downloader Live UI")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")

    args = parser.parse_args()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("\n========================================================")
    print("      SIGN LANGUAGE VIDEO DOWNLOADER - LIVE UI          ")
    print("========================================================")
    print(f"Server URL: http://{args.host}:{args.port}")
    print(f"Videos Folder: {OUTPUT_DIR}")
    print("Press Ctrl+C to stop the server.")
    print("========================================================\n")

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
