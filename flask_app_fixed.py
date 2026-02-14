from flask import Flask, request, render_template_string, redirect, send_from_directory, jsonify
import json, os, subprocess, signal, sys, atexit, time, psutil

app = Flask(__name__)

CLIPS_FILE = "./processed_clips.json"
VIDEO_SCRIPT = "./pareidolia_with_no_ram.py"
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
            
            # Collect up to 4 notes
            notes = []
            for note_idx in range(4):
                note = request.form.get(f"note_{i}_{note_idx}")
                if note and note != "None":
                    notes.append(note)
            
            # Store as comma-separated string (for backward compatibility)
            # or as array if you prefer to change the JSON structure
            notes_str = ",".join(notes) if notes else ""
            
            desc = request.form.get(f"desc_{i}")
            restart = request.form.get(f"restart_{i}") == "on"
            
            # Update clip with new values
            original_clip.update({
                "midi_channel": int(channel),
                "midi_note": notes_str,  # Store all notes
                "comments": desc,
                "restart_on_play": restart
            })
            new_clips.append(original_clip)
        
        save_clips(new_clips)
        restart_video_process()
        return redirect("/")

    # Generate thumbnails
    for clip in clips:
        clip["thumbnail"] = generate_thumbnail(clip)

    # Channel colors
    channel_colors = {
        1: '#e74c3c', 2: '#3498db', 3: '#2ecc71', 4: '#f39c12',
        5: '#9b59b6', 6: '#1abc9c', 7: '#e67e22', 8: '#34495e',
        9: '#c0392b', 10: '#2980b9', 11: '#27ae60', 12: '#f1c40f',
        13: '#8e44ad', 14: '#16a085', 15: '#d35400', 16: '#7f8c8d',
        -1: '#95a5a6'
    }

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Clip Mapper</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * {
                margin: 0;
                padding: 0;
                box-sizing: border-box;
            }
            
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
                padding-bottom: 100px;
            }
            
            .header {
                background: rgba(255, 255, 255, 0.95);
                backdrop-filter: blur(10px);
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
                position: sticky;
                top: 0;
                z-index: 100;
            }
            
            .header-content {
                max-width: 1200px;
                margin: 0 auto;
                padding: 20px;
            }
            
            h1 {
                color: #2c3e50;
                font-size: 28px;
                font-weight: 700;
                display: flex;
                align-items: center;
                gap: 12px;
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
                max-width: 1200px;
                margin: 0 auto;
                padding: 30px 20px;
            }
            
            .scale-selector {
                background: white;
                border-radius: 12px;
                padding: 24px;
                margin-bottom: 30px;
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
            }
            
            .scale-selector h2 {
                color: #2c3e50;
                font-size: 20px;
                margin-bottom: 16px;
                display: flex;
                align-items: center;
                gap: 8px;
            }
            
            .scale-controls {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 20px;
            }
            
            .scale-control-group {
                display: flex;
                flex-direction: column;
                gap: 8px;
            }
            
            .scale-control-group label {
                font-weight: 600;
                color: #34495e;
                font-size: 14px;
            }
            
            .scale-control-group select {
                padding: 12px;
                border: 2px solid #ecf0f1;
                border-radius: 8px;
                font-size: 16px;
                background: white;
                transition: all 0.3s;
            }
            
            .scale-control-group select:focus {
                outline: none;
                border-color: #667eea;
                box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
            }
            
            .stats {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
                gap: 16px;
                margin-bottom: 30px;
            }
            
            .stat-item {
                background: white;
                border-radius: 12px;
                padding: 20px;
                text-align: center;
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
            }
            
            .stat-value {
                font-size: 32px;
                font-weight: 700;
                color: #667eea;
            }
            
            .stat-label {
                font-size: 14px;
                color: #7f8c8d;
                margin-top: 4px;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }
            
            .clip-card {
                background: white;
                border-radius: 12px;
                margin-bottom: 20px;
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
                overflow: hidden;
                display: grid;
                grid-template-columns: 200px 1fr;
                transition: transform 0.3s, box-shadow 0.3s;
            }
            
            .clip-card:hover {
                transform: translateY(-2px);
                box-shadow: 0 8px 30px rgba(0, 0, 0, 0.15);
            }
            
            .clip-thumbnail {
                width: 200px;
                height: 200px;
                object-fit: cover;
            }
            
            .clip-info {
                padding: 20px;
                display: flex;
                flex-direction: column;
                gap: 16px;
            }
            
            .clip-name {
                font-size: 20px;
                color: #2c3e50;
                font-weight: 600;
                display: flex;
                align-items: center;
                gap: 10px;
            }
            
            .channel-badge {
                font-size: 12px;
                padding: 4px 12px;
                border-radius: 20px;
                color: white;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }
            
            .clip-controls {
                display: grid;
                grid-template-columns: 150px 1fr;
                gap: 16px;
            }
            
            .control-group {
                display: flex;
                flex-direction: column;
                gap: 6px;
            }
            
            .control-label {
                font-size: 12px;
                font-weight: 600;
                color: #7f8c8d;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }
            
            .notes-grid {
                display: grid;
                grid-template-columns: repeat(4, 1fr);
                gap: 8px;
            }
            
            select, input[type="text"] {
                padding: 10px;
                border: 2px solid #ecf0f1;
                border-radius: 8px;
                font-size: 15px;
                transition: all 0.3s;
                background: white;
            }
            
            select:focus, input[type="text"]:focus {
                outline: none;
                border-color: #667eea;
                box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
            }
            
            .description-input {
                width: 100%;
            }
            
            .checkbox-group {
                display: flex;
                align-items: center;
                gap: 8px;
                padding: 12px;
                background: #f8f9fa;
                border-radius: 8px;
            }
            
            .checkbox-group input[type="checkbox"] {
                width: 20px;
                height: 20px;
                cursor: pointer;
            }
            
            .checkbox-group label {
                cursor: pointer;
                font-size: 14px;
                color: #2c3e50;
                user-select: none;
            }
            
            .action-buttons {
                position: fixed;
                bottom: 0;
                left: 0;
                right: 0;
                background: white;
                padding: 20px;
                box-shadow: 0 -4px 20px rgba(0, 0, 0, 0.15);
                display: flex;
                gap: 12px;
                justify-content: center;
                z-index: 99;
            }
            
            button {
                padding: 14px 32px;
                border: none;
                border-radius: 8px;
                font-size: 16px;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.3s;
                display: flex;
                align-items: center;
                gap: 8px;
            }
            
            .btn-save {
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
            }
            
            .btn-save:hover {
                transform: translateY(-2px);
                box-shadow: 0 6px 20px rgba(102, 126, 234, 0.5);
            }
            
            .btn-shutdown {
                background: #e74c3c;
                color: white;
                box-shadow: 0 4px 15px rgba(231, 76, 60, 0.4);
            }
            
            .btn-shutdown:hover {
                background: #c0392b;
                transform: translateY(-2px);
                box-shadow: 0 6px 20px rgba(231, 76, 60, 0.5);
            }
            
            @media (max-width: 768px) {
                .clip-card {
                    grid-template-columns: 1fr;
                }
                
                .clip-thumbnail {
                    width: 100%;
                    height: 200px;
                }
                
                .clip-controls {
                    grid-template-columns: 1fr;
                }
                
                .notes-grid {
                    grid-template-columns: repeat(2, 1fr);
                }
                
                .scale-controls {
                    grid-template-columns: 1fr;
                }
                
                .action-buttons {
                    flex-direction: column;
                }
                
                button {
                    width: 100%;
                    justify-content: center;
                }
            }
            
            @media (prefers-color-scheme: dark) {
                body {
                    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
                }
                
                .header, .stat-item, .clip-card, .action-buttons, .scale-selector {
                    background: #0f3460;
                }
                
                h1, .clip-name, .scale-selector h2 {
                    color: #ecf0f1;
                }
                
                .stat-label, .control-label, .scale-control-group label {
                    color: #bdc3c7;
                }
                
                select, input[type="text"] {
                    background: #16213e;
                    border-color: #34495e;
                    color: #ecf0f1;
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
            <div class="scale-selector">
                <h2>🎼 Scale & Key Settings</h2>
                <div class="scale-controls">
                    <div class="scale-control-group">
                        <label for="keySelect">Root Note</label>
                        <select id="keySelect" onchange="updateAvailableNotes()">
                            <option value="C">C</option>
                            <option value="C#">C# / Db</option>
                            <option value="D">D</option>
                            <option value="D#">D# / Eb</option>
                            <option value="E">E</option>
                            <option value="F">F</option>
                            <option value="F#">F# / Gb</option>
                            <option value="G">G</option>
                            <option value="G#">G# / Ab</option>
                            <option value="A">A</option>
                            <option value="A#">A# / Bb</option>
                            <option value="B">B</option>
                        </select>
                    </div>
                    <div class="scale-control-group">
                        <label for="scaleSelect">Scale Type</label>
                        <select id="scaleSelect" onchange="updateAvailableNotes()">
                            <option value="chromatic">Chromatic (All Notes)</option>
                            <option value="major">Major</option>
                            <option value="minor">Natural Minor</option>
                            <option value="harmonic_minor">Harmonic Minor</option>
                            <option value="melodic_minor">Melodic Minor</option>
                            <option value="dorian">Dorian</option>
                            <option value="phrygian">Phrygian</option>
                            <option value="lydian">Lydian</option>
                            <option value="mixolydian">Mixolydian</option>
                            <option value="pentatonic_major">Pentatonic Major</option>
                            <option value="pentatonic_minor">Pentatonic Minor</option>
                            <option value="blues">Blues Scale</option>
                            <option value="whole_tone">Whole Tone</option>
                        </select>
                    </div>
                </div>
            </div>
            
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
                                <label class="control-label">MIDI Notes (up to 4)</label>
                                <div class="notes-grid">
                                    {% set current_notes = clip.get('midi_note', '').split(',') if clip.get('midi_note') else [] %}
                                    {% for note_idx in range(4) %}
                                    <select name="note_{{i}}_{{note_idx}}" class="note-select">
                                        <option value="None">None</option>
                                    </select>
                                    {% endfor %}
                                </div>
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
                        <input type="hidden" class="current-notes" value="{{clip.get('midi_note', '')}}">
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
            // Music theory: scale intervals (semitones from root)
            const scaleIntervals = {
                chromatic: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
                major: [0, 2, 4, 5, 7, 9, 11],
                minor: [0, 2, 3, 5, 7, 8, 10],
                harmonic_minor: [0, 2, 3, 5, 7, 8, 11],
                melodic_minor: [0, 2, 3, 5, 7, 9, 11],
                dorian: [0, 2, 3, 5, 7, 9, 10],
                phrygian: [0, 1, 3, 5, 7, 8, 10],
                lydian: [0, 2, 4, 6, 7, 9, 11],
                mixolydian: [0, 2, 4, 5, 7, 9, 10],
                pentatonic_major: [0, 2, 4, 7, 9],
                pentatonic_minor: [0, 3, 5, 7, 10],
                blues: [0, 3, 5, 6, 7, 10],
                whole_tone: [0, 2, 4, 6, 8, 10]
            };
            
            // All chromatic notes
            const chromaticNotes = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
            
            // All MIDI note names from C0 to B8
            function getAllMidiNotes() {
                const notes = [];
                for (let octave = 0; octave <= 8; octave++) {
                    for (let note of chromaticNotes) {
                        notes.push(note + octave);
                    }
                }
                return notes;
            }
            
            // Get notes in a specific scale
            function getScaleNotes(root, scaleType) {
                const rootIndex = chromaticNotes.indexOf(root);
                const intervals = scaleIntervals[scaleType];
                const scaleNotes = [];
                
                for (let octave = 0; octave <= 8; octave++) {
                    for (let interval of intervals) {
                        const noteIndex = (rootIndex + interval) % 12;
                        scaleNotes.push(chromaticNotes[noteIndex] + octave);
                    }
                }
                
                return scaleNotes;
            }
            
            // Update all note dropdowns based on selected scale
            function updateAvailableNotes() {
                const root = document.getElementById('keySelect').value;
                const scaleType = document.getElementById('scaleSelect').value;
                const availableNotes = getScaleNotes(root, scaleType);
                
                // Update all note select dropdowns
                const noteSelects = document.querySelectorAll('.note-select');
                noteSelects.forEach(select => {
                    const currentValue = select.value;
                    
                    // Clear existing options except None
                    select.innerHTML = '<option value="None">None</option>';
                    
                    // Add available notes
                    availableNotes.forEach(note => {
                        const option = document.createElement('option');
                        option.value = note;
                        option.textContent = note;
                        select.appendChild(option);
                    });
                    
                    // Restore previous selection if still valid
                    if (availableNotes.includes(currentValue) || currentValue === 'None') {
                        select.value = currentValue;
                    }
                });
            }
            
            // Initialize note dropdowns with saved values
            function initializeNoteSelects() {
                const clipCards = document.querySelectorAll('.clip-card');
                clipCards.forEach(card => {
                    const currentNotesInput = card.querySelector('.current-notes');
                    const noteSelects = card.querySelectorAll('.note-select');
                    
                    if (currentNotesInput) {
                        const savedNotes = currentNotesInput.value.split(',').filter(n => n.trim());
                        savedNotes.forEach((note, idx) => {
                            if (idx < noteSelects.length && note.trim()) {
                                noteSelects[idx].value = note.trim();
                            }
                        });
                    }
                });
            }
            
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
            
            // Initialize on page load
            updateAvailableNotes();
            initializeNoteSelects();
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