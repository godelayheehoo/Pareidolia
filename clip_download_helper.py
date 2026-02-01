import os
import json
import requests
from pathlib import Path
from zipfile import ZipFile
from tqdm import tqdm

VIDEO_JSON_PATH = "clips.json"
VIDEOS_DIR = Path("./videos")

# Load your JSON
with open(VIDEO_JSON_PATH, "r", encoding="utf-8") as f:
    data = json.load(f)

# Step 1-2: Find missing files
missing_clips = [clip for clip in data["clips"] if not Path(clip["file_path"]).exists()]

# Step 3: Collect unique sources
sources = set()
clips_missing_source = []
for clip in missing_clips:
    if "source" in clip and clip["source"]:
        sources.add(clip["source"])
    else:
        clips_missing_source.append(clip["name"])

# Step 4: Report clips missing source
if clips_missing_source:
    print("Warning: These clips are missing a source URL:")
    for name in clips_missing_source:
        print(f"  - {name}")

# Ensure videos directory exists
VIDEOS_DIR.mkdir(exist_ok=True)

# Step 5-6: Download and unzip sources
for src_url in sources:
    print(f"\nProcessing source: {src_url}")
    
    # Get filename from URL
    zip_name = src_url.split("/")[-1]
    if not zip_name.endswith(".zip"):
        raise ValueError(f"Source is not a zip file: {src_url}")
    
    zip_path = VIDEOS_DIR / zip_name

    # Download the file if it doesn't exist
    if not zip_path.exists():
        print(f"Downloading {zip_name}...")
        response = requests.get(src_url, stream=True)
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        with open(zip_path, "wb") as f, tqdm(total=total, unit='B', unit_scale=True, desc=zip_name) as pbar:
            for chunk in response.iter_content(1024):
                f.write(chunk)
                pbar.update(len(chunk))
    else:
        print(f"{zip_name} already exists, skipping download.")

    # Step 6-7: Unzip and move contents
    print(f"Unzipping {zip_name}...")
    with ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(VIDEOS_DIR)
    print("Removing zip file...")
    os.remove(zip_path)

# Step 8: Report still-missing files
still_missing = [clip["file_path"] for clip in data["clips"] if not Path(clip["file_path"]).exists()]
if still_missing:
    print("\nThe following files are still missing:")
    for path in still_missing:
        print(f"  - {path}")
else:
    print("\nAll video files are present.")
