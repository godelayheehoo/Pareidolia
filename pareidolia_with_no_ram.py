#!/usr/bin/env python3
"""
Multi-video MIDI-triggered player for Raspberry Pi 4
Uses pre-processed, optimized clip files for better performance
With playback position tracking and restart_on_play support
"""

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
from pathlib import Path
import json
from pprint import pprint
import sys
import signal
import mido
import queue
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional
from tools import note_to_midi
import threading 

# -------------------------------
# Configuration
# -------------------------------
MAX_SIMULTANEOUS_VIDEOS = 4

# -------------------------------
# Playback Position Tracking
# -------------------------------
clip_playback_data = {}  # key: file_path (URI), value: {'position': seconds, 'start_time': unix_time}

# -------------------------------
# Load Clips Configuration
# -------------------------------
print("Loading processed clips configuration...")
with open("processed_clips.json", "r") as f:
    clips_config = json.load(f)['clips']

for c in clips_config:
    # Convert to URIs
    if not Path(c['file_path']).exists():
        raise FileNotFoundError(f"Clip file does not exist: {c['file_path']}")
    
    c['file_path'] = str(Path(c['file_path']).resolve().as_uri())
    
    # Convert MIDI notes - now supports comma-separated chords
    midi_notes = []
    if isinstance(c['midi_note'], str) and c['midi_note']:
        # Parse comma-separated notes
        note_strings = [n.strip() for n in c['midi_note'].split(',') if n.strip()]
        for note_str in note_strings:
            try:
                # Try to parse as note name first (e.g., "C4")
                if note_str[0].isalpha():
                    midi_notes.append(note_to_midi(note_str))
                else:
                    # Already a MIDI number
                    midi_notes.append(int(note_str))
            except (ValueError, IndexError, KeyError):
                print(f"Warning: Could not parse note '{note_str}' in clip '{c['name']}'")
    elif isinstance(c['midi_note'], int) and c['midi_note'] >= 0:
        # Single MIDI number (old format)
        midi_notes.append(c['midi_note'])
    
    # Store as sorted set (order doesn't matter for chord matching)
    c['required_notes'] = frozenset(midi_notes) if midi_notes else frozenset()
    
    if c['midi_channel'] > 0:
        c['midi_channel'] = c['midi_channel'] - 1  # Convert to 0-based
    
    # Set default restart_on_play if not present
    if 'restart_on_play' not in c:
        c['restart_on_play'] = False
    
    pprint(c)
    print("\n")

# Build list of all clips for chord matching
# We no longer use a simple lookup dict since we need to check all clips
# against the currently active notes
all_clips = clips_config

# Track currently active notes per MIDI channel
# Structure: { channel: set(note1, note2, ...) }
active_notes_by_channel = {}

print("\nClip chord requirements:")
for clip in all_clips:
    channel = clip.get('midi_channel', -1)
    notes = clip.get('required_notes', frozenset())
    ch_str = f"ch={channel}" if channel >= 0 else "any channel"
    notes_str = f"{{{', '.join(map(str, sorted(notes)))}}}" if notes else "{empty}"
    print(f"  '{clip['name']}': {ch_str}, notes={notes_str}")

# -------------------------------
# Initialize GStreamer
# -------------------------------
print("\nInitializing GStreamer...")
Gst.init(None)

import os
os.environ['GST_DEBUG'] = '2'

# -------------------------------
# Active Video Tracking
# -------------------------------
@dataclass
class ActiveVideo:
    """Represents an actively playing video"""
    clip: dict
    pipeline: Gst.Element
    compositor_pad: Gst.Pad
    loop_timer_id: Optional[int] = None
    midi_key: tuple = field(default_factory=tuple)
    start_time: float = field(default_factory=time.time)

active_videos: OrderedDict[tuple, ActiveVideo] = OrderedDict()
loop = None

# -------------------------------
# Main Compositor Pipeline
# -------------------------------
def is_desktop_environment():
    """Check if we're running in a desktop/X11 environment"""
    if os.environ.get('DISPLAY'):
        return True
    if os.environ.get('WAYLAND_DISPLAY'):
        return True
    session_type = os.environ.get('XDG_SESSION_TYPE', '')
    if session_type in ('x11', 'wayland'):
        return True
    return False

main_pipeline = Gst.Pipeline.new("main_pipeline")
compositor = Gst.ElementFactory.make("compositor", "compositor")
compositor.set_property("background", 1)

