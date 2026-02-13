#!/usr/bin/env python3
"""
Process video clips for optimal Raspberry Pi playback.

Reads clips.json and:
1. Extracts each clip segment to a separate file in processed_clips/
2. Optimizes for Pi (lower resolution, hardware-friendly encoding)
3. Creates processed_clips.json with updated file paths and timing

Usage:
  python process_clips.py                    # Process all clips
  python process_clips.py --cleanup          # Remove orphaned processed clips
  python process_clips.py --cleanup-all      # Remove all processed clips and JSON
"""

import json
import subprocess
from pathlib import Path
import sys
import argparse
import hashlib
from pprint import pprint

# Configuration
PROCESSED_DIR = Path("processed_clips")
INPUT_JSON = "clips.json"
OUTPUT_JSON = "processed_clips.json"

# Video encoding settings optimized for Raspberry Pi 4
# Using aggressive compression for maximum performance
SCALE = "480:270"  # Quarter resolution (very lo-fi, great for multi-video)
VIDEO_BITRATE = "500k"  # Low bitrate for lo-fi aesthetic and performance
FRAMERATE = "24"  # Lower framerate reduces CPU load
PRESET = "ultrafast"  # Fast encoding, less CPU during playback

def generate_clip_filename(clip):
    """Generate a unique filename for a processed clip"""
    # Use clip name + hash of source file + timestamps for uniqueness
    name_part = clip['name'].replace(' ', '_').replace('/', '_')
    
    # Create a hash from source file and timestamps for uniqueness
    hash_input = f"{clip['file_path']}_{clip['start_sec']}_{clip.get('end_sec', -1)}"
    hash_short = hashlib.md5(hash_input.encode()).hexdigest()[:8]
    
    return f"{name_part}_{hash_short}.mp4"

def extract_clip(source_file, start_sec, end_sec, output_file):
    """
    Extract and optimize a clip using ffmpeg.
    
    Returns True if successful, False otherwise.
    """
    print(f"  Processing: {output_file.name}")
    print(f"    Source: {source_file}")
    print(f"    Time: {start_sec}s to {end_sec if end_sec >= 0 else 'end'}s")
    
    # Build ffmpeg command
    # IMPORTANT: -i BEFORE -ss for accurate seeking, then re-encode
    cmd = [
        "ffmpeg",
        "-y",  # Overwrite output file
        "-i", str(source_file),  # Input file FIRST
        "-ss", str(start_sec),  # Start time AFTER input for accurate seeking
    ]
    
    # Add duration if end_sec is specified
    if end_sec >= 0:
        duration = end_sec - start_sec
        cmd.extend(["-t", str(duration)])
    
    cmd.extend([
        "-vf", f"scale={SCALE},fps={FRAMERATE}",  # Scale down and limit framerate
        "-c:v", "libx264",  # H.264 codec (Pi hardware can decode this)
        "-preset", PRESET,  # Fast encoding
        "-b:v", VIDEO_BITRATE,  # Video bitrate
        "-c:a", "aac",  # Audio codec
        "-b:a", "96k",  # Lower audio bitrate
        "-avoid_negative_ts", "make_zero",  # Fix timestamp issues
        "-reset_timestamps", "1",  # Reset timestamps to start at 0
        "-fflags", "+genpts",  # Generate presentation timestamps
        str(output_file)
    ])
    
    try:
        # Run ffmpeg
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        print(f"    ✓ Success")
        return True
    except subprocess.CalledProcessError as e:
        print(f"    ✗ Failed: {e}")
        print(f"    Error output: {e.stderr[-500:]}")  # Last 500 chars
        return False
    except FileNotFoundError:
        print(f"    ✗ Error: ffmpeg not found. Please install ffmpeg.")
        return False

