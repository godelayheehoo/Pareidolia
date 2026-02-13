#!/usr/bin/env python3
"""
Multi-video MIDI-triggered player for Raspberry Pi 4
Uses pre-processed, optimized clip files for better performance
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
MAX_SIMULTANEOUS_VIDEOS = 6

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
    
    # Convert MIDI note if string
    if isinstance(c['midi_note'], str):
        c['midi_note'] = note_to_midi(c['midi_note'])
    if c['midi_channel'] > 0:
        c['midi_channel'] = c['midi_channel'] - 1  # Convert to 0-based
    
    pprint(c)
    print("\n")

# Build lookup dictionaries
keypress_map = {}
midi_map = {}

for clip in clips_config:
    key = clip.get('debug_keypress')
    if key:
        keypress_map[key] = clip
    
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

def start_clip_playback(clip):
    """
    Create playbin for a pre-processed clip.
    Since clips are pre-extracted, no seeking needed - just play from start!
    """
    playbin = Gst.ElementFactory.make("playbin", f"player_{id(clip)}")
    playbin.set_property("uri", clip['file_path'])
    
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
    
    # No need to seek - processed clips start at 0!
    # Just set to PLAYING
    playbin.set_state(Gst.State.PLAYING)
    
    return playbin

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
    
    # Create playbin - no seeking needed!
    playbin = start_clip_playback(clip)
    if playbin is None:
        print("  Failed to create playbin")
        return
    
    # Create intervideosrc
    intervideosrc = Gst.ElementFactory.make("intervideosrc", f"src_{id(clip)}")
    intervideosrc.set_property("channel", f"channel_{id(clip)}")
    
    main_pipeline.add(intervideosrc)
    
    # Get compositor pad
    compositor_pad = compositor.request_pad_simple("sink_%u")
    
    # Link to compositor
    src_pad = intervideosrc.get_static_pad("src")
    src_pad.link(compositor_pad)
    
    intervideosrc.sync_state_with_parent()
    
    # Set up looping if needed
    # For processed clips, end_sec is already adjusted (0-based)
    loop_timer_id = None
    end_sec = clip.get('end_sec', -1)
    
    if end_sec >= 0:
        end_ns = int(end_sec * Gst.SECOND)
        
        def check_loop():
            success_pos, pos = playbin.query_position(Gst.Format.TIME)
            if success_pos and pos >= end_ns:
                # Loop back to start (which is 0 for processed clips)
                playbin.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    0  # Start is always 0 for processed clips!
                )
            return True
        
        loop_timer_id = GLib.timeout_add(100, check_loop)
    
    # Create ActiveVideo
    active_video = ActiveVideo(
        clip=clip,
        pipeline=playbin,
        compositor_pad=compositor_pad,
        loop_timer_id=loop_timer_id,
        midi_key=midi_key,
        start_time=time.time()
    )
    
    active_videos[midi_key] = active_video
    
    print(f"  Added video. Total active: {len(active_videos)}")
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

def find_clip_for_midi(channel, note):
    """Find the appropriate clip for a MIDI channel/note combination"""
    if (channel, note) in midi_map:
        return midi_map[(channel, note)]
    if 'any_channel' in midi_map and note in midi_map['any_channel']:
        return midi_map['any_channel'][note]
    if 'any_note' in midi_map and channel in midi_map['any_note']:
        return midi_map['any_note'][channel]
    if 'any_any' in midi_map:
        return midi_map['any_any']
    return None

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
                
                clip = find_clip_for_midi(msg.channel, msg.note)
                if clip:
                    print(f"  → Triggering clip '{clip['name']}'")
                    midi_key = (msg.channel, msg.note)
                    add_video(clip, midi_key)
                else:
                    print(f"  → No clip mapped to ch={msg.channel} note={msg.note}")
            
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
# Keyboard Input Handler
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
    global running, loop
    print("\nShutting down...")
    running = False
    stop_event.set()
    
    for key in list(active_videos.keys()):
        remove_video(key)
    
    main_pipeline.set_state(Gst.State.NULL)
    if loop and loop.is_running():
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
    shutdown()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

kbd_thread = threading.Thread(target=keyboard_thread, daemon=True)
kbd_thread.start()

# -------------------------------
# Start GLib Main Loop
# -------------------------------
print("=== Starting main loop ===")
print("System ready! Using pre-processed optimized clips...\n")

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