if is_desktop_environment():
    print("Desktop environment detected - using autovideosink")
    videosink = Gst.ElementFactory.make("autovideosink", "videosink")
    print("Note: Press F11 for fullscreen in desktop mode")
else:
    print("Console mode detected - using kmssink for fullscreen")
    videosink = Gst.ElementFactory.make("kmssink", "videosink")
    if videosink:
        print("kmssink loaded successfully")
        try:
            videosink.set_property("fullscreen", True)
        except:
            pass
    else:
        print("WARNING: kmssink not available, falling back to autovideosink")
        videosink = Gst.ElementFactory.make("autovideosink", "videosink")

main_pipeline.add(compositor)
main_pipeline.add(videosink)
compositor.link(videosink)

bus = main_pipeline.get_bus()
bus.add_signal_watch()

def calculate_layout(num_videos):
    """Calculate positions and sizes for videos based on count"""
    width = 1920
    height = 1080
    
    if num_videos == 1:
        return [(0, 0, width, height)]
    elif num_videos == 2:
        half_width = width // 2
        return [
            (0, 0, half_width, height),
            (half_width, 0, half_width, height)
        ]
    elif num_videos == 3:
        half_width = width // 2
        half_height = height // 2
        return [
            (0, 0, half_width, height),
            (half_width, 0, half_width, half_height),
            (half_width, half_height, half_width, half_height)
        ]
    elif num_videos == 4:
        half_width = width // 2
        half_height = height // 2
        return [
            (0, 0, half_width, half_height),
            (half_width, 0, half_width, half_height),
            (0, half_height, half_width, half_height),
            (half_width, half_height, half_width, half_height)
        ]
    return []

def update_layout():
    """Update the position and size of all active video pads in the compositor"""
    num_videos = len(active_videos)
    if num_videos == 0:
        return
    
    layout = calculate_layout(num_videos)
    
    for idx, (key, video) in enumerate(active_videos.items()):
        if idx >= len(layout):
            break
        
        x, y, w, h = layout[idx]
        pad = video.compositor_pad
        
        pad.set_property("xpos", x)
        pad.set_property("ypos", y)
        pad.set_property("width", w)
        pad.set_property("height", h)
        
        print(f"  Video {idx} '{video.clip['name']}' -> pos({x},{y}) size({w}x{h})")

def start_clip_playback(clip, start_position=0, midi_key=None):
    """
    Create playbin for a pre-processed clip.
    Optionally seeks to start_position if > 0.
    Sets up automatic looping when clip reaches end.
    """
    playbin = Gst.ElementFactory.make("playbin", f"player_{id(clip)}")
    playbin.set_property("uri", clip['file_path'])
    
    # Store midi_key on the playbin for EOS handling
    playbin.midi_key = midi_key
    playbin.clip = clip
    
    # Create video output bin
    videobin = Gst.Bin.new(f"videobin_{id(clip)}")
    videoscale = Gst.ElementFactory.make("videoscale", "videoscale")
    videoconvert = Gst.ElementFactory.make("videoconvert", "videoconvert")
    sink = Gst.ElementFactory.make("intervideosink", "intervideosink")
    sink.set_property("channel", f"channel_{id(clip)}")
    
    videobin.add(videoscale)
    videobin.add(videoconvert)
    videobin.add(sink)
    
    videoscale.link(videoconvert)
    videoconvert.link(sink)
    
    # Add ghost pad
    pad = videoscale.get_static_pad("sink")
    ghost_pad = Gst.GhostPad.new("sink", pad)
    videobin.add_pad(ghost_pad)
    
    playbin.set_property("video-sink", videobin)
    
    # Set up bus message watching for EOS
    bus = playbin.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_playbin_message)
    
    # Set to PAUSED first for seeking
    playbin.set_state(Gst.State.PAUSED)
    
    # Wait for preroll if we need to seek
    if start_position > 0:
        ret = playbin.get_state(5 * Gst.SECOND)
        if ret[0] == Gst.StateChangeReturn.SUCCESS or ret[0] == Gst.StateChangeReturn.ASYNC:
            # Seek to the desired position
            seek_ns = int(start_position * Gst.SECOND)
            playbin.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                seek_ns
            )
            print(f"  Seeking to {start_position:.1f}s")
    
    # Now set to PLAYING
    playbin.set_state(Gst.State.PLAYING)
    
    return playbin

