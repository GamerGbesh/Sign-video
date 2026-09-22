import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import urllib.parse
import requests

logger = logging.getLogger("sign_downloader")


def sanitize_filename(name: str) -> str:
    """Sanitize string for folder or file name."""
    return re.sub(r"[^\w\-_]", "_", name.strip().lower())


def extract_youtube_id(url: str) -> Optional[str]:
    """Extract YouTube video ID from URL if applicable."""
    patterns = [
        r"(?:v=|\/v\/|youtu\.be\/|\/embed\/|\/shorts\/)([a-zA-Z0-9_-]{11})",
        r"(?:embed\/|v\/|vi\/|youtu\.be\/|youtube\.com\/user\/.*?#p\/u\/[0-9]\/)([a-zA-Z0-9_-]{11})",
    ]
    for p in patterns:
        match = re.search(p, url)
        if match:
            return match.group(1)
    return None


def is_valid_video(file_path: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Validate a video file using ffprobe.
    Returns (is_valid, metadata_dict).
    """
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 1024:
        return False, None

    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration,nb_read_packets,r_frame_rate",
        "-of", "json",
        file_path,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            return False, None
        data = json.loads(res.stdout)
        streams = data.get("streams", [])
        if not streams:
            return False, None
        return True, streams[0]
    except Exception as e:
        logger.debug(f"ffprobe validation error on {file_path}: {e}")
        return False, None


def download_youtube_clip(url: str, output_path: str) -> bool:
    """
    Download a video from YouTube using yt-dlp.
    Saves to output_path.
    """
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "--merge-output-format", "mp4",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best",
        "-o", output_path,
        url,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return res.returncode == 0 and os.path.exists(output_path)
    except Exception as e:
        logger.debug(f"yt-dlp error on {url}: {e}")
        return False


def download_direct_clip(url: str, output_path: str) -> bool:
    """
    Download a video directly via HTTP/HTTPS streaming.
    Validates content-type and saves to output_path.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": url,
    }
    try:
        with requests.get(url, headers=headers, stream=True, timeout=20) as r:
            if r.status_code != 200:
                logger.debug(f"Direct download {url} returned HTTP {r.status_code}")
                return False

            content_type = r.headers.get("content-type", "").lower()
            if "text/html" in content_type:
                logger.debug(f"Direct download {url} returned HTML content")
                return False

            with open(output_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)

        return os.path.exists(output_path) and os.path.getsize(output_path) > 1024
    except Exception as e:
        logger.debug(f"Direct download error on {url}: {e}")
        return False


def trim_and_normalize_video(
    src_path: str,
    dst_path: str,
    frame_start: int,
    frame_end: int,
    fps: int = 25,
) -> bool:
    """
    Trim video if frame_end > 0, following WLASL 1-indexed specification.
    Also normalizes encoding to standard H.264 mp4.
    """
    try:
        if frame_end > 0 and frame_end >= frame_start:
            s = max(0, frame_start - 1)
            e = max(0, frame_end - 1)
            cmd = [
                "ffmpeg",
                "-y",
                "-v", "error",
                "-i", src_path,
                "-vf", f"select=between(n\\,{s}\\,{e}),setpts=PTS-STARTPTS",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-crf", "20",
                "-preset", "fast",
                "-an",
                dst_path,
            ]
        else:
            cmd = [
                "ffmpeg",
                "-y",
                "-v", "error",
                "-i", src_path,
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-crf", "20",
                "-preset", "fast",
                "-an",
                dst_path,
            ]

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if res.returncode != 0:
            logger.debug(f"ffmpeg processing failed: {res.stderr}")
            return False

        valid, _ = is_valid_video(dst_path)
        return valid
    except Exception as e:
        logger.debug(f"Video trim/normalize exception: {e}")
        return False


def trim_video_by_time(
    src_path: str,
    dst_path: str,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
) -> bool:
    """
    Trim video using timestamp strings (e.g. '00:01:23', '05.5', or seconds).
    """
    try:
        cmd = ["ffmpeg", "-y", "-v", "error"]
        if start_time:
            cmd.extend(["-ss", str(start_time)])
        if end_time:
            cmd.extend(["-to", str(end_time)])
        cmd.extend([
            "-i", src_path,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", "20",
            "-preset", "fast",
            "-an",
            dst_path,
        ])
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if res.returncode != 0:
            return False
        valid, _ = is_valid_video(dst_path)
        return valid
    except Exception as e:
        logger.debug(f"trim_video_by_time exception: {e}")
        return False


