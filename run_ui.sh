#!/bin/bash
# Launcher for the Sign Video Downloader Live UI
PORT=${1:-5000}
HOST="127.0.0.1"

echo "=========================================================="
echo "    Launching Sign Language Video Downloader Live UI      "
echo "=========================================================="
echo "Starting web server on http://${HOST}:${PORT}"
echo "Open this URL in your browser to add words, paste links,"
echo "and monitor downloads in real-time."
echo "=========================================================="

python3 app.py --host "${HOST}" --port "${PORT}"
