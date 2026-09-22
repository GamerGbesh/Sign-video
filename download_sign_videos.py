#!/usr/bin/env python3
"""
CLI script to download sign language videos for curated common words from the WLASL dataset.
"""

import argparse
import logging
import os
import sys

from downloader import WLASLDownloader

DEFAULT_WORDS = [
    "hello",
    "thank you",
    "goodbye",
    "please",
    "sorry",
    "yes",
    "no",
    "help",
    "friend",
    "name",
]

DEFAULT_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "WLASL_v0.3.json")


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    format_str = "[%(levelname)s] %(message)s"
    logging.basicConfig(level=level, format=format_str, handlers=[logging.StreamHandler(sys.stdout)])


def main():
    parser = argparse.ArgumentParser(
        description="Download WLASL sign language videos organized into folders per word."
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default=DEFAULT_JSON_PATH,
        help=f"Path to WLASL JSON index file (default: {DEFAULT_JSON_PATH})",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="videos",
        help="Target root folder for downloaded videos (default: 'videos')",
    )
    parser.add_argument(
        "--words",
        type=str,
        nargs="+",
        default=None,
        help="List of words to download (default: 10 curated common words)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable detailed debug logging",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    words = args.words if args.words else DEFAULT_WORDS

    # Expand any comma-separated entries
    expanded_words = []
    for w in words:
        if "," in w:
            expanded_words.extend([item.strip() for item in w.split(",") if item.strip()])
        else:
            expanded_words.append(w.strip())

    print("==================================================")
    print("        WLASL SIGN LANGUAGE VIDEO DOWNLOADER      ")
    print("==================================================")
    print(f"Target Words ({len(expanded_words)}): {', '.join(expanded_words)}")
    print(f"Dataset Index: {args.dataset}")
    print(f"Output Folder: {os.path.abspath(args.output_dir)}")
    print("==================================================\n")

    downloader = WLASLDownloader(
        json_path=args.dataset,
        output_dir=args.output_dir,
    )

    report = downloader.download_words(expanded_words)

    print("\n==================================================")
    print("                 DOWNLOAD SUMMARY                 ")
    print("==================================================")
    for word, stats in report["words"].items():
        folder = stats["sanitized_folder"]
        success = stats["successful"]
        already = stats["already_existed"]
        failed = stats["failed"]
        total = stats["total_instances"]
        print(f"{word:<12} (videos/{folder:<12}): {success + already}/{total} available ({failed} unavailable/failed)")

    overall = report["overall"]
    print("--------------------------------------------------")
    print(
        f"Total Instances: {overall['total_instances']} | "
        f"Downloaded/Available: {overall['successful'] + overall['already_existed']} | "
        f"Unavailable: {overall['failed']}"
    )
    print(f"Full JSON report saved to: {os.path.join(args.output_dir, 'download_report.json')}")
    print("==================================================\n")


if __name__ == "__main__":
    main()