def parse_url_entry(raw_line: str) -> Dict[str, Any]:
    """
    Parse a single link entry that may have optional timestamps or frames.
    Examples:
      'https://www.youtube.com/watch?v=xyz'
      'https://www.youtube.com/watch?v=xyz 00:02-00:05'
      'https://www.youtube.com/watch?v=xyz [10-85]'
    """
    cleaned = raw_line.strip()
    if not cleaned:
        return {}

    # Check for optional trailing range e.g. 00:01-00:05 or 10-50
    range_match = re.search(r"[\s,]+(?:\[)?([0-9:.]+)\s*[-to]+\s*([0-9:.]+)(?:\])?$", cleaned, re.IGNORECASE)
    start_val = None
    end_val = None
    url = cleaned

    if range_match:
        start_val = range_match.group(1).strip()
        end_val = range_match.group(2).strip()
        url = cleaned[: range_match.start()].strip()

    # Determine if range is timestamps or integer frame numbers
    is_frame = False
    if start_val and end_val:
        if ":" not in start_val and ":" not in end_val and "." not in start_val and "." not in end_val:
            try:
                frame_s = int(start_val)
                frame_e = int(end_val)
                is_frame = True
                return {
                    "url": url,
                    "frame_start": frame_s,
                    "frame_end": frame_e,
                }
            except ValueError:
                pass

        return {
            "url": url,
            "start_time": start_val,
            "end_time": end_val,
        }

    return {"url": url}


def parse_bulk_input(content: str) -> List[Dict[str, Any]]:
    """
    Parse bulk text or file content into a list of word items:
    [
      { "word": "coffee", "entries": [ {"url": ...}, ... ] },
      ...
    ]
    Supports:
      1. JSON (dict or list)
      2. Key-Value or Block format:
         word:
           url1
           url2
      3. CSV format:
         word, url
         word, url, start, end
    """
    content = content.strip()
    if not content:
        return []

    # 1. Try JSON
    if content.startswith("{") or content.startswith("["):
        try:
            data = json.loads(content)
            result = []
            if isinstance(data, dict):
                for k, v in data.items():
                    entries = []
                    if isinstance(v, list):
                        for item in v:
                            if isinstance(item, str):
                                entries.append(parse_url_entry(item))
                            elif isinstance(item, dict):
                                entries.append(item)
                    elif isinstance(v, str):
                        entries.append(parse_url_entry(v))
                    if entries:
                        result.append({"word": k, "entries": entries})
                return result
            elif isinstance(data, list):
                for elem in data:
                    if isinstance(elem, dict) and "word" in elem:
                        w = elem["word"]
                        urls = elem.get("urls") or elem.get("instances") or []
                        entries = []
                        for u in urls:
                            if isinstance(u, str):
                                entries.append(parse_url_entry(u))
                            elif isinstance(u, dict):
                                entries.append(u)
                        if entries:
                            result.append({"word": w, "entries": entries})
                if result:
                    return result
        except json.JSONDecodeError:
            pass

    # 2. Block or Line-based parsing
    lines = content.splitlines()
    result = []
    current_word = None
    current_entries: List[Dict[str, Any]] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Check for "word:" block header
        if line.endswith(":") and "://" not in line:
            if current_word and current_entries:
                result.append({"word": current_word, "entries": current_entries})
            current_word = line[:-1].strip()
            current_entries = []
            continue

        # Check for CSV format: "word, url" or "word, url, 10, 50"
        if "," in line and "://" in line:
            parts = [p.strip() for p in line.split(",")]
            # find which part contains url
            url_idx = -1
            for idx, p in enumerate(parts):
                if "://" in p:
                    url_idx = idx
                    break
            if url_idx > 0:
                w = "_".join(parts[:url_idx]).strip()
                url = parts[url_idx]
                entry = {"url": url}
                if len(parts) >= url_idx + 3:
                    entry["start_time"] = parts[url_idx + 1]
                    entry["end_time"] = parts[url_idx + 2]
                # append to result or existing
                found = False
                for r in result:
                    if r["word"].lower() == w.lower():
                        r["entries"].append(entry)
                        found = True
                        break
                if not found:
                    result.append({"word": w, "entries": [entry]})
                continue

        # Check if line is a URL under current_word
        if "://" in line:
            entry = parse_url_entry(line)
            if current_word:
                current_entries.append(entry)
            else:
                # No current word set yet, extract from domain or default
                default_w = "custom"
                current_entries.append(entry)
                current_word = default_w
        else:
            # Maybe a standalone word header without colon
            if current_word and current_entries:
                result.append({"word": current_word, "entries": current_entries})
            current_word = line
            current_entries = []

    if current_word and current_entries:
        result.append({"word": current_word, "entries": current_entries})

    return result


