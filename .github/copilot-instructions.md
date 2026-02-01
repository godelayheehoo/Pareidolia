Purpose
This repo contains a small Python/GStreamer+GTK example (a local video player) and supporting artifacts (a Jupyter notebook and a large local video file). These instructions help an AI coding agent be productive here: how the project is structured, how to run and debug the player, and the repo-specific conventions to follow.

# MIDI‑Driven Video Clip Player (Project Intent)

## High‑level goal

This project is an experimental, performance‑oriented video playback tool driven by MIDI input. The intent is to trigger specific video files and/or clip regions (start points within a file) in response to MIDI events while music is playing. Video is visual‑only; audio from the clips is ignored.

This is a **work in progress**. Exact capabilities will evolve as the project develops, but the core idea is stable: **MIDI events select and control video clips**.

---

## Current scope (what it does / will do first)

* Use **GStreamer + GTK** for video playback
* React to **MIDI input** (USB MIDI for now)
* Map MIDI channels and/or MIDI notes to:

  * a video file
  * a clip start time
  * an optional clip end time
* Seek immediately when triggered
* Loop clips automatically when configured

Out of scope (for now):

* Audio playback from video
* Crossfades or blending
* Tempo‑locked playback speed
* Multiple simultaneous videos

---

## Mental model

Think of this as a **cue list**, not a single video timeline.

Each cue ("clip") is:

* self‑contained
* independently triggerable
* mapped explicitly to MIDI

The system listens for MIDI events and activates the corresponding clip.

---

## JSON configuration format

Configuration is defined in a single JSON file containing a list of clips. Each clip is fully self‑describing.

### Top‑level structure

```json
{
  "clips": [ ... ]
}
```

### Clip fields

Each clip object contains:

| Field          | Type               | Description                                        |
| -------------- | ------------------ | -------------------------------------------------- |
| `name`         | string             | Human‑readable identifier (for debugging/logging)  |
| `file_path`    | string             | Path to the video file                             |
| `start_sec`    | number             | Start time in seconds                              |
| `end_sec`      | number             | End time in seconds, or `-1` for infinite playback |
| `midi_channel` | number             | MIDI channel (1–16)                                |
| `midi_note`    | number \| number[]  | MIDI note number or list of numbers that triggers this clip           |
| `exclusive`    | boolean (optional) | If true, stop any currently playing clip           |

### Example

```json
{
  "clips": [
    {
      "name": "intro_loop",
      "file_path": "./videos/paddy.mp4",
      "start_sec": 30.0,
      "end_sec": -1,
      "midi_channel": 1,
      "midi_note": 36,
      "exclusive": true
    },
    {
      "name": "chase_section",
      "file_path": "./videos/paddy.mp4",
      "start_sec": 60.0,
      "end_sec": 95.0,
      "midi_channel": 1,
      "midi_note": [35, 38]
    }
  ]
}
```

### Semantics

* `end_sec = -1` means **play indefinitely** (no looping logic required)
* If `end_sec >= 0`, the clip loops back to `start_sec` when playback reaches `end_sec`
* MIDI channels are **1‑indexed** (standard MIDI convention)
* If midi_channel = -1, the clip responds to any channel,
* If midi_note = -1, the clip responds to any note on the specified channel

---

## Runtime behavior (conceptual)

1. Load JSON configuration
2. Listen for MIDI input
3. On matching `(channel, note)`:

   * Seek the pipeline to `start_sec`
   * Begin playback
4. If `end_sec >= 0`:

   * Monitor playback position
   * Loop when the end is reached

---

## Future considerations (explicitly not locked in yet)

* Mapping ranges or CCs instead of notes
* Tempo‑linked playback speed
* Clip retrigger behavior (ignore / restart / quantize)
* Multiple pipelines or preloading

This document is meant to describe **intent and structure**, not final behavior. Changes are expected as the project evolves.

## GSTREAMER CODING RULES - NEVER VIOLATE:

1. NEVER define the same function twice in the same file. Always check if a function already exists before adding it.

2. When changing a playbin's URI property:
   - MUST set pipeline to NULL or READY state first: pipeline.set_state(Gst.State.READY)
   - Then change URI: pipeline.set_property('uri', new_uri)
   - Then set to PAUSED: pipeline.set_state(Gst.State.PAUSED)
   - Wait for state change: pipeline.get_state(Gst.CLOCK_TIME_NONE)
   - Only then seek or play

3. When seeking in GStreamer:
   - Pipeline should be in PAUSED or PLAYING state (preferably PAUSED)
   - Use seek_simple() BEFORE setting to PLAYING state when starting new clips
   - Never seek while simultaneously changing URI without proper state transitions

4. The error "gst_video_center_rect: assertion 'src->h != 0' failed" means the video sink lost dimension information. Common causes:
   - **MOST COMMON: Invalid or incorrect file paths** - Always verify paths exist and URIs are correctly formatted
   - Changing URI without proper state transitions
   - Seeking before pipeline is ready
   - Race conditions from setting PLAYING before seeking
   - File doesn't exist or can't be opened
   - File permission issues

5. Always check for duplicate function definitions before adding code to existing files.

6. When debugging GStreamer issues:
   - First verify all file paths are correct and files exist
   - Add logging to show URIs being used
   - Check pipeline state transitions
   - Use get_state() to wait for state changes to complete
   - Set GST_DEBUG environment variable for detailed logs if needed