from flask import Flask, request, render_template_string, redirect, send_from_directory
import json, os, subprocess, signal, sys, atexit, time

app = Flask(__name__)

CLIPS_FILE = "./processed_clips.json"
VIDEO_SCRIPT = "./pareidolia_no_ram.py"
VIDEO_FOLDER = "./processed_clips"
THUMB_FOLDER = "./thumbnails"
KILL_FILE = "./STOP_SERVER"  # Touch this file to stop the server
os.makedirs(THUMB_FOLDER, exist_ok=True)

video_process = None

def cleanup():
    """Clean up the video process on shutdown"""
    global video_process
    if video_process:
        print("\n🛑 Terminating video process...")
        try:
            video_process.terminate()
            video_process.wait(timeout=5)
            print("✓ Video process terminated cleanly")
        except subprocess.TimeoutExpired:
            print("⚠ Video process didn't terminate, killing forcefully...")
            video_process.kill()
            video_process.wait()
        except Exception as e:
            print(f"⚠ Error during cleanup: {e}")
        video_process = None

# Register cleanup to run on normal exit
atexit.register(cleanup)

def signal_handler(sig, frame):
    """Handle Ctrl+C and termination signals"""
    print(f"\n📡 Received signal {sig}")
    cleanup()
    print("👋 Flask app shutting down...")
    sys.exit(0)

# Register signal handlers for Ctrl+C and SIGTERM
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def get_clip_files():
    return [f for f in os.listdir(VIDEO_FOLDER) if f.endswith((".mp4", ".mov", ".avi"))]

def load_clips():
    try:
        with open(CLIPS_FILE) as f:
            return json.load(f)["clips"]
    except:
        return []

def save_clips(clips):
    with open(CLIPS_FILE, "w") as f:
        json.dump({"clips": clips}, f, indent=2)

def start_video_process():
    """Start the video process with proper process group handling"""
    global video_process
    
    # Validate JSON before starting
    try:
        clips = load_clips()
        if not clips:
            print("⚠ Warning: No clips in JSON - video process may not start correctly")
    except Exception as e:
        print(f"⚠ Warning: Error reading clips JSON: {e}")
    
    try:
        # Ensure video process inherits environment (especially DISPLAY)
        env = os.environ.copy()
        
        video_process = subprocess.Popen(
            ["python3", VIDEO_SCRIPT],
            preexec_fn=os.setsid,  # Create new process group for better control
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env  # Pass full environment including DISPLAY
        )
        print(f"✓ Video process started (PID: {video_process.pid})")
        
        # Monitor for immediate crashes
        try:
            return_code = video_process.wait(timeout=2)
            print(f"⚠ Video process exited immediately with code {return_code}")
            # Try to get error output
            stderr = video_process.stderr.read().decode() if video_process.stderr else ""
            if stderr:
                print(f"Error output: {stderr[:500]}")
            return None
        except subprocess.TimeoutExpired:
            # Good - it's still running
            return video_process
    except Exception as e:
        print(f"⚠ Failed to start video process: {e}")
        return None

def restart_video_process():
    """Restart the video process"""
    global video_process
    if video_process:
        print("🔄 Stopping existing video process...")
        try:
            # Use process group kill for more reliable termination
            os.killpg(os.getpgid(video_process.pid), signal.SIGTERM)
            video_process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            # Process already dead or didn't stop, force kill
            try:
                video_process.kill()
                video_process.wait()
            except:
                pass
        video_process = None
    
    print("🚀 Starting new video process...")
    return start_video_process()

def generate_thumbnail(clip):
    file_path = clip["file_path"]
    start = clip.get("start_sec", 0)
    end = clip.get("end_sec", start + 5 if start else 5)
    if end < 0:  # fallback if end_sec is -1
        end = start + 5
    midpoint = start + (end - start)/2
    name_safe = clip["name"].replace(" ", "_")
    thumb_path = os.path.join(THUMB_FOLDER, f"{name_safe}.png")

    # only generate if missing
    if not os.path.exists(thumb_path):
        subprocess.run([
            "ffmpeg",
            "-y",
            "-ss", str(midpoint),
            "-i", file_path,
            "-frames:v", "1",
            "-q:v", "2",
            "-vf", "scale=480:-1",  # scale width to 480px, keep aspect ratio
            thumb_path
        ], capture_output=True)  # Suppress ffmpeg output
    return thumb_path

@app.route('/thumbnails/<filename>')
def serve_thumbnail(filename):
    return send_from_directory(THUMB_FOLDER, filename)

@app.route("/shutdown", methods=["POST"])
def shutdown_server():
    """Shutdown endpoint to stop both Flask and video process"""
    print("\n🛑 Shutdown requested from web interface")
    cleanup()
    
    # Shutdown Flask
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        # Alternative method for newer Werkzeug versions
        import sys
        print("👋 Exiting...")
        sys.exit(0)
    func()
    return "Server shutting down..."

