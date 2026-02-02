import mido
import threading
import queue
import time

# ---------- MIDI setup ----------
print("MIDI backend:", mido.backend)
inputs = mido.get_input_names()

print("Available MIDI inputs:")
for i, name in enumerate(inputs):
    print(f"  {i}: {name}")

PORT_NAME = next((name for name in inputs if "Deluge MIDI 1" in name), None)
if PORT_NAME is None:
    raise RuntimeError("No Deluge MIDI input found!")

print(f"Opening MIDI input: {PORT_NAME}")

# ---------- Thread plumbing ----------
midi_queue = queue.Queue()
stop_event = threading.Event()


def midi_reader(port_name):
    with mido.open_input(port_name) as inport:
        for msg in inport:
            if stop_event.is_set():
                break
            midi_queue.put((time.monotonic_ns(), msg))


reader_thread = threading.Thread(
    target=midi_reader,
    args=(PORT_NAME,),
    daemon=True,   # critical: allows clean process exit
)
reader_thread.start()

print("Listening for MIDI (Ctrl+C to exit)...")

# ---------- Main loop (current consumer) ----------
try:
    while True:
        try:
            ts, msg = midi_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        if msg.type in ("note_on", "note_off"):
            print(
                f"t={ts} "
                f"ch={msg.channel} "
                f"type={msg.type} "
                f"note={msg.note} "
                f"vel={msg.velocity}"
            )

except KeyboardInterrupt:
    print("\nExiting cleanly")
    stop_event.set()
    reader_thread.join(timeout=1)
