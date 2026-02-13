from flask import Flask, request, render_template_string, redirect, send_from_directory, jsonify
import json, os, subprocess, signal, sys, atexit, time, psutil

app = Flask(__name__)

CLIPS_FILE = "./processed_clips.json"
VIDEO_SCRIPT = "./pareidolia_with_resume.py"
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

def is_video_process_running():
    """Check if the video process is actually running"""
    global video_process
    if video_process is None:
        return False
    
    # Check if process is still alive
    try:
        # poll() returns None if process is still running
        if video_process.poll() is None:
            return True
        else:
            # Process has terminated
            video_process = None
            return False
    except:
        return False

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

@app.route('/api/status')
def api_status():
    """API endpoint to check if video process is running"""
    return jsonify({
        'running': is_video_process_running(),
        'pid': video_process.pid if video_process and is_video_process_running() else None
    })

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
            restart_on_play = request.form.get(f"restart_{i}") == "on"
            
            # Start with original clip data to preserve all fields
            new_clip = original_clip.copy()
            
            # Update only the fields from the form
            if channel is not None:
                new_clip["midi_channel"] = int(channel)
            if note:
                new_clip["midi_note"] = note
            if desc:
                new_clip["comments"] = desc
            
            # Update restart_on_play
            new_clip["restart_on_play"] = restart_on_play
            
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

    # Get MIDI channel colors for visual coding
    channel_colors = {
        1: '#FF6B6B', 2: '#4ECDC4', 3: '#45B7D1', 4: '#FFA07A',
        5: '#98D8C8', 6: '#F7DC6F', 7: '#BB8FCE', 8: '#85C1E2',
        9: '#F8B739', 10: '#52B788', 11: '#E07A5F', 12: '#81B29A',
        13: '#F4A261', 14: '#E76F51', 15: '#8AB17D', 16: '#C77DFF',
        -1: '#95a5a6'
    }

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Video Clip Mapper</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <style>
            * {
                box-sizing: border-box;
                -webkit-tap-highlight-color: transparent;
            }
            
            body { 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
                margin: 0;
                padding: 0;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
            }
            
            .header {
                background: rgba(255, 255, 255, 0.95);
                padding: 15px 20px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                position: sticky;
                top: 0;
                z-index: 100;
            }
            
            .header-content {
                display: flex;
                justify-content: space-between;
                align-items: center;
                max-width: 800px;
                margin: 0 auto;
            }
            
            .header h1 {
                margin: 0;
                font-size: 20px;
                color: #2c3e50;
                display: flex;
                align-items: center;
                gap: 10px;
            }
            
            .status-indicator {
                width: 12px;
                height: 12px;
                border-radius: 50%;
                background: #95a5a6;
                animation: pulse 2s ease-in-out infinite;
            }
            
            .status-indicator.running {
                background: #2ecc71;
            }
            
            .status-indicator.stopped {
                background: #e74c3c;
                animation: none;
            }
            
            @keyframes pulse {
                0%, 100% { opacity: 1; }
                50% { opacity: 0.5; }
            }
            
            .container {
                max-width: 800px;
                margin: 0 auto;
                padding: 20px;
            }
            
            .clip-card {
                background: white;
                border-radius: 12px;
                margin-bottom: 15px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                overflow: hidden;
                transition: transform 0.2s, box-shadow 0.2s;
            }
            
            .clip-card:active {
                transform: scale(0.98);
            }
            
            .clip-thumbnail {
                width: 100%;
                height: 180px;
                object-fit: cover;
                background: #f0f0f0;
            }
            
            .clip-info {
                padding: 15px;
            }
            
            .clip-name {
                font-size: 18px;
                font-weight: 600;
                color: #2c3e50;
                margin: 0 0 10px 0;
            }
            
            .clip-controls {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 10px;
                margin-bottom: 10px;
            }
            
            .control-group {
                display: flex;
                flex-direction: column;
            }
            
            .control-label {
                font-size: 12px;
                color: #7f8c8d;
                margin-bottom: 5px;
                font-weight: 500;
            }
            
            select, input[type="text"] {
                padding: 10px;
                border: 2px solid #ecf0f1;
                border-radius: 8px;
                font-size: 16px;
                background: white;
                transition: border-color 0.2s;
            }
            
            select:focus, input[type="text"]:focus {
                outline: none;
                border-color: #667eea;
            }
            
            .channel-badge {
                display: inline-block;
                padding: 4px 8px;
                border-radius: 6px;
                font-size: 12px;
                font-weight: 600;
                color: white;
                margin-left: 5px;
            }
            
            .description-input {
                width: 100%;
                margin-top: 10px;
            }
            
            .checkbox-group {
                display: flex;
                align-items: center;
                gap: 8px;
                margin-top: 10px;
                padding: 10px;
                background: #f8f9fa;
                border-radius: 8px;
            }
            
            .checkbox-group input[type="checkbox"] {
                width: 22px;
                height: 22px;
                cursor: pointer;
            }
            
            .checkbox-group label {
                font-size: 14px;
                color: #2c3e50;
                cursor: pointer;
                user-select: none;
            }
            
            .action-buttons {
                display: flex;
                gap: 10px;
                margin-top: 20px;
                position: sticky;
                bottom: 20px;
            }
            
            button {
                flex: 1;
                padding: 16px;
                border: none;
                border-radius: 12px;
                font-size: 16px;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.2s;
                box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            }
            
            button:active {
                transform: translateY(2px);
                box-shadow: 0 2px 6px rgba(0,0,0,0.15);
            }
            
            .btn-save {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
            }
            
            .btn-shutdown {
                background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
                color: white;
            }
            
            .stats {
                background: rgba(255, 255, 255, 0.95);
                padding: 15px;
                border-radius: 12px;
                margin-bottom: 20px;
                display: flex;
                justify-content: space-around;
                text-align: center;
            }
            
            .stat-item {
                flex: 1;
            }
            
            .stat-value {
                font-size: 24px;
                font-weight: 700;
                color: #667eea;
            }
            
            .stat-label {
                font-size: 12px;
                color: #7f8c8d;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }
            
            @media (prefers-color-scheme: dark) {
                body {
                    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
                }
                
                .header {
                    background: rgba(26, 26, 46, 0.95);
                }
                
                .header h1 {
                    color: #ecf0f1;
                }
                
                .clip-card, .stats {
                    background: #1a1a2e;
                    color: #ecf0f1;
                }
                
                .clip-name {
                    color: #ecf0f1;
                }
                
                select, input[type="text"] {
                    background: #16213e;
                    color: #ecf0f1;
                    border-color: #2c3e50;
                }
                
                .checkbox-group {
                    background: #16213e;
                }
                
                .checkbox-group label {
                    color: #ecf0f1;
                }
            }
        </style>
    </head>
    <body>
        <div class="header">
            <div class="header-content">
                <h1>
                    🎵 Clip Mapper
                    <span class="status-indicator" id="statusIndicator"></span>
                </h1>
            </div>
        </div>
        
        <div class="container">
            <div class="stats">
                <div class="stat-item">
                    <div class="stat-value">{{ clips|length }}</div>
                    <div class="stat-label">Clips</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value" id="channelCount">0</div>
                    <div class="stat-label">Channels</div>
                </div>
            </div>
            
            <form method="POST" id="mainForm">
                {% for i, clip in clips %}
                <div class="clip-card">
                    <img src="/thumbnails/{{clip['thumbnail'].split('/')[-1]}}" 
                         alt="{{clip['name']}}" 
                         class="clip-thumbnail">
                    
                    <div class="clip-info">
                        <h3 class="clip-name">
                            {{clip['name']}}
                            <span class="channel-badge" 
                                  style="background-color: {{ channel_colors.get(clip.get('midi_channel', -1), '#95a5a6') }}">
                                Ch {{clip.get('midi_channel', -1)}}
                            </span>
                        </h3>
                        
                        <div class="clip-controls">
                            <div class="control-group">
                                <label class="control-label">MIDI Channel</label>
                                <select name="channel_{{i}}" onchange="updateChannelBadge(this, {{i}})">
                                    <option value="-1" {% if clip.get('midi_channel') == -1 or clip.get('midi_channel')=='-1' %}selected{% endif %}>Any (-1)</option>
                                    {% for ch in range(1, 17) %}
                                    <option value="{{ch}}" {% if clip.get('midi_channel') == ch or clip.get('midi_channel')==ch|string %}selected{% endif %}>{{ch}}</option>
                                    {% endfor %}
                                </select>
                            </div>
                            
                            <div class="control-group">
                                <label class="control-label">MIDI Note</label>
                                <input type="text" 
                                       name="note_{{i}}" 
                                       value="{{clip.get('midi_note','')}}"
                                       placeholder="e.g. C4 or 60">
                            </div>
                        </div>
                        
                        <div class="control-group">
                            <label class="control-label">Description</label>
                            <input type="text" 
                                   name="desc_{{i}}" 
                                   class="description-input"
                                   value="{{clip.get('comments','')}}"
                                   placeholder="Optional notes...">
                        </div>
                        
                        <div class="checkbox-group">
                            <input type="checkbox" 
                                   name="restart_{{i}}" 
                                   id="restart_{{i}}"
                                   {% if clip.get('restart_on_play', False) %}checked{% endif %}>
                            <label for="restart_{{i}}">🔄 Always restart from beginning</label>
                        </div>
                        
                        <input type="hidden" name="name_{{i}}" value="{{clip['name']}}">
                    </div>
                </div>
                {% endfor %}
                
                <input type="hidden" name="rows" value="{{clips|length}}">
                
                <div class="action-buttons">
                    <button type="submit" class="btn-save">💾 Save & Restart</button>
                    <button type="button" class="btn-shutdown" onclick="confirmShutdown()">🛑 Shutdown</button>
                </div>
            </form>
        </div>
        
        <script>
            // Check video process status
            function updateStatus() {
                fetch('/api/status')
                    .then(r => r.json())
                    .then(data => {
                        const indicator = document.getElementById('statusIndicator');
                        if (data.running) {
                            indicator.className = 'status-indicator running';
                            indicator.title = 'Video process running (PID: ' + data.pid + ')';
                        } else {
                            indicator.className = 'status-indicator stopped';
                            indicator.title = 'Video process stopped';
                        }
                    })
                    .catch(() => {
                        document.getElementById('statusIndicator').className = 'status-indicator';
                    });
            }
            
            // Update status every 3 seconds
            updateStatus();
            setInterval(updateStatus, 3000);
            
            // Count unique channels
            function countChannels() {
                const selects = document.querySelectorAll('select[name^="channel_"]');
                const channels = new Set();
                selects.forEach(s => {
                    const val = parseInt(s.value);
                    if (val > 0) channels.add(val);
                });
                document.getElementById('channelCount').textContent = channels.size;
            }
            countChannels();
            
            // Update channel badge color dynamically
            const channelColors = {{ channel_colors|tojson }};
            
            function updateChannelBadge(select, index) {
                const card = select.closest('.clip-card');
                const badge = card.querySelector('.channel-badge');
                const channel = parseInt(select.value);
                badge.textContent = 'Ch ' + channel;
                badge.style.backgroundColor = channelColors[channel] || '#95a5a6';
                countChannels();
            }
            
            function confirmShutdown() {
                if (confirm('⚠️ Stop both Flask and video process?')) {
                    fetch('/shutdown', { method: 'POST' })
                        .then(() => {
                            alert('✓ Server shutting down...');
                        });
                }
            }
            
            // Smooth scroll for sticky buttons
            window.addEventListener('scroll', () => {
                const buttons = document.querySelector('.action-buttons');
                if (window.scrollY > 100) {
                    buttons.style.boxShadow = '0 -4px 20px rgba(0,0,0,0.2)';
                } else {
                    buttons.style.boxShadow = '0 4px 12px rgba(0,0,0,0.15)';
                }
            });
        </script>
    </body>
    </html>
    """
    return render_template_string(html, clips=list(enumerate(clips)), clip_files=clip_files, channel_colors=channel_colors)

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