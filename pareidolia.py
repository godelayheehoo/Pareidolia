#!/usr/bin/env python3
"""
Multi-video MIDI-triggered player for Raspberry Pi 4
Supports up to 4 simultaneous video clips triggered by MIDI
Uses GStreamer compositor for video mixing
"""

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
from pathlib import Path
import json
from pprint import pprint
import sys
import termios
import tty
import threading
import signal
import mido
import queue
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional
from tools import note_to_midi

# -------------------------------
# Configuration
# -------------------------------
MAX_SIMULTANEOUS_VIDEOS = 4

# -------------------------------
# Load Clips Configuration
# -------------------------------
print("Loading clips configuration...")
with open("clips.json", "r") as f:
    clips_config = json.load(f)['clips']

for c in clips_config:
    # Convert to URIs
    if not Path(c['file_path']).exists():
        raise FileNotFoundError(
            f"Clip file does not exist: {c['file_path']}. "
            f"Note the presence of clip_download_helper.py to download missing files."
        )
    c['file_path'] = str(Path(c['file_path']).resolve().as_uri())
    if c['midi_channel']>0:
        c['midi_channel'] = c['midi_channel']-1  # Convert to 0-based

    if isinstance(c['midi_note'], str):
        c['midi_note'] = note_to_midi(c['midi_note'])

    pprint(c)
    print("\n")

# Build lookup dictionaries
keypress_map = {}  # For debug keyboard control
midi_map = {}  # For MIDI control: (channel, note) -> clip

for clip in clips_config:
    # Debug keypress mapping
    key = clip.get('debug_keypress')
    if key:
        keypress_map[key] = clip
    
    # MIDI mapping: (channel, note) -> clip
    midi_channel = clip.get('midi_channel', -1)
    midi_note = clip.get('midi_note', -1)
    
    if midi_channel >= 0 and midi_note >= 0:
        midi_map[(midi_channel, midi_note)] = clip
    elif midi_channel == -1 and midi_note >= 0:
        if 'any_channel' not in midi_map:
            midi_map['any_channel'] = {}
        midi_map['any_channel'][midi_note] = clip
    elif midi_channel >= 0 and midi_note == -1:
        if 'any_note' not in midi_map:
            midi_map['any_note'] = {}
        midi_map['any_note'][midi_channel] = clip
    elif midi_channel == -1 and midi_note == -1:
        midi_map['any_any'] = clip

print("\nMIDI mapping created:")
print(f"  Specific mappings: {len([k for k in midi_map.keys() if isinstance(k, tuple)])}")

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
    midi_key: tuple = field(default_factory=tuple)  # (channel, note) for tracking
    start_time: float = field(default_factory=time.time)

# OrderedDict to track active videos (insertion order = age)
active_videos: OrderedDict[tuple, ActiveVideo] = OrderedDict()

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

# Create main pipeline
main_pipeline = Gst.Pipeline.new("main_pipeline")

# Create compositor
compositor = Gst.ElementFactory.make("compositor", "compositor")
compositor.set_property("background", 1)  # Black background

# Create video sink
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

# Add elements to pipeline
main_pipeline.add(compositor)
main_pipeline.add(videosink)

# Link compositor to videosink
compositor.link(videosink)

# Get bus for message handling
bus = main_pipeline.get_bus()
bus.add_signal_watch()

def calculate_layout(num_videos):
    """
    Calculate positions and sizes for videos based on count.
    Returns list of (x, y, width, height) in pixels for 1920x1080 output.
    """
    width = 1920
    height = 1080
    
    if num_videos == 1:
        return [(0, 0, width, height)]
    elif num_videos == 2:
        # Side by side
        half_width = width // 2
        return [
            (0, 0, half_width, height),
            (half_width, 0, half_width, height)
        ]
    elif num_videos == 3:
        # One on left, two stacked on right
        half_width = width // 2
        half_height = height // 2
        return [
            (0, 0, half_width, height),
            (half_width, 0, half_width, half_height),
            (half_width, half_height, half_width, half_height)
        ]
    elif num_videos == 4:
        # 2x2 grid
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
        
        # Set pad properties for position and size
        pad.set_property("xpos", x)
        pad.set_property("ypos", y)
        pad.set_property("width", w)
        pad.set_property("height", h)
        
        print(f"  Video {idx} '{video.clip['name']}' -> pos({x},{y}) size({w}x{h})")

