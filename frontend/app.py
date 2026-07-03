import sys
import os
import time
import re
import struct
import webbrowser
from pathlib import Path
from threading import Timer, Thread
from flask import Flask, render_template, jsonify, request, Response

# Handle PyInstaller frozen mode for module imports and resource paths
if getattr(sys, 'frozen', False):
    # PyInstaller bundles everything into sys._MEIPASS
    _base_path = sys._MEIPASS
    template_folder = os.path.join(_base_path, 'templates')
    static_folder = os.path.join(_base_path, 'static')
    sys.path.insert(0, _base_path)
else:
    template_folder = os.path.join(os.path.dirname(__file__), 'templates')
    static_folder = os.path.join(os.path.dirname(__file__), 'static')
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from code_base.accelon import AccelonDB
from opencc import OpenCC

app = Flask(__name__, 
            template_folder=template_folder,
            static_folder=static_folder)

app.config["MAX_CONTENT_LENGTH"] = 250 * 1024 * 1024  # Support up to 250MB adb files

# Determine PROGRAM_DIR and DB paths
if getattr(sys, 'frozen', False):
    PROGRAM_DIR = Path(sys.executable).resolve().parent
else:
    CODE_DIR = Path(__file__).resolve().parent
    PROGRAM_DIR = CODE_DIR.parent

DATA_ROOT_DIR = PROGRAM_DIR / "DB"
FILES_DIR = DATA_ROOT_DIR / "files"

def ensure_dirs():
    """Creates the DB/files folder next to the program."""
    FILES_DIR.mkdir(parents=True, exist_ok=True)

# In-memory dictionary database cache (dataset_name -> { 'db': AccelonDB, 'lines': list[str] })
loaded_datasets = {}

def get_db(dataset_name: str):
    """Retrieves or loads the AccelonDB instance for the dataset on the fly."""
    if dataset_name not in loaded_datasets:
        db_path = FILES_DIR / f"{dataset_name}.adb"
        if not db_path.exists():
            raise FileNotFoundError(f"Database file not found: {db_path}")
            
        print(f"Loading database {dataset_name} on the fly...")
        db = AccelonDB(str(db_path))
        print(f"Decompressing full text for {dataset_name}...")
        full_xml = db.get_xml()
        db_lines = full_xml.split('\n')
        print(f"Loaded {len(db_lines):,} lines.")
        
        loaded_datasets[dataset_name] = {
            'db': db,
            'lines': db_lines
        }
    return loaded_datasets[dataset_name]

def safe_dataset_name(filename: str) -> str:
    name = Path(filename).stem
    name = re.sub(r"[^0-9A-Za-z가-힣_\-]+", "_", name)
    name = name.strip("_")
    if not name:
        name = "dataset"
    return name

def list_datasets() -> list[str]:
    ensure_dirs()
    if not FILES_DIR.exists():
        return []
    datasets = []
    for path in FILES_DIR.iterdir():
        if path.is_file() and path.suffix.lower() == '.adb':
            datasets.append(path.stem)
    return sorted(datasets)


# ==========================================
#  HTTP ROUTES
# ==========================================

@app.route('/')
def index():
    """Serves the welcome landing screen."""
    return render_template('index.html')


@app.route('/upload')
def upload_view():
    """Serves the dictionary upload screen."""
    return render_template('upload.html')


@app.post('/api/upload')
def upload_file():
    """Accepts uploaded .adb database, saves it in DB/files, and tests parser loading."""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"ok": False, "error": "Empty filename"}), 400

    filename_lower = file.filename.lower()
    if not filename_lower.endswith(".adb"):
        return jsonify({"ok": False, "error": "Accelon 데이터베이스 파일(.adb)만 업로드할 수 있습니다."}), 400

    # Ensure DB directory exists next to the program on upload
    ensure_dirs()

    dataset_name = safe_dataset_name(file.filename)
    adb_path = FILES_DIR / f"{dataset_name}.adb"

    try:
        file.save(adb_path)
        
        # Test load
        print(f"Validating uploaded dictionary file: {adb_path}")
        db = AccelonDB(str(adb_path))
        full_xml = db.get_xml()
        db_lines = full_xml.split('\n')
        
        # Immediately cache the loaded db
        loaded_datasets[dataset_name] = {
            'db': db,
            'lines': db_lines
        }
        
        return jsonify({
            "ok": True,
            "dataset_name": dataset_name,
            "line_count": len(db_lines)
        })
    except Exception as e:
        if adb_path.exists():
            adb_path.unlink()
        return jsonify({"ok": False, "error": f"사전 데이터 구성 중 오류 발생: {str(e)}"}), 500


@app.route('/api/datasets')
def api_datasets():
    """Lists stems of all registered dictionaries inside DB/files/."""
    # When this button / list view is called, the DB directory is created
    ensure_dirs()
    try:
        datasets = list_datasets()
        return jsonify({"ok": True, "datasets": datasets})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/search/<dataset_name>')
def search_view(dataset_name):
    """Serves search view for a specific dictionary."""
    db_path = FILES_DIR / f"{dataset_name}.adb"
    if not db_path.exists():
        return "Dataset not found", 404
    return render_template("search.html", dataset_name=dataset_name)


