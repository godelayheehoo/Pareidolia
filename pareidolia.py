#!/usr/bin/env python3
"""
MIDI-triggered video player for Raspberry Pi 4
Plays video clips from clips.json when MIDI notes are received
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
    pprint(c)
    print("\n")

# Build lookup dictionaries
keypress_map = {}  # For debug keyboard control
midi_map = {}  # For MIDI control

for clip in clips_config:
    # Debug keypress mapping
    key = clip.get('debug_keypress')
    if key:
        keypress_map[key] = clip
    
    # MIDI mapping: (channel, note) -> clip
    # -1 means "any", so we'll handle that specially
    midi_channel = clip.get('midi_channel', -1)
    midi_note = clip.get('midi_note', -1)
    
    if midi_channel >= 0 and midi_note >= 0:
        # Specific channel and note
        midi_map[(midi_channel, midi_note)] = clip
    elif midi_channel == -1 and midi_note >= 0:
        # Any channel, specific note
        if 'any_channel' not in midi_map:
            midi_map['any_channel'] = {}
        midi_map['any_channel'][midi_note] = clip
    elif midi_channel >= 0 and midi_note == -1:
        # Specific channel, any note
        if 'any_note' not in midi_map:
            midi_map['any_note'] = {}
        midi_map['any_note'][midi_channel] = clip
    elif midi_channel == -1 and midi_note == -1:
        # Any channel, any note
        midi_map['any_any'] = clip

print("\nMIDI mapping created:")
print(f"  Specific mappings: {len([k for k in midi_map.keys() if isinstance(k, tuple)])}")
print(f"  Any-channel notes: {len(midi_map.get('any_channel', {}))}")
print(f"  Any-note channels: {len(midi_map.get('any_note', {}))}")
print(f"  Any-any: {'yes' if 'any_any' in midi_map else 'no'}")

# -------------------------------
# Initialize GStreamer
# -------------------------------
print("\nInitializing GStreamer...")
Gst.init(None)

import os
os.environ['GST_DEBUG'] = '2'  # 0=none, 1=error, 2=warning, 3=info, 4=debug, 5=log

# -------------------------------
# GStreamer Playbin
# -------------------------------
pipeline = Gst.ElementFactory.make("playbin", "player")
pipeline.set_property("uri", clips_config[0].get('file_path', None))

videosink = Gst.ElementFactory.make("autovideosink", "videosink")
pipeline.set_property("video-sink", videosink)

# Timer/loop state
loop_timer_id = None
CHECK_INTERVAL_MS = 100
loop = None  # Will be set later
current_clip = None  # Track current clip for exclusivity

def show_clip(clip):
    """
    Seek to start of clip and play it. If end_sec >= 0, install a GLib timeout
    that checks position and loops back to start when end is reached. If end_sec
    == -1, play indefinitely and remove any existing loop timer.
    """
    global loop_timer_id, current_clip

    print(f"\n=== show_clip called for '{clip['name']}' ===")
    
    # Check if this clip is marked as exclusive and is already playing
    if clip.get('exclusive', False) and current_clip == clip:
        print(f"  Clip '{clip['name']}' is exclusive and already playing - ignoring")
        return
    
    current_clip = clip
    
    # Stop and remove existing loop timer if present
    if loop_timer_id is not None:
        print("  Removing existing loop timer")
        try:
            GLib.source_remove(loop_timer_id)
        except Exception as e:
            print(f"  Error removing timer: {e}")
        loop_timer_id = None

    # If the clip points to a different file, update the pipeline URI
    clip_uri = clip['file_path']
    current_uri = pipeline.get_property('uri')
    
    print(f"  Current URI: {current_uri}")
    print(f"  Target URI:  {clip_uri}")
    
    # Get current state
    ret, state, pending = pipeline.get_state(0)
    print(f"  Current pipeline state: {state.value_nick}, pending: {pending.value_nick}")
    
    # If changing files, need to stop, change URI, then start again
    if clip_uri != current_uri:
        print("  URI changed - resetting pipeline")
        pipeline.set_state(Gst.State.READY)
        ret, state, pending = pipeline.get_state(Gst.CLOCK_TIME_NONE)
        print(f"  After READY: {state.value_nick}")
        
        pipeline.set_property('uri', clip_uri)
        print("  URI property updated")

    # Set to PAUSED state and wait for it to be ready
    print("  Setting to PAUSED")
    pipeline.set_state(Gst.State.PAUSED)
    ret, state, pending = pipeline.get_state(5 * Gst.SECOND)  # 5 second timeout
    print(f"  After PAUSED: ret={ret.value_nick}, state={state.value_nick}, pending={pending.value_nick}")
    
    if ret == Gst.StateChangeReturn.FAILURE:
        print("  ERROR: Failed to pause pipeline!")
        return
    
    start_ns = int(clip['start_sec'] * Gst.SECOND)
    end_sec = clip.get('end_sec', -1)
    end_ns = int(end_sec * Gst.SECOND) if end_sec >= 0 else -1

    print(f"  Seeking to {start_ns / Gst.SECOND}s")
    
    # Now seek while in PAUSED state
    success = pipeline.seek_simple(
        Gst.Format.TIME,
        Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
        start_ns
    )
    
    print(f"  Seek result: {success}")
    
    if success:
        # After successful seek, set to PLAYING
        print("  Setting to PLAYING")
        pipeline.set_state(Gst.State.PLAYING)
        ret, state, pending = pipeline.get_state(1 * Gst.SECOND)
        print(f"  After PLAYING: {state.value_nick}")
        print(f"✓ Playing clip '{clip['name']}' from {clip['start_sec']}s to {clip.get('end_sec', 'end')}s")
    else:
        print(f"✗ Seek failed for clip '{clip['name']}'")
        # Try to play anyway
        pipeline.set_state(Gst.State.PLAYING)

    # If end_sec >= 0, install a timer to loop
    if end_ns >= 0:
        print(f"  Installing loop timer (check every {CHECK_INTERVAL_MS}ms)")
        def check_loop():
            # Query current position
            success_pos, pos = pipeline.query_position(Gst.Format.TIME)
            if success_pos:
                if pos >= end_ns:
                    print(f"  Loop: position {pos/Gst.SECOND}s >= end {end_ns/Gst.SECOND}s, seeking back")
                    pipeline.seek_simple(
                        Gst.Format.TIME,
                        Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                        start_ns
                    )
            # Keep the timer running until explicitly removed
            return True

        loop_timer_id = GLib.timeout_add(CHECK_INTERVAL_MS, check_loop)
    else:
        # For clips that play to end (end_sec == -1), set up loop back to start
        print(f"  Will loop back to start when EOS reached")
        # The bus message handler will handle this
    
    print("=== show_clip complete ===\n")

def on_bus_message(bus, message):
    """Handle GStreamer bus messages for debugging"""
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
        # If current clip has end_sec == -1, loop back to start
        if current_clip and current_clip.get('end_sec', -1) == -1:
            print(f"  Looping clip '{current_clip['name']}' back to start")
            GLib.idle_add(show_clip, current_clip)
    elif t == Gst.MessageType.STATE_CHANGED:
        if message.src == pipeline:
            old, new, pending = message.parse_state_changed()
            print(f"BUS: Pipeline state: {old.value_nick} -> {new.value_nick} (pending: {pending.value_nick})")
    elif t == Gst.MessageType.ASYNC_DONE:
        print("BUS: ASYNC_DONE - pipeline is ready")
    
    return True

# Set up bus watch
bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect("message", on_bus_message)

# Start playing initial file
print("\n=== Initial startup ===")
print("Setting to PAUSED")
pipeline.set_state(Gst.State.PAUSED)
ret, state, pending = pipeline.get_state(5 * Gst.SECOND)
print(f"After PAUSED: {state.value_nick}")

print("Setting to PLAYING")
pipeline.set_state(Gst.State.PLAYING)
ret, state, pending = pipeline.get_state(1 * Gst.SECOND)
print(f"After PLAYING: {state.value_nick}")
print("=== Startup complete ===\n")

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
    PORT_NAME = None  # Will skip MIDI thread
else:
    print(f"Opening MIDI input: {PORT_NAME}")

# MIDI queue and thread
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
            
            # Only process note_on messages with velocity > 0
            if msg.type == 'note_on' and msg.velocity > 0:
                print(f"\nMIDI: ch={msg.channel} note={msg.note} vel={msg.velocity}")
                
                clip = find_clip_for_midi(msg.channel, msg.note)
                if clip:
                    print(f"  → Triggering clip '{clip['name']}'")
                    show_clip(clip)
                else:
                    print(f"  → No clip mapped to ch={msg.channel} note={msg.note}")
    except Exception as e:
        print(f"Error processing MIDI: {e}")
    
    return True  # Keep the timer running

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
    GLib.timeout_add(10, process_midi_messages)  # Check every 10ms
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

# Flag to control the keyboard thread
running = True

def shutdown():
    """Clean shutdown function"""
    global running
    print("\nShutting down...")
    running = False
    stop_event.set()  # Stop MIDI thread
    pipeline.set_state(Gst.State.NULL)
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
            
            # Handle Ctrl+C (character code 3)
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
                # Use GLib.idle_add to call show_clip from the main thread
                GLib.idle_add(show_clip, clip)
            elif key.isprintable():  # Only print for printable characters
                print(f"\nKey '{key}' pressed (no clip mapped)")
                
        except Exception as e:
            print(f"Error in keyboard thread: {e}")
            running = False
            break

# Signal handler for Ctrl+C from the terminal
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
    pipeline.set_state(Gst.State.NULL)
    print("Pipeline stopped")
    print("Exited cleanly")