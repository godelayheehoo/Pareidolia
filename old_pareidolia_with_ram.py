#!/usr/bin/env python3
"""
Multi-video MIDI-triggered player for Raspberry Pi 4 - RAM PRELOADED VERSION
Loads all video clips into RAM at startup for instant, high-performance playback.
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

print("=" * 60)
print("RAM-PRELOADED VIDEO PLAYER")
print("Loading all clips into memory for maximum performance...")
print("=" * 60)

# -------------------------------
# Load Clips Configuration
# -------------------------------
print("\nLoading clips configuration...")
with open("processed_clips.json", "r") as f:
    clips_config = json.load(f)['clips']

# Preload all video files into RAM
print("\nPreloading video files into RAM...")
for c in clips_config:
    file_path = Path(c['file_path'])
    if not file_path.exists():
        raise FileNotFoundError(f"Clip file does not exist: {file_path}")
    
    # Read entire file into memory
    print(f"  Loading: {file_path.name} ({file_path.stat().st_size / 1024 / 1024:.1f} MB)")
    with open(file_path, 'rb') as f:
        c['file_data'] = f.read()
    
    # Convert to URI for GStreamer (we'll use appsrc instead though)
    c['file_path'] = str(file_path.resolve().as_uri())
    
    # Convert MIDI note if string
    if isinstance(c['midi_note'], str):
        c['midi_note'] = note_to_midi(c['midi_note'])
    if c['midi_channel'] > 0:
        c['midi_channel'] = c['midi_channel'] - 1  # Convert to 0-based
    
    pprint({k: v for k, v in c.items() if k != 'file_data'})  # Don't print binary data
    print()

total_ram = sum(len(c['file_data']) for c in clips_config) / 1024 / 1024
print(f"Total RAM used: {total_ram:.1f} MB")
print("All clips loaded!\n")

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

print("MIDI mapping created")

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
    appsrc: Gst.Element
    loop_timer_id: Optional[int] = None
    midi_key: tuple = field(default_factory=tuple)
    start_time: float = field(default_factory=time.time)
    playback_position: int = 0  # For looping

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
    print("Note: Press F11 for fullscreen")
else:
    print("Console mode detected - using kmssink")
    videosink = Gst.ElementFactory.make("kmssink", "videosink")
    if videosink:
        print("kmssink loaded successfully")
        try:
            videosink.set_property("fullscreen", True)
        except:
            pass
    else:
        print("WARNING: kmssink not available, using autovideosink")
        videosink = Gst.ElementFactory.make("autovideosink", "videosink")

main_pipeline.add(compositor)
main_pipeline.add(videosink)
compositor.link(videosink)

bus = main_pipeline.get_bus()
bus.add_signal_watch()

def calculate_layout(num_videos):
    """Calculate positions for videos"""
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
    """Update the position and size of all active video pads"""
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

def create_ram_pipeline(clip):
    """
    Create a pipeline that plays from RAM using appsrc.
    Since clips are pre-extracted, we just play from start to end.
    """
    pipeline = Gst.Pipeline.new(f"pipeline_{id(clip)}")
    
    # Create appsrc - feeds data from RAM
    appsrc = Gst.ElementFactory.make("appsrc", "appsrc")
    appsrc.set_property("format", Gst.Format.TIME)
    appsrc.set_property("is-live", True)
    appsrc.set_property("block", False)
    
    # Create decoder pipeline
    decodebin = Gst.ElementFactory.make("decodebin", "decodebin")
    queue = Gst.ElementFactory.make("queue", "queue")
    videoconvert = Gst.ElementFactory.make("videoconvert", "videoconvert")
    videoscale = Gst.ElementFactory.make("videoscale", "videoscale")
    sink = Gst.ElementFactory.make("intervideosink", "intervideosink")
    sink.set_property("channel", f"channel_{id(clip)}")
    
    pipeline.add(appsrc)
    pipeline.add(decodebin)
    pipeline.add(queue)
    pipeline.add(videoconvert)
    pipeline.add(videoscale)
    pipeline.add(sink)
    
    # Link static elements
    appsrc.link(decodebin)
    queue.link(videoconvert)
    videoconvert.link(videoscale)
    videoscale.link(sink)
    
    # Handle dynamic pad from decodebin
    def on_pad_added(element, pad):
        caps = pad.get_current_caps()
        if caps:
            structure = caps.get_structure(0)
            if structure.get_name().startswith("video/"):
                sink_pad = queue.get_static_pad("sink")
                if not sink_pad.is_linked():
                    pad.link(sink_pad)
    
    decodebin.connect("pad-added", on_pad_added)
    
    # Feed data from RAM
    file_data = clip['file_data']
    chunk_size = 4096
    offset = 0
    
    def need_data(src, length):
        nonlocal offset
        if offset < len(file_data):
            chunk = file_data[offset:offset + chunk_size]
            buf = Gst.Buffer.new_wrapped(chunk)
            src.emit("push-buffer", buf)
            offset += len(chunk)
        else:
            # Loop: reset to beginning
            offset = 0
            if clip.get('end_sec', -1) != -1:  # Only loop if not playing to end
                chunk = file_data[offset:offset + chunk_size]
                buf = Gst.Buffer.new_wrapped(chunk)
                src.emit("push-buffer", buf)
                offset += len(chunk)
            else:
                src.emit("end-of-stream")
    
    appsrc.connect("need-data", need_data)
    
    return pipeline, appsrc

def add_video(clip, midi_key):
    """Add a new video to the compositor"""
    global active_videos
    
    print(f"\n=== add_video: '{clip['name']}' (key={midi_key}) ===")
    
    if clip.get('exclusive', False) and midi_key in active_videos:
        print(f"  Clip is exclusive and already playing - ignoring")
        return
    
    if len(active_videos) >= MAX_SIMULTANEOUS_VIDEOS:
        oldest_key = next(iter(active_videos))
        print(f"  At capacity, removing oldest: {oldest_key}")
        remove_video(oldest_key)
    
    # Create RAM-based pipeline
    pipeline, appsrc = create_ram_pipeline(clip)
    
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
    
    # Start the pipeline
    pipeline.set_state(Gst.State.PLAYING)
    
    # Create ActiveVideo object
    active_video = ActiveVideo(
        clip=clip,
        pipeline=pipeline,
        compositor_pad=compositor_pad,
        appsrc=appsrc,
        loop_timer_id=None,
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
        return
    
    print(f"\n=== remove_video: key={midi_key} ===")
    
    video = active_videos[midi_key]
    
    # Stop pipeline
    video.pipeline.set_state(Gst.State.NULL)
    
    # Remove intervideosrc
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
    print("WARNING: No Deluge MIDI input found!")
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
                    print(f"  → No clip mapped")
            
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                print(f"\nMIDI NOTE_OFF: ch={msg.channel} note={msg.note}")
                midi_key = (msg.channel, msg.note)
                
                if midi_key in active_videos:
                    print(f"  → Stopping video")
                    remove_video(midi_key)
                    
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
    print("MIDI listening skipped")

print("=== MIDI Setup Complete ===\n")

# -------------------------------
# Keyboard Input Handler
# -------------------------------
def getch():
    """Get a single character from stdin"""
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
    """Clean shutdown"""
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
    """Handle keyboard input"""
    global running
    print("\n=== Keyboard control active ===")
    print("Available keys:", ", ".join(sorted(keypress_map.keys())))
    print("Press 'q' to quit or Ctrl+C\n")
    
    while running:
        try:
            key = getch()
            
            if ord(key) == 3 or key == 'q':
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
    """Handle SIGINT (Ctrl+C)"""
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
print("System ready! All clips in RAM - zero latency!\n")

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