def start_clip_playback(clip, start_ns, end_ns):
    """
    Create a new playbin pipeline for a clip and connect it to the compositor.
    Returns the pipeline.
    """
    # Create playbin for this clip
    playbin = Gst.ElementFactory.make("playbin", f"player_{id(clip)}")
    playbin.set_property("uri", clip['file_path'])
    
    # Create a videoscale and videoconvert for the output
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
    
    # Set to PAUSED and wait
    playbin.set_state(Gst.State.PAUSED)
    ret, state, pending = playbin.get_state(5 * Gst.SECOND)
    
    if ret == Gst.StateChangeReturn.FAILURE:
        print(f"ERROR: Failed to pause playbin for clip '{clip['name']}'")
        return None
    
    # Seek to start position
    success = playbin.seek_simple(
        Gst.Format.TIME,
        Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
        start_ns
    )
    
    if not success:
        print(f"WARNING: Seek failed for clip '{clip['name']}'")
    
    # Set to PLAYING
    playbin.set_state(Gst.State.PLAYING)
    
    return playbin

def add_video(clip, midi_key):
    """
    Add a new video to the compositor.
    If at max capacity, remove the oldest video first.
    """
    global active_videos
    
    print(f"\n=== add_video: '{clip['name']}' (key={midi_key}) ===")
    
    # Check if this exact clip is already playing (for exclusive clips)
    if clip.get('exclusive', False) and midi_key in active_videos:
        print(f"  Clip is exclusive and already playing - ignoring")
        return
    
    # If at capacity, remove oldest video
    if len(active_videos) >= MAX_SIMULTANEOUS_VIDEOS:
        oldest_key = next(iter(active_videos))
        print(f"  At capacity ({MAX_SIMULTANEOUS_VIDEOS}), removing oldest: {oldest_key}")
        remove_video(oldest_key)
    
    # Calculate timing
    start_ns = int(clip['start_sec'] * Gst.SECOND)
    end_sec = clip.get('end_sec', -1)
    end_ns = int(end_sec * Gst.SECOND) if end_sec >= 0 else -1
    
    # Create playbin pipeline
    playbin = start_clip_playback(clip, start_ns, end_ns)
    if playbin is None:
        print("  Failed to create playbin")
        return
    
    # Create intervideosrc to receive video from the playbin
    intervideosrc = Gst.ElementFactory.make("intervideosrc", f"src_{id(clip)}")
    intervideosrc.set_property("channel", f"channel_{id(clip)}")
    
    # Add intervideosrc to main pipeline
    main_pipeline.add(intervideosrc)
    
    # Request a new sink pad from compositor
    compositor_pad = compositor.get_request_pad("sink_%u")
    
    # Link intervideosrc to compositor pad
    src_pad = intervideosrc.get_static_pad("src")
    src_pad.link(compositor_pad)
    
    # Set intervideosrc to PLAYING
    intervideosrc.sync_state_with_parent()
    
    # Set up loop timer if needed
    loop_timer_id = None
    if end_ns >= 0:
        def check_loop():
            success_pos, pos = playbin.query_position(Gst.Format.TIME)
            if success_pos and pos >= end_ns:
                playbin.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    start_ns
                )
            return True
        
        loop_timer_id = GLib.timeout_add(100, check_loop)
    
    # Create ActiveVideo object
    active_video = ActiveVideo(
        clip=clip,
        pipeline=playbin,
        compositor_pad=compositor_pad,
        loop_timer_id=loop_timer_id,
        midi_key=midi_key,
        start_time=time.time()
    )
    
    # Add to active videos
    active_videos[midi_key] = active_video
    
    print(f"  Added video. Total active: {len(active_videos)}")
    
    # Update layout
    update_layout()
    
    print("=== add_video complete ===\n")

def remove_video(midi_key):
    """Remove a video from the compositor"""
    global active_videos
    
    if midi_key not in active_videos:
        print(f"remove_video: key {midi_key} not found in active videos")
        return
    
    print(f"\n=== remove_video: key={midi_key} ===")
    
    video = active_videos[midi_key]
    
    # Stop loop timer if exists
    if video.loop_timer_id is not None:
        GLib.source_remove(video.loop_timer_id)
    
    # Stop playbin
    video.pipeline.set_state(Gst.State.NULL)
    
    # Find and remove intervideosrc from main pipeline
    intervideosrc = main_pipeline.get_by_name(f"src_{id(video.clip)}")
    if intervideosrc:
        # Unlink from compositor
        src_pad = intervideosrc.get_static_pad("src")
        peer_pad = src_pad.get_peer()
        if peer_pad:
            src_pad.unlink(peer_pad)
            compositor.release_request_pad(peer_pad)
        
        # Remove from pipeline
        intervideosrc.set_state(Gst.State.NULL)
        main_pipeline.remove(intervideosrc)
    
    # Remove from active videos
    del active_videos[midi_key]
    
    print(f"  Removed video. Total active: {len(active_videos)}")
    
    # Update layout for remaining videos
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
    print("Available inputs:", inputs)
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