class CustomBatchDownloader:
    """
    Downloader for custom user-submitted words and arbitrary video URLs.
    Supports progress event callbacks for live streaming.
    """

    def __init__(
        self,
        output_dir: str = "videos",
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.output_dir = output_dir
        self.callback = callback
        self.should_stop = False

    def emit(self, event_type: str, data: Dict[str, Any]):
        if self.callback:
            try:
                payload = {"type": event_type, "timestamp": time.time(), **data}
                self.callback(payload)
            except Exception as e:
                logger.debug(f"Callback error: {e}")

    def stop(self):
        self.should_stop = True

    def download_batch(self, batch_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        self.should_stop = False
        os.makedirs(self.output_dir, exist_ok=True)

        total_words = len(batch_items)
        total_links = sum(len(item.get("entries", [])) for item in batch_items)

        overall_stats = {
            "total_words": total_words,
            "total_links": total_links,
            "successful": 0,
            "failed": 0,
            "already_existed": 0,
            "words": {},
        }

        self.emit(
            "start",
            {
                "message": f"Starting download batch: {total_words} words, {total_links} total links.",
                "total_words": total_words,
                "total_links": total_links,
            },
        )

        processed_links = 0

        for w_idx, item in enumerate(batch_items, 1):
            if self.should_stop:
                self.emit("log", {"level": "warning", "message": "Download process stopped by user."})
                break

            raw_word = item.get("word", "unnamed").strip()
            sanitized_word = sanitize_filename(raw_word)
            word_dir = os.path.join(self.output_dir, sanitized_word)
            os.makedirs(word_dir, exist_ok=True)

            entries = item.get("entries", [])
            word_stats = {
                "word": raw_word,
                "folder": sanitized_word,
                "total": len(entries),
                "successful": 0,
                "failed": 0,
                "already_existed": 0,
                "files": [],
            }

            self.emit(
                "word_start",
                {
                    "word": raw_word,
                    "folder": sanitized_word,
                    "word_index": w_idx,
                    "total_words": total_words,
                    "word_total_links": len(entries),
                    "message": f"[{raw_word}] Processing folder 'videos/{sanitized_word}' ({len(entries)} links)",
                },
            )

            for e_idx, entry in enumerate(entries, 1):
                if self.should_stop:
                    break

                url = entry.get("url", "").strip()
                if not url:
                    continue

                processed_links += 1

                # Generate a clean identifier
                yt_id = extract_youtube_id(url)
                if yt_id:
                    file_id = f"yt_{yt_id}"
                else:
                    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
                    parsed = urllib.parse.urlparse(url)
                    base_name = os.path.basename(parsed.path)
                    clean_base = re.sub(r"[^\w\-_]", "", os.path.splitext(base_name)[0])
                    if clean_base and len(clean_base) < 25:
                        file_id = f"{clean_base}_{url_hash}"
                    else:
                        file_id = f"clip_{url_hash}"

                final_name = f"{file_id}.mp4"
                final_path = os.path.join(word_dir, final_name)

                # Check if already exists
                if os.path.exists(final_path):
                    valid, meta = is_valid_video(final_path)
                    if valid:
                        word_stats["already_existed"] += 1
                        overall_stats["already_existed"] += 1
                        word_stats["files"].append(final_name)
                        self.emit(
                            "link_result",
                            {
                                "status": "already_exists",
                                "word": raw_word,
                                "file": final_name,
                                "url": url,
                                "progress_percent": int((processed_links / max(1, total_links)) * 100),
                                "message": f"[{raw_word}] {final_name} already exists and is valid.",
                            },
                        )
                        continue
                    else:
                        os.remove(final_path)

                # Proceed to download
                self.emit(
                    "log",
                    {
                        "level": "info",
                        "message": f"[{raw_word}] Downloading ({e_idx}/{len(entries)}): {url} ...",
                    },
                )

                dl_ok = False
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_raw = os.path.join(temp_dir, f"raw_{file_id}.mp4")
                    is_yt = "youtube.com" in url or "youtu.be" in url

                    if is_yt:
                        dl_ok = download_youtube_clip(url, temp_raw)
                    else:
                        dl_ok = download_direct_clip(url, temp_raw)

                    if not dl_ok:
                        # Check if yt-dlp stored with another extension
                        for tf in os.listdir(temp_dir):
                            if tf.startswith(f"raw_{file_id}"):
                                temp_raw = os.path.join(temp_dir, tf)
                                dl_ok = is_valid_video(temp_raw)[0]
                                break

                    if not dl_ok:
                        word_stats["failed"] += 1
                        overall_stats["failed"] += 1
                        self.emit(
                            "link_result",
                            {
                                "status": "failed",
                                "word": raw_word,
                                "url": url,
                                "error": "Download failed or video link unavailable",
                                "progress_percent": int((processed_links / max(1, total_links)) * 100),
                                "message": f"[{raw_word}] FAILED: {url} (unavailable or invalid)",
                            },
                        )
                        continue

                    # Trimming & normalization
                    temp_final = os.path.join(temp_dir, f"final_{file_id}.mp4")
                    proc_ok = False

                    if "start_time" in entry or "end_time" in entry:
                        proc_ok = trim_video_by_time(
                            src_path=temp_raw,
                            dst_path=temp_final,
                            start_time=entry.get("start_time"),
                            end_time=entry.get("end_time"),
                        )
                    elif entry.get("frame_end", -1) > 0:
                        proc_ok = trim_and_normalize_video(
                            src_path=temp_raw,
                            dst_path=temp_final,
                            frame_start=entry.get("frame_start", 1),
                            frame_end=entry.get("frame_end", -1),
                            fps=entry.get("fps", 25),
                        )
                    else:
                        proc_ok = trim_and_normalize_video(
                            src_path=temp_raw,
                            dst_path=temp_final,
                            frame_start=1,
                            frame_end=-1,
                        )

                    if not proc_ok or not is_valid_video(temp_final)[0]:
                        word_stats["failed"] += 1
                        overall_stats["failed"] += 1
                        self.emit(
                            "link_result",
                            {
                                "status": "failed",
                                "word": raw_word,
                                "url": url,
                                "error": "Normalization or trimming failed",
                                "progress_percent": int((processed_links / max(1, total_links)) * 100),
                                "message": f"[{raw_word}] Processing failed for {url}",
                            },
                        )
                        continue

                    # Move final file
                    shutil.move(temp_final, final_path)
                    word_stats["successful"] += 1
                    overall_stats["successful"] += 1
                    word_stats["files"].append(final_name)

                    self.emit(
                        "link_result",
                        {
                            "status": "success",
                            "word": raw_word,
                            "file": final_name,
                            "url": url,
                            "progress_percent": int((processed_links / max(1, total_links)) * 100),
                            "message": f"[{raw_word}] SUCCESS: Saved {final_name}",
                        },
                    )

                # Polite interval
                time.sleep(0.3)

            overall_stats["words"][raw_word] = word_stats
            self.emit(
                "word_complete",
                {
                    "word": raw_word,
                    "folder": sanitized_word,
                    "stats": word_stats,
                    "message": f"[{raw_word}] Completed: {word_stats['successful'] + word_stats['already_existed']}/{word_stats['total']} available.",
                },
            )

        self.emit(
            "complete",
            {
                "overall": overall_stats,
                "message": f"Batch download finished! {overall_stats['successful'] + overall_stats['already_existed']}/{total_links} videos saved.",
            },
        )
        return overall_stats


class WLASLDownloader:
    def __init__(
        self,
        json_path: str,
        output_dir: str = "videos",
        keep_failed: bool = False,
    ):
        self.json_path = json_path
        self.output_dir = output_dir
        self.keep_failed = keep_failed
        self.dataset: List[Dict[str, Any]] = []
        self._load_dataset()

    def _load_dataset(self):
        if not os.path.exists(self.json_path):
            raise FileNotFoundError(f"Dataset JSON file not found: {self.json_path}")
        logger.info(f"Loading WLASL dataset from {self.json_path}...")
        with open(self.json_path, "r", encoding="utf-8") as f:
            self.dataset = json.load(f)
        logger.info(f"Loaded {len(self.dataset)} glosses from dataset.")

    def get_instances_for_gloss(self, gloss: str) -> List[Dict[str, Any]]:
        target = gloss.strip().lower()
        for item in self.dataset:
            if item.get("gloss", "").strip().lower() == target:
                return item.get("instances", [])
        return []

    def download_instance(
        self,
        gloss: str,
        instance: Dict[str, Any],
        target_dir: str,
    ) -> Dict[str, Any]:
        video_id = instance.get("video_id")
        url = instance.get("url")
        frame_start = instance.get("frame_start", 1)
        frame_end = instance.get("frame_end", -1)
        fps = instance.get("fps", 25)

        result = {
            "video_id": video_id,
            "url": url,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "source": instance.get("source"),
            "status": "pending",
            "file_path": None,
            "error": None,
        }

        final_video_name = f"{video_id}.mp4"
        final_video_path = os.path.join(target_dir, final_video_name)

        if os.path.exists(final_video_path):
            valid, meta = is_valid_video(final_video_path)
            if valid:
                result["status"] = "already_exists"
                result["file_path"] = final_video_path
                result["metadata"] = meta
                logger.info(f"[{gloss}] Video {video_id} already exists and is valid.")
                return result
            else:
                os.remove(final_video_path)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_raw_path = os.path.join(temp_dir, f"raw_{video_id}.mp4")

            is_youtube = "youtube.com" in url or "youtu.be" in url
            dl_success = False
            if is_youtube:
                logger.info(f"[{gloss}] Downloading YouTube video {video_id} ({url})...")
                dl_success = download_youtube_clip(url, temp_raw_path)
            else:
                logger.info(f"[{gloss}] Downloading direct video {video_id} ({url})...")
                dl_success = download_direct_clip(url, temp_raw_path)

            if not dl_success:
                found_raw = False
                for f in os.listdir(temp_dir):
                    if f.startswith(f"raw_{video_id}"):
                        temp_raw_path = os.path.join(temp_dir, f)
                        found_raw = True
                        break
                if not found_raw or not is_valid_video(temp_raw_path)[0]:
                    result["status"] = "failed"
                    result["error"] = "Download failed or file unavailable"
                    logger.warning(f"[{gloss}] Video {video_id} failed to download from {url}")
                    return result

            temp_final_path = os.path.join(temp_dir, f"final_{video_id}.mp4")
            trim_success = trim_and_normalize_video(
                src_path=temp_raw_path,
                dst_path=temp_final_path,
                frame_start=frame_start,
                frame_end=frame_end,
                fps=fps,
            )

            if not trim_success:
                result["status"] = "failed"
                result["error"] = "Trimming or video encoding failed"
                logger.warning(f"[{gloss}] Video {video_id} trimming/normalization failed")
                return result

            valid, meta = is_valid_video(temp_final_path)
            if not valid:
                result["status"] = "failed"
                result["error"] = "Final video validation failed"
                logger.warning(f"[{gloss}] Video {video_id} failed final validation")
                return result

            shutil.move(temp_final_path, final_video_path)
            result["status"] = "success"
            result["file_path"] = final_video_path
            result["metadata"] = meta
            logger.info(f"[{gloss}] Successfully saved video {video_id} -> {final_video_path}")
            return result

    def download_words(self, words: List[str]) -> Dict[str, Any]:
        os.makedirs(self.output_dir, exist_ok=True)
        summary_report: Dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_words": len(words),
            "words": {},
            "overall": {
                "total_instances": 0,
                "successful": 0,
                "failed": 0,
                "already_existed": 0,
            },
        }

        for word in words:
            sanitized_word = sanitize_filename(word)
            word_dir = os.path.join(self.output_dir, sanitized_word)
            os.makedirs(word_dir, exist_ok=True)

            instances = self.get_instances_for_gloss(word)
            logger.info(f"\n==========================================")
            logger.info(f"Processing word: '{word}' ({len(instances)} instances found in dataset)")
            logger.info(f"Target folder: {word_dir}")
            logger.info(f"==========================================")

            word_stats = {
                "gloss": word,
                "sanitized_folder": sanitized_word,
                "total_instances": len(instances),
                "successful": 0,
                "failed": 0,
                "already_existed": 0,
                "instances": [],
            }

            for inst in instances:
                res = self.download_instance(word, inst, word_dir)
                word_stats["instances"].append(res)
                if res["status"] == "success":
                    word_stats["successful"] += 1
                elif res["status"] == "already_exists":
                    word_stats["already_existed"] += 1
                else:
                    word_stats["failed"] += 1

                time.sleep(0.5)

            summary_report["words"][word] = word_stats
            summary_report["overall"]["total_instances"] += word_stats["total_instances"]
            summary_report["overall"]["successful"] += word_stats["successful"]
            summary_report["overall"]["already_existed"] += word_stats["already_existed"]
            summary_report["overall"]["failed"] += word_stats["failed"]

        report_path = os.path.join(self.output_dir, "download_report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(summary_report, f, indent=2)

        logger.info("\nDownload run completed.")
        logger.info(f"Report written to: {report_path}")
        return summary_report