@app.route("/", methods=["GET", "POST"])
def index():
    clip_files = get_clip_files()
    clips = load_clips()

    if request.method == "POST":
        new_clips = []
        rows = int(request.form.get("rows", 0))
        for i in range(rows):
            name = request.form.get(f"name_{i}")
            if not name:
                continue
            
            # Find the original clip to preserve all fields
            original_clip = next((c for c in clips if c["name"] == name), None)
            if not original_clip:
                continue
            
            # Get form values
            channel = request.form.get(f"channel_{i}")
            note = request.form.get(f"note_{i}")
            desc = request.form.get(f"desc_{i}")
            
            # Start with original clip data to preserve all fields
            new_clip = original_clip.copy()
            
            # Update only the fields from the form
            if channel is not None:
                new_clip["midi_channel"] = int(channel)
            if note:
                new_clip["midi_note"] = note
            if desc:
                new_clip["comments"] = desc
            
            new_clips.append(new_clip)
        
        save_clips(new_clips)
        
        # Only restart if we actually saved clips
        if new_clips:
            print(f"✓ Saved {len(new_clips)} clips")
            restart_video_process()
        else:
            print("⚠ No clips to save - keeping video process running")
        
        return redirect("/")

    # generate thumbnails for all clips
    for clip in clips:
        clip["thumbnail"] = generate_thumbnail(clip)

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Video Clip Mapper</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; }
            table { border-collapse: collapse; width: 100%; }
            th, td { padding: 8px; text-align: left; border: 1px solid #ddd; }
            th { background-color: #4CAF50; color: white; }
            button { background-color: #4CAF50; color: white; padding: 10px 20px; 
                     border: none; cursor: pointer; font-size: 16px; margin-top: 10px; }
            button:hover { background-color: #45a049; }
            img { border: 1px solid #ddd; border-radius: 4px; }
        </style>
    </head>
    <body>
    <h2>🎵 MIDI Clip Mappings</h2>
    <form method="POST">
    <table>
        <tr><th>Thumbnail</th><th>Name</th><th>Channel</th><th>Note</th><th>Description</th></tr>
        {% for i, clip in clips %}
        <tr>
        <td><img src="/thumbnails/{{clip['thumbnail'].split('/')[-1]}}" width="160"></td>
        <td>{{clip['name']}}</td>
        <td>
            <select name="channel_{{i}}">
            <option value="-1" {% if clip.get('midi_channel') == -1 or clip.get('midi_channel')=='-1' %}selected{% endif %}>-1</option>
            {% for ch in range(1, 17) %}
            <option value="{{ch}}" {% if clip.get('midi_channel') == ch or clip.get('midi_channel')==ch|string %}selected{% endif %}>{{ch}}</option>
            {% endfor %}
            </select>
        </td>
        <td><input type="text" name="note_{{i}}" value="{{clip.get('midi_note','')}}"></td>
        <td><input type="text" name="desc_{{i}}" value="{{clip.get('comments','')}}"></td>
        <input type="hidden" name="name_{{i}}" value="{{clip['name']}}">
        </tr>
        {% endfor %}
    </table>
    <input type="hidden" name="rows" value="{{clips|length}}">
    <button type="submit">💾 Save & Restart</button>
    </form>
    <br>
    <form method="POST" action="/shutdown" style="display: inline;">
    <button type="submit" style="background-color: #f44336;" onclick="return confirm('Stop both Flask and video process?')">🛑 Shutdown</button>
    </form>
    </body>
    </html>
    """
    return render_template_string(html, clips=list(enumerate(clips)), clip_files=clip_files)

if __name__ == "__main__":
    print("=" * 50)
    print("🎬 Video Clip MIDI Mapper")
    print("=" * 50)
    
    # Remove kill file if it exists
    if os.path.exists(KILL_FILE):
        os.remove(KILL_FILE)
    
    # Start the video process
    start_video_process()
    
    print("\n📱 Web interface starting...")
    print("💡 Get your Pi's IP with: hostname -I")
    print("🌐 Access at: http://<your-pi-ip>:5000")
    print("\n⚠️  Press Ctrl+C to stop both Flask and video process")
    print("⚠️  Or create file './STOP_SERVER' to trigger shutdown")
    print("⚠️  Or use the 🛑 Shutdown button in web interface")
    print("=" * 50 + "\n")
    
    # Check for kill file periodically
    import threading
    def check_kill_file():
        while True:
            if os.path.exists(KILL_FILE):
                print("\n🛑 Kill file detected!")
                cleanup()
                os._exit(0)
            time.sleep(1)
    
    kill_thread = threading.Thread(target=check_kill_file, daemon=True)
    kill_thread.start()
    
    try:
        app.run(host="0.0.0.0", port=5000)
    except KeyboardInterrupt:
        print("\n⌨️  Keyboard interrupt received")
    finally:
        cleanup()