def find_clip_for_midi(channel, note):
    """Find the appropriate clip for a MIDI channel/note combination"""
    # Check specific (channel, note) mapping first
    if (channel, note) in midi_map:
        return midi_map[(channel, note)]
    
    # Check any-channel for this note
    if 'any_channel' in midi_map and note in midi_map['any_channel']:
        return midi_map['any_channel'][note]
    
    # Check any-note for this channel
    if 'any_note' in midi_map and channel in midi_map['any_note']:
        return midi_map['any_note'][channel]
    
    # Check any-any
    if 'any_any' in midi_map:
        return midi_map['any_any']
    
    return None

def process_midi_messages():
    """Process MIDI messages from the queue (called by GLib timer)"""
    try:
        while True:
            try:
                ts, msg = midi_queue.get_nowait()
            except queue.Empty:
                break
            
            # Process note_on messages with velocity > 0
            if msg.type == 'note_on' and msg.velocity > 0:
                print(f"\nMIDI NOTE_ON: ch={msg.channel} note={msg.note} vel={msg.velocity}")
                
                clip = find_clip_for_midi(msg.channel, msg.note)
                if clip:
                    print(f"  → Triggering clip '{clip['name']}'")
                    midi_key = (msg.channel, msg.note)
                    add_video(clip, midi_key)
                else:
                    print(f"  → No clip mapped to ch={msg.channel} note={msg.note}")
            
            # Process note_off messages (or note_on with velocity 0)
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                print(f"\nMIDI NOTE_OFF: ch={msg.channel} note={msg.note}")
                midi_key = (msg.channel, msg.note)
                
                if midi_key in active_videos:
                    print(f"  → Stopping video for ch={msg.channel} note={msg.note}")
                    remove_video(midi_key)
                else:
                    print(f"  → No active video for ch={msg.channel} note={msg.note}")
                    
    except Exception as e:
        print(f"Error processing MIDI: {e}")
        import traceback
        traceback.print_exc()
    
    return True

# Start MIDI reader thread if port available
if PORT_NAME:
    reader_thread = threading.Thread(
        target=midi_reader,
        args=(PORT_NAME,),
        daemon=True,
    )
    reader_thread.start()
    print("MIDI listening started")
    
    # Install GLib timer to check MIDI queue
    GLib.timeout_add(10, process_midi_messages)
else:
    print("MIDI listening skipped (no input found)")

print("=== MIDI Setup Complete ===\n")

# -------------------------------
# Keyboard Input Handler (Debug)
# -------------------------------
def getch():
    """Get a single character from stdin without waiting for Enter"""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch

running = True

def shutdown():
    """Clean shutdown function"""
    global running
    print("\nShutting down...")
    running = False
    stop_event.set()
    
    # Stop all active videos
    for key in list(active_videos.keys()):
        remove_video(key)
    
    main_pipeline.set_state(Gst.State.NULL)
    if loop:
        loop.quit()

def keyboard_thread():
    """Thread to handle keyboard input for debugging"""
    global running
    print("\n=== Keyboard control active ===")
    print("Press keys to trigger clips (see clips.json for debug_keypress mappings)")
    print("Available keys:", ", ".join(sorted(keypress_map.keys())))
    print("Press 'q' to quit or Ctrl+C\n")
    
    while running:
        try:
            key = getch()
            
            if ord(key) == 3:  # Ctrl+C
                print("\n^C detected")
                GLib.idle_add(shutdown)
                break
            
            if key == 'q':
                print("\nQuitting...")
                GLib.idle_add(shutdown)
                break
            
            if key in keypress_map:
                clip = keypress_map[key]
                print(f"\nKey '{key}' pressed → triggering clip '{clip['name']}'")
                # Use a fake MIDI key for keyboard triggers
                midi_key = ('keyboard', ord(key))
                GLib.idle_add(add_video, clip, midi_key)
            elif key.isprintable():
                print(f"\nKey '{key}' pressed (no clip mapped)")
                
        except Exception as e:
            print(f"Error in keyboard thread: {e}")
            running = False
            break

def signal_handler(sig, frame):
    """Handle SIGINT (Ctrl+C) from terminal"""
    print("\n^C signal received")
    GLib.idle_add(shutdown)

signal.signal(signal.SIGINT, signal_handler)

# Start keyboard input thread
kbd_thread = threading.Thread(target=keyboard_thread, daemon=True)
kbd_thread.start()

# -------------------------------
# Start GLib Main Loop
# -------------------------------
print("=== Starting main loop ===")
print("System ready! Waiting for MIDI input or keyboard commands...\n")

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