def process_clips():
    """Main processing function"""
    print("=" * 60)
    print("Video Clip Processor for Raspberry Pi")
    print("=" * 60)
    
    # Load clips.json
    print(f"\nLoading {INPUT_JSON}...")
    try:
        with open(INPUT_JSON, 'r') as f:
            clips_data = json.load(f)
    except FileNotFoundError:
        print(f"Error: {INPUT_JSON} not found!")
        return False
    
    clips = clips_data.get('clips', [])
    print(f"Found {len(clips)} clips to process")
    
    # Create output directory
    PROCESSED_DIR.mkdir(exist_ok=True)
    print(f"Output directory: {PROCESSED_DIR}/")
    
    # Process each clip
    processed_clips = []
    success_count = 0
    fail_count = 0
    
    for i, clip in enumerate(clips, 1):
        print(f"\n[{i}/{len(clips)}] Processing '{clip['name']}'")
        
        # Get source file path
        source_path = Path(clip['file_path'])
        if not source_path.exists():
            print(f"  ✗ Source file not found: {source_path}")
            fail_count += 1
            continue
        
        # Generate output filename
        output_filename = generate_clip_filename(clip)
        output_path = PROCESSED_DIR / output_filename

        if output_path.exists() and not args.force:
            print(f"  ✓ Skipping (already exists): {output_filename}")
            success_count += 1
            continue
        
        # Extract clip
        start_sec = clip['start_sec']
        end_sec = clip.get('end_sec', -1)
        
        if extract_clip(source_path, start_sec, end_sec, output_path):
            # Create processed clip entry
            processed_clip = clip.copy()
            processed_clip['file_path'] = str(output_path)
            processed_clip['start_sec'] = 0  # Processed clips start at 0
            
            # Update end_sec for processed clip
            if end_sec >= 0:
                processed_clip['end_sec'] = end_sec - start_sec
            else:
                # For clips that go to end, we need to get the duration
                # For now, keep it as -1 (play to end)
                processed_clip['end_sec'] = -1
            
            processed_clip['original_file'] = str(source_path)
            processed_clip['original_start'] = start_sec
            processed_clip['original_end'] = end_sec
            
            processed_clips.append(processed_clip)
            success_count += 1
        else:
            fail_count += 1
    
    # Save processed_clips.json
    print(f"\n{'=' * 60}")
    print(f"Processing complete!")
    print(f"  Success: {success_count}")
    print(f"  Failed: {fail_count}")
    
    if processed_clips:
        output_data = {'clips': processed_clips}
        with open(OUTPUT_JSON, 'w') as f:
            json.dump(output_data, f, indent=2)
        print(f"\nSaved {OUTPUT_JSON} with {len(processed_clips)} clips")
        print(f"\nTo use processed clips, update your player script to load:")
        print(f"  {OUTPUT_JSON} instead of {INPUT_JSON}")
    
    return success_count > 0

def cleanup_orphaned():
    """Remove processed clips that aren't in processed_clips.json"""
    print("=" * 60)
    print("Cleaning up orphaned processed clips")
    print("=" * 60)
    
    if not PROCESSED_DIR.exists():
        print(f"\nNo {PROCESSED_DIR}/ directory found. Nothing to clean.")
        return
    
    # Load processed_clips.json to see which files are referenced
    referenced_files = set()
    if Path(OUTPUT_JSON).exists():
        print(f"\nLoading {OUTPUT_JSON}...")
        with open(OUTPUT_JSON, 'r') as f:
            data = json.load(f)
        
        for clip in data.get('clips', []):
            file_path = Path(clip['file_path'])
            if file_path.parent == PROCESSED_DIR:
                referenced_files.add(file_path.name)
        
        print(f"Found {len(referenced_files)} referenced clips")
    else:
        print(f"\nNo {OUTPUT_JSON} found. All processed clips will be considered orphaned.")
    
    # Find all files in processed_clips/
    all_files = list(PROCESSED_DIR.glob("*.mp4"))
    print(f"Found {len(all_files)} total files in {PROCESSED_DIR}/")
    
    # Remove orphaned files
    removed_count = 0
    for file_path in all_files:
        if file_path.name not in referenced_files:
            print(f"  Removing orphaned: {file_path.name}")
            file_path.unlink()
            removed_count += 1
    
    print(f"\n{'=' * 60}")
    print(f"Cleanup complete!")
    print(f"  Removed: {removed_count} orphaned files")
    print(f"  Kept: {len(referenced_files)} referenced files")

def cleanup_all():
    """Remove all processed clips and the JSON file"""
    print("=" * 60)
    print("Removing ALL processed clips")
    print("=" * 60)
    
    removed_count = 0
    
    # Remove all files in processed_clips/
    if PROCESSED_DIR.exists():
        all_files = list(PROCESSED_DIR.glob("*.mp4"))
        print(f"\nRemoving {len(all_files)} processed clip files...")
        for file_path in all_files:
            print(f"  Removing: {file_path.name}")
            file_path.unlink()
            removed_count += 1
        
        # Remove directory if empty
        if not any(PROCESSED_DIR.iterdir()):
            PROCESSED_DIR.rmdir()
            print(f"Removed empty directory: {PROCESSED_DIR}/")
    
    # Remove processed_clips.json
    if Path(OUTPUT_JSON).exists():
        print(f"\nRemoving {OUTPUT_JSON}")
        Path(OUTPUT_JSON).unlink()
    
    print(f"\n{'=' * 60}")
    print(f"Cleanup complete!")
    print(f"  Removed: {removed_count} files")

def main():
    parser = argparse.ArgumentParser(
        description="Process video clips for optimal Raspberry Pi playback"
    )
    parser.add_argument(
        '--cleanup',
        action='store_true',
        help='Remove orphaned processed clips (not in processed_clips.json)'
    )
    parser.add_argument(
        '--cleanup-all',
        action='store_true',
        help='Remove ALL processed clips and processed_clips.json'
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Force reprocessing of all clips'
    )
    
    args = parser.parse_args()
    
    if args.cleanup_all:
        cleanup_all()
    elif args.cleanup:
        cleanup_orphaned()
    else:
        # Normal processing
        success = process_clips()
        sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()