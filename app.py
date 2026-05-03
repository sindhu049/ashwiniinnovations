import os
import uuid
import msgpack
import tempfile
from functools import wraps
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

app = Flask(__name__)
# Support large video uploads (500MB limit)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
# Secure secret key from environment or a robust default
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "ar_secure_master_778899")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
ADMIN_TOKEN    = os.environ.get("ADMIN_TOKEN", "default_secret_token")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
BUCKET_NAME  = "ar_assets"

# Dynamic Admin Route defined by Token
ADMIN_BASE_PATH = f"/admin-{ADMIN_TOKEN}"

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Authentication Decorator ──────────────────
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated_function

def load_mapping():
    if not supabase:
        return []
    try:
        res = supabase.table("ar_mappings").select("*").order("target_index").execute()
        return res.data
    except Exception as e:
        print("Supabase load error:", e)
        return []

def get_public_url(path):
    if not supabase:
        return f"/static/{path}"
    return supabase.storage.from_(BUCKET_NAME).get_public_url(path)


# ── / → Landing page (Public) ────────────────
@app.route("/")
def home():
    mapping = load_mapping()
    mind_ready = len(mapping) > 0
    return render_template(
        "index.html", 
        mapping=mapping, 
        mind_ready=mind_ready,
        admin_path=ADMIN_BASE_PATH
    )

# ── /login → Admin Login Handler ─────────────
@app.route("/login", methods=["GET", "POST"])
def admin_login():
    # Force fresh login every time
    if request.method == "GET":
        session.pop("admin_logged_in", None)
        
    if request.method == "POST":
        u = request.form.get("username")
        p = request.form.get("password")
        if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            session.permanent = True
            return redirect(ADMIN_BASE_PATH)
        return render_template("login.html", error="Invalid credentials")
    return render_template("login.html")


# ── /logout ──────────────────────────────────
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ── Dynamic /admin-<token> ────────────────────
@app.route(ADMIN_BASE_PATH, methods=["GET", "POST"])
@login_required
def admin():
    if not supabase:
        return "Supabase credentials not found.", 500

    mapping = load_mapping()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "upload_pair":
            image_file = request.files.get("image")
            video_file = request.files.get("video")
            mind_file  = request.files.get("mind_file")

            if not image_file or not video_file or not mind_file:
                return "Missing files.", 400

            new_mind_bytes = mind_file.read()
            try:
                new_data = msgpack.unpackb(new_mind_bytes)
            except Exception as e:
                return f"Failed to parse: {str(e)}", 400

            # 1. Download master targets.mind
            master_data = None
            try:
                res = supabase.storage.from_(BUCKET_NAME).download("targets/targets.mind")
                if res: master_data = msgpack.unpackb(res)
            except Exception: pass
            
            if not master_data or "dataList" not in master_data:
                master_data = {"v": 2, "dataList": []}

            # 2. Append new target
            if "dataList" in new_data and len(new_data["dataList"]) > 0:
                master_data["dataList"].append(new_data["dataList"][0])

            # 3. Upload merged master
            merged_bytes = msgpack.packb(master_data)
            supabase.storage.from_(BUCKET_NAME).upload(
                "targets/targets.mind", 
                merged_bytes, 
                {"upsert": "true", "content-type": "application/octet-stream"}
            )

            # 4. Upload video using a temporary file
            v_ext = os.path.splitext(secure_filename(video_file.filename))[1].lower() or ".mp4"
            vid_name = f"videos/vid_{uuid.uuid4().hex[:8]}{v_ext}"
            
            # Use a temporary file path
            temp_vid_path = os.path.join(tempfile.gettempdir(), f"vid_{uuid.uuid4().hex}.mp4")
            try:
                video_file.save(temp_vid_path)
                supabase.storage.from_(BUCKET_NAME).upload(
                    vid_name,
                    temp_vid_path,
                    {"upsert": "true", "content-type": video_file.content_type}
                )
            finally:
                if os.path.exists(temp_vid_path):
                    os.unlink(temp_vid_path)

            # 5. Upload image using a temporary file
            i_ext = os.path.splitext(secure_filename(image_file.filename))[1].lower() or ".jpg"
            img_name = f"images/img_{uuid.uuid4().hex[:8]}{i_ext}"
            
            temp_img_path = os.path.join(tempfile.gettempdir(), f"img_{uuid.uuid4().hex}.jpg")
            try:
                image_file.save(temp_img_path)
                supabase.storage.from_(BUCKET_NAME).upload(
                    img_name,
                    temp_img_path,
                    {"upsert": "true", "content-type": image_file.content_type}
                )
            finally:
                if os.path.exists(temp_img_path):
                    os.unlink(temp_img_path)

            # 6. Save mapping
            target_index = len(master_data["dataList"]) - 1
            supabase.table("ar_mappings").insert({
                "mind_key": f"target_{target_index}.mind",
                "image_url": get_public_url(img_name),
                "video_url": get_public_url(vid_name),
                "target_index": target_index
            }).execute()

            return jsonify({"status": "success"})

    mind_file_exists = len(mapping) > 0
    return render_template("admin.html", mapping=mapping, mind_file_exists=mind_file_exists, admin_path=ADMIN_BASE_PATH)


