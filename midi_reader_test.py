import mido
import time

# --- List available inputs ---
print("MIDI backend:", mido.backend)
inputs = mido.get_input_names()
print("Available MIDI inputs:")
for i, name in enumerate(inputs):
    print(f"  {i}: {name}")

# --- Choose the correct Deluge USB input ---
# Replace with the port you actually want; usually "Deluge MIDI 1" works
PORT_NAME = next((name for name in inputs if "Deluge MIDI 1" in name), None)
if PORT_NAME is None:
    raise RuntimeError("No Deluge MIDI input found!")

print(f"Opening MIDI input: {PORT_NAME}")


def midi_callback(msg):
    if msg.type in ("note_on", "note_off"):
        print(
            f"channel={msg.channel} "
            f"type={msg.type} "
            f"note={msg.note} "
            f"velocity={msg.velocity}"
        )

with mido.open_input(PORT_NAME, callback=midi_callback):
    print("Listening for MIDI (press Ctrl+C to exit)...")
    try:
        while True:
            time.sleep(1)  # main thread just idles
    except KeyboardInterrupt:
        print("\nExiting cleanly")
