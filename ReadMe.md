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
    - `midi_number` → MIDI note number to trigger it
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
    "end_sec": -1
  },
  {
    "file_path": "clips/clip2.mp4",
    "midi_number": 62,
    "channel": 1,
    "start_sec": 15,
    "end_sec": 45
  }
]