def on_playbin_message(bus, message):
    """Handle messages from individual playbins (for looping)"""
    t = message.type
    
    if t == Gst.MessageType.EOS:
        # Clip reached the end - loop it!
        playbin = message.src
        if hasattr(playbin, 'midi_key') and hasattr(playbin, 'clip'):
            midi_key = playbin.midi_key
            clip = playbin.clip
            
            if midi_key and midi_key in active_videos:
                print(f"  Clip '{clip['name']}' reached end - looping!")
                
                # Reset the playback position for this clip
                file_path = clip['file_path']
                if file_path in clip_playback_data:
                    clip_playback_data[file_path]['position'] = 0
                
                # Restart from beginning
                GLib.idle_add(lambda: restart_clip_at_end(midi_key, clip))
    
    elif t == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        playbin = message.src
        if hasattr(playbin, 'clip'):
            print(f"Playbin ERROR for '{playbin.clip['name']}': {err}")
    
    return True

def restart_clip_at_end(midi_key, clip):
    """Restart a clip that has reached its natural end"""
    if midi_key in active_videos:
        # Remove and re-add to loop
        remove_video(midi_key)
        add_video(clip, midi_key)
    return False

def add_video(clip, midi_key):
    """Add a new video to the compositor"""
    global active_videos
    
    print(f"\n=== add_video: '{clip['name']}' (key={midi_key}) ===")
    
    if clip.get('exclusive', False) and midi_key in active_videos:
        print(f"  Clip is exclusive and already playing - ignoring")
        return
    
    if len(active_videos) >= MAX_SIMULTANEOUS_VIDEOS:
        oldest_key = next(iter(active_videos))
        print(f"  At capacity ({MAX_SIMULTANEOUS_VIDEOS}), removing oldest: {oldest_key}")
        remove_video(oldest_key)
    
    # Determine start position based on restart_on_play
    file_path = clip['file_path']
    restart_on_play = clip.get('restart_on_play', False)
    
    start_position = 0
    if not restart_on_play and file_path in clip_playback_data:
        start_position = clip_playback_data[file_path].get('position', 0)
        print(f"  Resuming from {start_position:.1f}s (restart_on_play=False)")
    else:
        # Reset position if restart_on_play is True
        if file_path in clip_playback_data:
            clip_playback_data[file_path]['position'] = 0
        print(f"  Starting from beginning (restart_on_play={restart_on_play})")
    
    # Initialize clip data if needed
    if file_path not in clip_playback_data:
        clip_playback_data[file_path] = {'position': 0}
    
    # Create playbin with optional seek
    playbin = start_clip_playback(clip, start_position, midi_key)
    if playbin is None:
        print("  ERROR: Failed to create playbin!")
        return
    
    # Create intervideosrc in main pipeline to receive from playbin
    intervideosrc = Gst.ElementFactory.make("intervideosrc", f"src_{id(clip)}")
    intervideosrc.set_property("channel", f"channel_{id(clip)}")
    main_pipeline.add(intervideosrc)
    intervideosrc.sync_state_with_parent()
    
    # Link to compositor
    src_pad = intervideosrc.get_static_pad("src")
    sink_pad = compositor.get_request_pad("sink_%u")
    src_pad.link(sink_pad)
    
    # Setup looping if configured
    loop_timer_id = None
    loop_duration = clip.get('loop_duration_ms')
    if loop_duration and loop_duration > 0:
        def restart_clip():
            print(f"  Looping clip '{clip['name']}'")
            if midi_key in active_videos:
                remove_video(midi_key)
                add_video(clip, midi_key)
            return False
        
        loop_timer_id = GLib.timeout_add(loop_duration, restart_clip)
        print(f"  Loop timer set for {loop_duration}ms")
    
    # Store active video and record start time
    active_videos[midi_key] = ActiveVideo(
        clip=clip,
        pipeline=playbin,
        compositor_pad=sink_pad,
        loop_timer_id=loop_timer_id,
        midi_key=midi_key,
        start_time=time.time()
    )
    
    # Record start time for position tracking
    clip_playback_data[file_path]['start_time'] = time.time()
    
    print(f"  Added video. Total active: {len(active_videos)}")
    update_layout()
    print("=== add_video complete ===\n")

