"""Record the live /watch-data feed into docs/demo.json for the GitHub Pages replay.

Run the server first, then:  python tools/record_demo.py --minutes 30
"""
import argparse
import json
import os
import time

import requests

DEMO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "demo.json")
CURRENT_KEYS = ("visible", "line1", "line2", "line3", "line4", "raw", "enrichment")


def slim(frame):
    return {
        "generated_at": frame["generated_at"],
        "current": {key: frame["current"].get(key) for key in CURRENT_KEYS},
        "planes": frame["planes"],
        "next_up": frame["next_up"],
        "window": frame["window"],
    }


def write_demo(frames, path=DEMO_PATH):
    recorded_on = time.strftime("%B %d, %Y", time.localtime(frames[0]["generated_at"]))
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"recorded_on": recorded_on, "frames": [slim(fr) for fr in frames]}, f, separators=(",", ":"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5001/watch-data")
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--every", type=float, default=12, help="seconds between samples")
    args = parser.parse_args()

    frames = []
    end = time.time() + args.minutes * 60
    while time.time() < end:
        try:
            frames.append(requests.get(args.url, timeout=60).json())
            write_demo(frames)
            current = frames[-1]["current"]
            print(time.strftime("%H:%M:%S"), len(frames), current["line1"], flush=True)
        except Exception as e:
            print("error:", e, flush=True)
        time.sleep(args.every)


if __name__ == "__main__":
    main()
