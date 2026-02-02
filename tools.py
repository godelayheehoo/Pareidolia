def note_to_midi(note: str) -> int:
    pitch_classes = {
        "C": 0,
        "C#": 1,
        "D": 2,
        "D#": 3,
        "E": 4,
        "F": 5,
        "F#": 6,
        "G": 7,
        "G#": 8,
        "A": 9,
        "A#": 10,
        "B": 11,
    }

    # split note into pitch and octave
    if note[1] == "#":
        pitch = note[:2]
        octave = int(note[2:])
    else:
        pitch = note[0]
        octave = int(note[1:])

    return (octave + 2) * 12 + pitch_classes[pitch]
