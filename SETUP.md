# Pareidolia Setup Guide

This guide will help you set up the Pareidolia video playback system on a Raspberry Pi.

## Tested On
- Raspberry Pi 4 Model B
- Raspberry Pi 5
- Raspberry Pi OS (Debian-based)

## Prerequisites
- Fresh Raspberry Pi with Raspberry Pi OS installed
- Internet connection
- SSH access configured (optional, but recommended)
- Git installed and configured

## Installation

### 1. System Update
```bash
sudo apt-get update
sudo apt-get upgrade -y
```

### 2. Install Python and Development Tools
```bash
sudo apt-get install -y python3 python3-pip python3-venv python3-dev
```

### 3. Install GStreamer and GObject Introspection
GStreamer handles video playback. We install it as system packages for better hardware acceleration support on Raspberry Pi.

```bash
sudo apt-get install -y \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    gstreamer1.0-plugins-ugly \
    gstreamer1.0-libav \
    python3-gi \
    python3-gi-cairo \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0
```

**What these packages do:**
- `gstreamer1.0-*`: Video codec support and playback engines
- `python3-gi`: Python GObject Introspection bindings
- `gir1.2-*`: Type libraries for GStreamer

### 4. Install MIDI Support Libraries
```bash
sudo apt-get install -y libasound2-dev libjack-jackd2-dev
```

### 5. Install uv (Optional but Recommended)
[uv](https://github.com/astral-sh/uv) is a fast Python package installer. It's much faster than pip.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.cargo/env
```

To make uv available in future sessions, add this to your `~/.bashrc`:
```bash
echo 'source $HOME/.cargo/env' >> ~/.bashrc
```

### 6. Clone the Repository
```bash
cd ~
mkdir -p projects
cd projects
git clone <your-repo-url> Pareidolia
cd Pareidolia
```

### 7. Set Up Python Virtual Environment
We use `--system-site-packages` to access the system's GStreamer installation.

```bash
python3 -m venv --system-site-packages venv
source venv/bin/activate
```

**To activate the virtual environment in future sessions:**
```bash
cd ~/projects/Pareidolia
source venv/bin/activate
```

### 8. Install Python Dependencies

**Option A: Using uv (recommended, faster)**
```bash
uv pip install mido python-rtmidi
```

**Option B: Using pip**
```bash
pip install --break-system-packages mido python-rtmidi
```

**Note:** The `--break-system-packages` flag is needed because we're mixing pip packages with system packages. This is safe in a virtual environment.

## Verification

Test that everything is installed correctly:

### Test GStreamer
```bash
python3 -c "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst; Gst.init(None); print('✓ GStreamer OK')"
```

### Test MIDI
```bash
python3 -c "import mido; print('✓ MIDI OK')"
```

### List MIDI Devices
```bash
python3 -c "import mido; print('Available MIDI inputs:'); print(mido.get_input_names())"
```

## Configuration

### Download Video Clips
If your `clips.json` references video files that need to be downloaded:

```bash
python clip_download_helper.py
```

### Configure clips.json
Edit `clips.json` to set up your video clips and MIDI mappings. Example structure:

```json
{
  "clips": [
    {
      "name": "intro_loop",
      "file_path": "videos/my_video.mp4",
      "start_sec": 0.0,
      "end_sec": 10.0,
      "midi_channel": 1,
      "midi_note": 36,
      "debug_keypress": "j"
    }
  ]
}
```

## Running the Application

### Basic Usage
```bash
source venv/bin/activate
python video_player_rpi_fixed.py
```

### Keyboard Controls
- Press the mapped keys (see `debug_keypress` in clips.json) to trigger clips
- Press `q` to quit
- Press `Ctrl+C` to force quit

## Troubleshooting

### "Could not create gtksink" Error
This is normal on Raspberry Pi. The application will automatically fall back to `autovideosink`, which uses hardware acceleration.

### Video Not Displaying
Ensure you have a display connected via HDMI. The video will render to the primary display output.

### MIDI Device Not Found
Check available MIDI devices:
```bash
python3 -c "import mido; print(mido.get_input_names())"
```

Make sure your MIDI device is connected and recognized by the system:
```bash
aconnect -l
```

### Permission Errors with MIDI
Add your user to the audio group:
```bash
sudo usermod -a -G audio $USER
```
Log out and back in for this to take effect.

### Python Version Issues
This project requires Python 3.9 or later. Check your version:
```bash
python3 --version
```

## Performance Tips

### For Raspberry Pi 4/5
- Use hardware-accelerated video formats (H.264, H.265)
- Keep video resolution at 1080p or lower for best performance
- Close unnecessary applications to free up RAM
- Consider running headless (without desktop environment) for better performance

### Enabling GPU Memory
Edit `/boot/firmware/config.txt` and ensure GPU memory is allocated:
```
gpu_mem=256
```

Reboot after making this change.

## Development

### Installing Additional Dependencies
Always activate the virtual environment first:
```bash
source venv/bin/activate
uv pip install <package-name>
# or
pip install --break-system-packages <package-name>
```

### Updating Dependencies
```bash
source venv/bin/activate
uv pip install --upgrade mido python-rtmidi
```

## System Information

To check your Raspberry Pi model and OS version:
```bash
cat /proc/device-tree/model
cat /etc/os-release
```

## Additional Resources

- [GStreamer Documentation](https://gstreamer.freedesktop.org/documentation/)
- [python-rtmidi Documentation](https://spotlightkid.github.io/python-rtmidi/)
- [mido Documentation](https://mido.readthedocs.io/)

## License

[Your License Here]

## Contributing

[Your Contributing Guidelines Here]
