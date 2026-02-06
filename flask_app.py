from flask import Flask, request, render_template_string, redirect
import json, os, subprocess, signal

app = Flask(__name__)

CLIPS_FILE = "./processed_clips.json"
VIDEO_SCRIPT = "./pareidolia_no_ram.py"
VIDEO_FOLDER = "./videos"
THUMB_FOLDER = "./thumbnails"
os.makedirs(THUMB_FOLDER, exist_ok=True)

video_process = None

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
        ])
    return thumb_path

@app.route("/", methods=["GET", "POST"])
def index():
    global video_process
    clip_files = get_clip_files()
    clips = load_clips()

    if request.method == "POST":
        new_clips = []
        rows = int(request.form.get("rows", 0))
        for i in range(rows):
            channel = request.form.get(f"channel_{i}")
            note = request.form.get(f"note_{i}")
            filename = request.form.get(f"clip_{i}")
            name = request.form.get(f"name_{i}")
            file_path = next((c["file_path"] for c in clips if c["name"] == name), "")
            if channel is not None and note and filename:
                new_clips.append({
                    "name": name,
                    "midi_channel": int(channel),
                    "midi_note": note,
                    "file_path": file_path
                })
        save_clips(new_clips)

        # restart video script
        if video_process:
            os.kill(video_process.pid, signal.SIGTERM)
        video_process = subprocess.Popen(["python3", VIDEO_SCRIPT])
        return redirect("/")

    # generate thumbnails for all clips
    for clip in clips:
        clip["thumbnail"] = generate_thumbnail(clip)

    html = """
    <h2>Clip Mappings</h2>
    <form method="POST">
      <table border="1" cellpadding="5">
        <tr><th>Thumbnail</th><th>Name</th><th>Channel</th><th>Note</th><th>Clip</th></tr>
        {% for i, clip in clips %}
        <tr>
          <td><img src="{{clip['thumbnail']}}" width="160"></td>
          <td>{{clip['name']}}</td>
          <td>
            <select name="channel_{{i}}">
              <option value="-1" {% if clip.get('midi_channel') == -1 or clip.get('midi_channel')=='-1' %}selected{% endif %}>-1</option>
              {% for ch in range(1, 17) %}
              <option value="{{ch}}" {% if clip.get('midi_channel') == ch or clip.get('midi_channel')==str(ch) %}selected{% endif %}>{{ch}}</option>
              {% endfor %}
            </select>
          </td>
          <td><input type="text" name="note_{{i}}" value="{{clip.get('midi_note','')}}"></td>
          <td>
            <select name="clip_{{i}}">
              {% for f in clip_files %}
              <option value="{{f}}" {% if f==clip['file_path'].split('/')[-1] %}selected{% endif %}>{{f}}</option>
              {% endfor %}
            </select>
          </td>
          <input type="hidden" name="name_{{i}}" value="{{clip['name']}}">
        </tr>
        {% endfor %}
      </table>
      <input type="hidden" name="rows" value="{{clips|length}}">
      <button type="submit">Save & Restart</button>
    </form>
    """
    return render_template_string(html, clips=list(enumerate(clips)), clip_files=clip_files)

if __name__ == "__main__":
    video_process = subprocess.Popen(["python3", VIDEO_SCRIPT])
    print("Get IP with hostname -I")
    app.run(host="0.0.0.0", port=5000)
