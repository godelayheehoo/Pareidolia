import os
import json
import requests
from pathlib import Path
from zipfile import ZipFile
from tqdm import tqdm
from urllib.parse import urlparse

VIDEO_JSON_PATH = "clips.json"
VIDEOS_DIR = Path("./videos")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi"}


def download_file(url: str, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        print(f"{dest.name} already exists, skipping download.")
        return

    print(f"Downloading {dest.name}...")
    r = requests.get(url, stream=True)
    r.raise_for_status()

    total = int(r.headers.get("content-length", 0))
    with open(dest, "wb") as f, tqdm(
        total=total, unit="B", unit_scale=True, desc=dest.name
    ) as pbar:
        for chunk in r.iter_content(chunk_size=1024):
            if chunk:
                f.write(chunk)
                pbar.update(len(chunk))


def process_zip(url: str):
    zip_name = Path(urlparse(url).path).name
    if not zip_name.endswith(".zip"):
        raise ValueError(f"Expected zip, got: {url}")

    zip_path = VIDEOS_DIR / zip_name
    download_file(url, zip_path)

    print(f"Unzipping {zip_name}...")
    with ZipFile(zip_path, "r") as z:
        z.extractall(VIDEOS_DIR)

    print(f"Removing {zip_name}...")
    zip_path.unlink()


def process_single_video(url: str):
    video_name = Path(urlparse(url).path).name
    ext = Path(video_name).suffix.lower()

    if ext not in VIDEO_EXTENSIONS:
        raise ValueError(f"Unsupported video type: {url}")

    dest_path = VIDEOS_DIR / video_name
    download_file(url, dest_path)


# ------------------ main ------------------

with open(VIDEO_JSON_PATH, "r", encoding="utf-8") as f:
    data = json.load(f)

missing_clips = [
    clip for clip in data["clips"]
    if not Path(clip["file_path"]).exists()
]

sources = set()
clips_missing_source = []

for clip in missing_clips:
    src = clip.get("source")
    if src:
        sources.add(src)
    else:
        clips_missing_source.append(clip["name"])

if clips_missing_source:
    print("Warning: These clips are missing a source URL:")
    for name in clips_missing_source:
        print(f"  - {name}")

VIDEOS_DIR.mkdir(exist_ok=True)

for src_url in sources:
    print(f"\nProcessing source: {src_url}")
    path = Path(urlparse(src_url).path)
    suffix = path.suffix.lower()

    if suffix == ".zip":
        process_zip(src_url)
    elif suffix in VIDEO_EXTENSIONS:
        process_single_video(src_url)
    else:
        raise ValueError(f"Unsupported source type: {src_url}")

still_missing = [
    clip["file_path"]
    for clip in data["clips"]
    if not Path(clip["file_path"]).exists()
]

if still_missing:
    print("\nThe following files are still missing:")
    for p in still_missing:
        print(f"  - {p}")
else:
    print("\nAll video files are present.")
