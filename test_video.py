import gi
gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gst, Gtk, Gdk, GLib
from pathlib import Path
import json
from pprint import pprint


#-------------------------------
# Load Clips Configuration
#-------------------------------
with open("clips.json", "r") as f:
    clips_config = json.load(f)['clips']

for c in clips_config:
    #convert to URIs
    c['file_path'] = str(Path(c['file_path']).resolve().as_uri())
    pprint(c)
    print("\n")

# -------------------------------
# Initialize GStreamer
# -------------------------------
Gst.init(None)

# Set debug level to see what's happening
import os
os.environ['GST_DEBUG'] = '2'  # 0=none, 1=error, 2=warning, 3=info, 4=debug, 5=log

# -------------------------------
# GTK Window
# -------------------------------
win = Gtk.Window(title="Video Player")
win.set_default_size(800, 600)
win.connect("destroy", Gtk.main_quit)

# -------------------------------
# GStreamer Playbin with gtksink
# -------------------------------
pipeline = Gst.ElementFactory.make("playbin", "player")
pipeline.set_property("uri", clips_config[0].get('file_path', None))

# gtksink automatically creates a GTK widget for video
gtksink = Gst.ElementFactory.make("gtksink", "videosink")
if not gtksink:
    print("ERROR: Could not create gtksink, trying xvimagesink")
    gtksink = Gst.ElementFactory.make("xvimagesink", "videosink")
    
pipeline.set_property("video-sink", gtksink)

# Get the GTK widget from gtksink
video_widget = gtksink.get_property("widget")
video_widget.set_can_focus(True)
video_widget.grab_focus()

win.add(video_widget)
win.show_all()

# Timer/loop state
loop_timer_id = None
CHECK_INTERVAL_MS = 100

def show_clip(clip):
    """
    Seek to start of clip and play it. If end_sec >= 0, install a GLib timeout
    that checks position and loops back to start when end is reached. If end_sec
    == -1, play indefinitely and remove any existing loop timer.
    """
    global loop_timer_id

    print(f"\n=== show_clip called for '{clip['name']}' ===")
    
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

# Start playing initial file - go to PAUSED first, then PLAYING
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
# Keyboard Event Handling
# -------------------------------
def on_key_press(widget, event):
    key = Gdk.keyval_name(event.keyval)
    print(f"\nKey pressed: {key}")

    matched = False
    for clip in clips_config:
        dbg = clip.get('debug_keypress')
        if dbg and key == dbg:
            print(f"Matched debug keypress for clip '{clip['name']}'")
            show_clip(clip)
            matched = True
            break
    if not matched:
        print(f"No debug keypress matched")

win.connect("key-press-event", on_key_press)

# -------------------------------
# Start GTK Main Loop
# -------------------------------
Gtk.main()