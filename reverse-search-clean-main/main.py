"""
main.py

Video identification tool.
Extracts distinctive frames, reverse-image-searches via Bing Visual Search,
and returns the identified title + YouTube URL.

Usage:
    python main.py input.mp4
    python main.py input.mp4 --frames 12 --samples 128
    python main.py input.mp4 --json          # machine-readable output for C#
    python main.py input.mp4 --save-frames ./debug_frames
"""

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

from frame_extractor import load_clip, extract_distinctive_frames, save_frames
from reverse_search import search_all_frames, CACHE_FILE
from aggregator import aggregate_results, format_report

TEMP_DIR    = Path(".vf_frames")
RESULTS_DIR = Path(".vf_results")


def _first_run_notice():
    hf_cache = Path.home() / ".cache" / "huggingface"
    pw_cache = Path.home() / ".cache" / "ms-playwright"
    missing = []
    if not (hf_cache / "hub").exists():
        missing.append("  CLIP model       ~600 MB  -> ~/.cache/huggingface/")
    if not pw_cache.exists():
        missing.append("  Playwright Chromium ~130 MB  -> ~/.cache/ms-playwright/")
    if missing:
        print("[First-run downloads]")
        for line in missing:
            print(line)
        print("  These are one-time downloads.\n")

# deletes temporary junk files to save space
def _clear_run_artifacts():
    """Remove cache and temp files after a completed run."""
    for p in [CACHE_FILE, TEMP_DIR, RESULTS_DIR]:
        try:
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)
        except Exception:
            pass

# main sequence that ties everything together
async def run(
    video_path: str,
    n_sample: int = 64,
    n_select: int = 8,
    save_frames_dir: str | None = None,
    timeout: int = 45,
    json_output: bool = False,
) -> dict:
    """
    Full pipeline. Returns result dict:
        {
            "title":       str | null,
            "youtube_url": str | null,
            "error":       str | null   # only present on failure
        }
    """
    if not Path(video_path).exists():
        result = {"title": None, "youtube_url": None, "error": f"File not found: {video_path}"}
        if json_output:
            print(json.dumps(result))
        else:
            print(f"[Error] {result['error']}")
        sys.exit(1)

    if not json_output:
        _first_run_notice()

    # 1. Load CLIP
    model, processor = load_clip()

    # 2. Extract frames
    if not json_output:
        print(f"\n[1/3] Extracting frames...")
    distinctive = extract_distinctive_frames(
        video_path, model, processor,
        n_sample=n_sample, n_select=n_select,
    )

    frame_dir   = save_frames_dir if save_frames_dir else str(TEMP_DIR)
    frame_paths = save_frames(distinctive, frame_dir)

    # 3. Search
    if not json_output:
        print(f"\n[2/3] Searching {len(frame_paths)} frames...")
    per_frame_results = await search_all_frames(frame_paths, timeout=timeout)

    # 4. Aggregate
    if not json_output:
        print(f"\n[3/3] Aggregating...")
    agg = aggregate_results(per_frame_results)

    # Build clean output (no internal voting data)
    result = {
        "title":       agg.get("title"),
        "youtube_url": agg.get("youtube_url"),
        "error":       None,
    }

    if json_output:
        # formats output so a computer program can read it
        print(json.dumps(result))
    else:
        print(format_report(agg, frames_searched=len(frame_paths)))

    # Always clean up cache + temp frames after a run
    _clear_run_artifacts()
    if save_frames_dir is None and TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR, ignore_errors=True)

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Identify a video by reverse-image-searching its distinctive frames."
    )
    parser.add_argument("video", nargs="?", help="Path to input video file")
    parser.add_argument("--samples", type=int, default=64,
                        help="Frames to sample from video (default: 64)")
    parser.add_argument("--frames",  type=int, default=8,
                        help="Distinctive frames to search (default: 8)")
    parser.add_argument("--save-frames", type=str, default=None,
                        help="Save extracted frames to this directory for inspection")
    parser.add_argument("--timeout", type=int, default=45,
                        help="Per-frame search timeout in seconds (default: 45)")
    parser.add_argument("--json", action="store_true",
                        help="Output a single JSON line to stdout (for C# subprocess use)")
    args = parser.parse_args()

    if not args.video:
        parser.print_help()
        sys.exit(1)

    asyncio.run(run(
        video_path=args.video,
        n_sample=args.samples,
        n_select=args.frames,
        save_frames_dir=args.save_frames,
        timeout=args.timeout,
        json_output=args.json,
    ))


if __name__ == "__main__":
    main()