@app.route('/api/info/<dataset_name>')
def api_info(dataset_name):
    """Returns metadata details for a specific dictionary dataset."""
    try:
        data = get_db(dataset_name)
        db = data['db']
        return jsonify({
            "ok": True,
            "dbname": db.dbname,
            "dbcname": db.dbcname,
            "version": db.version,
            "linecount": db.linecount,
            "tagcount": db.tagcount,
            "tokencount": db.tokencount
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/search/<dataset_name>')
def api_search(dataset_name):
    """Performs space-separated AND search with fallback on a specific dictionary."""
    try:
        data = get_db(dataset_name)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to load dataset: {e}"}), 404
        
    query = request.args.get('q', '').strip()
    mode = request.args.get('mode', 'all')
    limit = int(request.args.get('limit', '50'))
    
    if not query:
        return jsonify([])
        
    subqueries = [sq for sq in query.split() if sq]
    if not subqueries:
        return jsonify([])
        
    headword_pattern = re.compile(r'<_*(?:-)*詞[^>]*>(.*?)</_*(?:-)*詞>')
    quote_pattern = re.compile(r'[“‘「『]([^”’」』]+)[”’」』]')
    
    def strip_xml_tags(text):
        return re.sub(r'<[^>]+>', '', text)
        
    def perform_search(search_subqueries):
        results = []
        for idx, line in enumerate(data['lines']):
            is_match = False
            if mode == 'all':
                is_match = all(subq in line for subq in search_subqueries)
            elif mode == 'headword':
                hws = [strip_xml_tags(m.group(1)) for m in headword_pattern.finditer(line)]
                is_match = any(all(subq in hw for subq in search_subqueries) for hw in hws)
            elif mode == 'example':
                line_no_hw = headword_pattern.sub('', line)
                quotes = [strip_xml_tags(m.group(1)) for m in quote_pattern.finditer(line_no_hw)]
                is_match = any(all(subq in q for subq in search_subqueries) for q in quotes)
                
            if is_match:
                hws = [strip_xml_tags(m.group(1)) for m in headword_pattern.finditer(line)]
                hw_display = hws[0] if hws else f"Line {idx+1}"
                results.append({
                    'line_num': idx + 1,
                    'headword': hw_display,
                    'preview': strip_xml_tags(line)[:150]
                })
                if len(results) >= limit:
                    break
        return results
        
    results = perform_search(subqueries)
    
    # Try OpenCC fallback if no results found
    if not results:
        try:
            cc = OpenCC('t2s')
            converted_query = cc.convert(query)
            converted_subqueries = [sq for sq in converted_query.split() if sq]
            if converted_subqueries != subqueries:
                results = perform_search(converted_subqueries)
        except Exception as e:
            print(f"Fallback conversion error: {e}")
            
    return jsonify(results)


@app.route('/api/entry/<dataset_name>/<int:line_num>')
def api_entry(dataset_name, line_num):
    """Retrieves line content of a specific dictionary dataset."""
    try:
        data = get_db(dataset_name)
    except Exception as e:
        return jsonify({'error': str(e)}), 404
        
    if 1 <= line_num <= len(data['lines']):
        return jsonify({
            'line_num': line_num,
            'raw_content': data['lines'][line_num - 1]
        })
    return jsonify({'error': 'Line not found'}), 404


@app.route('/api/resource/<dataset_name>/<string:filename>')
def api_resource(dataset_name, filename):
    """Streams a PNG image directly from the specified database file."""
    try:
        data = get_db(dataset_name)
        db = data['db']
    except Exception as e:
        return "Dataset not found", 404
        
    if not db.resources or filename not in db.resources.names:
        return "Resource not found", 404
    idx = db.resources.names.index(filename)
    raw_bytes = db.resources.get_raw_data(idx)
    return Response(raw_bytes, mimetype='image/png')


# ==========================================
#  HEARTBEAT & WATCHDOG (AUTO-SHUTDOWN)
# ==========================================

clients = {}
startup_time = time.time()
first_heartbeat_received = False
no_clients_since = None

def heartbeat_watchdog():
    """Autoclose daemon checking heartbeat logs, matching kjj app structure."""
    global first_heartbeat_received, no_clients_since
    time.sleep(25) # Initial boot grace period
    while True:
        time.sleep(2)
        now = time.time()
        
        if not first_heartbeat_received:
            # Shutdown if browser never registers within 35s of server startup
            if now - startup_time > 35:
                print("[WATCHDOG] Initial heartbeat timeout. Shutting down server.")
                os._exit(0)
            continue
            
        active_clients = 0
        for cid, info in list(clients.items()):
            if info["state"] == "visible" and now - info["last_seen"] > 15:
                del clients[cid]
            elif info["state"] == "hidden" and now - info["last_seen"] > 86400: # 24h
                del clients[cid]
            elif info["state"] == "closed" and now - info["last_seen"] > 10:
                del clients[cid]
            else:
                active_clients += 1
                
        if active_clients == 0:
            if no_clients_since is None:
                no_clients_since = now
            elif now - no_clients_since > 3:
                print("[WATCHDOG] All clients closed. Shutting down server.")
                os._exit(0)
        else:
            no_clients_since = None

@app.post("/api/heartbeat")
def heartbeat_route():
    global first_heartbeat_received
    data = request.get_json(silent=True) or {}
    client_id = data.get("client_id", "default")
    state = data.get("state", "visible")
    
    clients[client_id] = {
        "last_seen": time.time(),
        "state": state
    }
    
    if state != "closed":
        first_heartbeat_received = True
        
    return jsonify({"ok": True})


def open_browser():
    webbrowser.open_new("http://127.0.0.1:5001/")

if __name__ == '__main__':
    if getattr(sys, 'frozen', False):
        # Package execution mode: auto open browser, no debug
        watchdog = Thread(target=heartbeat_watchdog, daemon=True)
        watchdog.start()
        Timer(1.0, open_browser).start()
        app.run(host='127.0.0.1', port=5001, debug=False)
    else:
        # Development execution mode
        watchdog = Thread(target=heartbeat_watchdog, daemon=True)
        watchdog.start()
        app.run(host='127.0.0.1', port=5001, debug=True)