def remove_video(midi_key):
    """Remove a video from the compositor and update playback position"""
    global active_videos
    
    if midi_key not in active_videos:
        print(f"remove_video: key {midi_key} not found in active videos")
        return
    
    print(f"\n=== remove_video: key={midi_key} ===")
    
    video = active_videos[midi_key]
    
    # Calculate elapsed time and update position
    file_path = video.clip['file_path']
    if file_path in clip_playback_data and 'start_time' in clip_playback_data[file_path]:
        elapsed = time.time() - clip_playback_data[file_path]['start_time']
        clip_playback_data[file_path]['position'] = clip_playback_data[file_path].get('position', 0) + elapsed
        print(f"  Updated position to {clip_playback_data[file_path]['position']:.1f}s")
    
    if video.loop_timer_id is not None:
        GLib.source_remove(video.loop_timer_id)
    
    video.pipeline.set_state(Gst.State.NULL)
    
    intervideosrc = main_pipeline.get_by_name(f"src_{id(video.clip)}")
    if intervideosrc:
        src_pad = intervideosrc.get_static_pad("src")
        peer_pad = src_pad.get_peer()
        if peer_pad:
            src_pad.unlink(peer_pad)
            compositor.release_request_pad(peer_pad)
        
        intervideosrc.set_state(Gst.State.NULL)
        main_pipeline.remove(intervideosrc)
    
    del active_videos[midi_key]
    
    print(f"  Removed video. Total active: {len(active_videos)}")
    update_layout()
    print("=== remove_video complete ===\n")

def on_bus_message(bus, message):
    """Handle GStreamer bus messages"""
    t = message.type
    if t == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        print(f"BUS ERROR: {err}")
        print(f"  Debug: {debug}")
    elif t == Gst.MessageType.WARNING:
        err, debug = message.parse_warning()
        print(f"BUS WARNING: {err}")
    elif t == Gst.MessageType.EOS:
        print("BUS: End of stream")
    elif t == Gst.MessageType.STATE_CHANGED:
        if message.src == main_pipeline:
            old, new, pending = message.parse_state_changed()
            print(f"BUS: Main pipeline state: {old.value_nick} -> {new.value_nick}")
    
    return True

bus.connect("message", on_bus_message)

# Start main pipeline
print("\n=== Starting main pipeline ===")
main_pipeline.set_state(Gst.State.PLAYING)
ret, state, pending = main_pipeline.get_state(2 * Gst.SECOND)
print(f"Main pipeline state: {state.value_nick}")
print("=== Main pipeline ready ===\n")

# -------------------------------
# MIDI Setup
# -------------------------------
print("=== MIDI Setup ===")
print("MIDI backend:", mido.backend)
inputs = mido.get_input_names()

print("Available MIDI inputs:")
for i, name in enumerate(inputs):
    print(f"  {i}: {name}")

PORT_NAME = next((name for name in inputs if "Deluge MIDI 1" in name), None)
if PORT_NAME is None:
    print("WARNING: No Deluge MIDI input found! MIDI control will not work.")
    PORT_NAME = None
else:
    print(f"Opening MIDI input: {PORT_NAME}")

midi_queue = queue.Queue()
stop_event = threading.Event()

def midi_reader(port_name):
    """Thread function to read MIDI messages"""
    try:
        with mido.open_input(port_name) as inport:
            for msg in inport:
                if stop_event.is_set():
                    break
                midi_queue.put((time.monotonic_ns(), msg))
    except Exception as e:
        print(f"MIDI reader error: {e}")

def is_chord_active(clip, channel, active_notes):
    """
    Check if a clip's required chord is currently active.
    
    Args:
        clip: Clip configuration dict with 'required_notes' and 'midi_channel'
        channel: The MIDI channel to check (or -1 for any)
        active_notes: Set of currently active MIDI notes on that channel
    
    Returns:
        True if the clip's chord is satisfied, False otherwise
    """
    required = clip.get('required_notes', frozenset())
    clip_channel = clip.get('midi_channel', -1)
    
    # Empty required notes means this clip shouldn't trigger
    if not required:
        return False
    
    # Check if channel matches
    if clip_channel >= 0 and clip_channel != channel:
        return False
    
    # Check if all required notes are in the active notes
    return required.issubset(active_notes)