# ── Dynamic /admin-<token>/delete/<idx> ───────
@app.route(f"{ADMIN_BASE_PATH}/delete/<int:idx>", methods=["POST"])
@login_required
def delete_entry(idx):
    if not supabase: return redirect(ADMIN_BASE_PATH)
    mapping = load_mapping()
    for m in mapping:
        if m.get("target_index") == idx:
            supabase.table("ar_mappings").delete().eq("id", m["id"]).execute()
            break
    return redirect(ADMIN_BASE_PATH)


# ── /api/mapping (Public) ─────────────────────
@app.route("/api/mapping")
def api_mapping():
    return jsonify(load_mapping())


# ── /api/rebuild-targets → Returns image list for recompilation ───
@app.route("/api/rebuild-targets")
@login_required
def api_rebuild_targets():
    """Returns all current target image URLs so admin panel can recompile .mind"""
    mapping = load_mapping()
    return jsonify({
        "targets": [
            {"index": i, "image_url": m["image_url"], "video_url": m["video_url"], "id": m["id"]}
            for i, m in enumerate(mapping)
        ]
    })


# ── /api/commit-rebuild → Accepts recompiled .mind + updates DB indices ──
@app.route("/api/commit-rebuild", methods=["POST"])
@login_required
def api_commit_rebuild():
    """Receives freshly compiled .mind file and re-indexes all DB records"""
    if not supabase:
        return jsonify({"error": "Supabase not configured"}), 500

    mind_file = request.files.get("mind_file")
    if not mind_file:
        return jsonify({"error": "No mind_file provided"}), 400

    try:
        # Upload the fresh .mind file (overwrites old bloated one)
        mind_bytes = mind_file.read()
        supabase.storage.from_(BUCKET_NAME).upload(
            "targets/targets.mind",
            mind_bytes,
            {"upsert": "true", "content-type": "application/octet-stream"}
        )

        # Re-index all mappings to sequential 0, 1, 2, ...
        mapping = load_mapping()
        for new_index, entry in enumerate(mapping):
            supabase.table("ar_mappings").update(
                {"target_index": new_index, "mind_key": f"target_{new_index}.mind"}
            ).eq("id", entry["id"]).execute()

        return jsonify({"status": "success", "count": len(mapping)})
    except Exception as e:
        print("Rebuild error:", e)
        return jsonify({"error": str(e)}), 500


# ── /ar → AR Viewer (Public) ──────────────────
@app.route("/ar")
def ar_viewer():
    mapping = load_mapping()
    targets_url = get_public_url("targets/targets.mind")
    mind_ready = len(mapping) > 0 and any(e.get("video_url") for e in mapping)
    return render_template("ar.html", mapping=mapping, mind_ready=mind_ready, targets_url=targets_url)


@app.after_request
def headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    # Allow camera on all pages (required for AR on mobile Chrome/Safari)
    resp.headers["Permissions-Policy"] = "camera=*, microphone=()"
    # Prevent caching of AR target data so fresh uploads are always picked up
    if request.path.endswith(".mind") or request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

