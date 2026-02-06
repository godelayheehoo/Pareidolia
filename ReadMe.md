# Video MIDI Visualizer

A Python-based visualizer that plays video clips in response to MIDI input. Designed for live performance or generative visuals, this project allows triggering, seeking, and optionally looping video clips using MIDI notes and channels.  

> ⚠️ **Note:** This project is a work in progress. Some capabilities (like playback speed tied to tempo or looping segments) are still being explored.

## Features

- **Play video clips using GStreamer** inside a GTK window.
- **Keyboard control** for quick testing:
  - `j` → Seek to 30 seconds
  - `k` → Seek to 1 minute
- **MIDI-triggered video playback** (planned):
  - Different MIDI channels and notes trigger different video files.
  - Clips can start at specified times and optionally loop.
- **JSON-configurable clips**:
  - Each clip specifies:
    - `file_path` → path to video file
    - `midi_number` → MIDI note number to trigger it.  This can also be a midi note-- C3 is set to 60.  Must enter whole notes or sharps (with a #), no flats. 
    - `channel` → MIDI channel
    - `start_sec` → where to start playback
    - `end_sec` → where to end playback (`-1` indicates endless looping)

## Example JSON

```json
[
  {
    "file_path": "clips/clip1.mp4",
    "midi_number": 60,
    "channel": 1,
    "start_sec": 0,
    "end_sec": -1,
	"comments":"Some comment"
	"source":"some_web_path.zip"
  },
  {
    "file_path": "clips/clip2.mp4",
    "midi_number": 62,
    "channel": 1,
    "start_sec": 15,
    "end_sec": 45,
	"comments":"This clip is from a different video file",
	"source":"some_web_path.zip"
  }
]
```

## Concerning chords
Note that MIDI is an inherently serial format. As such, chords are actually transmitted as sequential notes.  This may cause odd or unexpected behavior if you have multiple notes of a chord set for clips on the same channel. 

## Getting videos
A `clip_download_helper.py` script is included to download the clips listed in `clips.json`.  Current it only supports .zip and single-video file web resources. When run, it will look for all the specified
file paths and download any that are missing.  Think about how this interacts with reorganization- it will unzip or place files directly into the ./videos directory, however they can be moved afterwards. 
If they are moved, however, they won't be found when the script runs. 

A useful place to get clips in the internet archive is at https://archive.org/details/animationandcartoons?sort=-date&and%5B%5D=year%3A%5B1900+TO+1950%5D	

## Running the program
This can be run directrly from the rpi in console GUI mode.  It behaves slightly better if run from terminal mode though, you can switch between modes using `sudo raspi-config`