def find_active_clips(channel, active_notes):
    """
    Find all clips whose chords are satisfied by the currently active notes.
    
    Args:
        channel: MIDI channel number
        active_notes: Set of active MIDI note numbers on that channel
    
    Returns:
        List of (clip, unique_key) tuples for clips that should be playing
    """
    active_clips = []
    
    for clip in all_clips:
        clip_channel = clip.get('midi_channel', -1)
        
        # If clip wants a specific channel, check only that channel
        if clip_channel >= 0:
            if clip_channel == channel and is_chord_active(clip, channel, active_notes):
                clip_key = (clip['file_path'], channel)
                active_clips.append((clip, clip_key))
        
        # If clip accepts any channel (-1), check this channel
        elif clip_channel == -1:
            if is_chord_active(clip, channel, active_notes):
                # Use the actual channel in the key to distinguish instances
                clip_key = (clip['file_path'], channel)
                active_clips.append((clip, clip_key))
    
    return active_clips

def update_videos_for_channel(channel):
    """
    Update which videos are playing based on the current active notes for a channel.
    This is called whenever notes change on a channel.
    """
    active_notes = active_notes_by_channel.get(channel, set())
    
    print(f"\n=== Updating videos for channel {channel} ===")
    print(f"  Active notes: {sorted(active_notes) if active_notes else '(none)'}")
    
    # Find which clips should be playing now
    should_be_active = find_active_clips(channel, active_notes)
    should_be_active_keys = {clip_key for _, clip_key in should_be_active}
    
    # Get currently active video keys for this channel
    current_keys = {key for key in active_videos.keys() if key[1] == channel}
    
    # Stop videos that should no longer be playing
    for key in current_keys:
        if key not in should_be_active_keys:
            print(f"  Stopping: {active_videos[key].clip['name']}")
            remove_video(key)
    
    # Start videos that should be playing but aren't
    for clip, clip_key in should_be_active:
        if clip_key not in active_videos:
            print(f"  Starting: {clip['name']}")
            add_video(clip, clip_key)

def process_midi_messages():
    """Process MIDI messages from the queue"""
    try:
        while True:
            try:
                ts, msg = midi_queue.get_nowait()
            except queue.Empty:
                break
            
            if msg.type == 'note_on' and msg.velocity > 0:
                print(f"\nMIDI NOTE_ON: ch={msg.channel} note={msg.note} vel={msg.velocity}")
                
                # Add note to active set for this channel
                if msg.channel not in active_notes_by_channel:
                    active_notes_by_channel[msg.channel] = set()
                active_notes_by_channel[msg.channel].add(msg.note)
                
                # Update videos based on new chord state
                update_videos_for_channel(msg.channel)
            
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                print(f"\nMIDI NOTE_OFF: ch={msg.channel} note={msg.note}")
                
                # Remove note from active set for this channel
                if msg.channel in active_notes_by_channel:
                    active_notes_by_channel[msg.channel].discard(msg.note)
                    
                    # Clean up empty sets
                    if not active_notes_by_channel[msg.channel]:
                        del active_notes_by_channel[msg.channel]
                
                # Update videos based on new chord state
                update_videos_for_channel(msg.channel)
                    
    except Exception as e:
        print(f"Error processing MIDI: {e}")
        import traceback
        traceback.print_exc()
    
    return True

# Need to import threading for MIDI reader
import threading

if PORT_NAME:
    reader_thread = threading.Thread(
        target=midi_reader,
        args=(PORT_NAME,),
        daemon=True,
    )
    reader_thread.start()
    print("MIDI listening started")
    GLib.timeout_add(10, process_midi_messages)
else:
    print("MIDI listening skipped (no input found)")

print("=== MIDI Setup Complete ===\n")

# -------------------------------
# Shutdown Handler
# -------------------------------
running = True

def shutdown():
    """Clean shutdown function"""
    global running, loop
    print("\nShutting down...")
    running = False
    stop_event.set()
    
    for key in list(active_videos.keys()):
        remove_video(key)
    
    main_pipeline.set_state(Gst.State.NULL)
    if loop and loop.is_running():
        loop.quit()

def signal_handler(sig, frame):
    """Handle SIGINT (Ctrl+C) from terminal"""
    print("\n^C signal received")
    shutdown()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# -------------------------------
# Start GLib Main Loop
# -------------------------------
print("=== Starting main loop ===")
print("System ready! Using pre-processed optimized clips with resume support...\n")
print("Press Ctrl+C to quit\n")

loop = GLib.MainLoop()

try:
    loop.run()
except KeyboardInterrupt:
    print("\nInterrupted by user")
finally:
    stop_event.set()
    for key in list(active_videos.keys()):
        remove_video(key)
    main_pipeline.set_state(Gst.State.NULL)
    print("Pipeline stopped")
    print("Exited cleanly")