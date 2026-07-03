import sys
import os
import time
import re
import struct
import webbrowser
import sqlite3
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

# Determine PROGRAM_DIR, DB, and Files paths
if getattr(sys, 'frozen', False):
    PROGRAM_DIR = Path(sys.executable).resolve().parent
else:
    CODE_DIR = Path(__file__).resolve().parent
    PROGRAM_DIR = CODE_DIR.parent

DATA_ROOT_DIR = PROGRAM_DIR / "DB"
FILES_DIR = DATA_ROOT_DIR / "files"
DB_ROOT_DIR = DATA_ROOT_DIR / "DB"

def ensure_dirs():
    """Creates the DB, files, and DB directories next to the program."""
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    DB_ROOT_DIR.mkdir(parents=True, exist_ok=True)

# Cache for AccelonDB instances (only used for resource/image extraction on demand)
loaded_adbs = {}

def get_adb_instance(dataset_name: str) -> AccelonDB:
    """Gets or loads the AccelonDB instance for resource streaming on the fly."""
    if dataset_name not in loaded_adbs:
        adb_path = FILES_DIR / f"{dataset_name}.adb"
        if not adb_path.exists():
            raise FileNotFoundError(f"Database file not found: {adb_path}")
        loaded_adbs[dataset_name] = AccelonDB(str(adb_path))
    return loaded_adbs[dataset_name]

def get_sqlite_path(dataset_name: str) -> Path:
    """Returns the path to the SQLite file for the given dataset."""
    return DB_ROOT_DIR / dataset_name / "data.sqlite"

def build_sqlite_db(dataset_name: str):
    """Parses .adb dictionary and builds a fully indexed SQLite database in under 5 seconds."""
    adb_path = FILES_DIR / f"{dataset_name}.adb"
    if not adb_path.exists():
        raise FileNotFoundError(f"Database file not found: {adb_path}")
        
    print(f"Building SQLite database for {dataset_name}...")
    start_time = time.time()
    
    # Parse adb
    db = AccelonDB(str(adb_path))
    full_xml = db.get_xml()
    db_lines = full_xml.split('\n')
    print(f"Parsed {len(db_lines):,} lines in {time.time() - start_time:.2f}s.")
    
    # Ensure directories exist
    db_dir = DB_ROOT_DIR / dataset_name
    db_dir.mkdir(parents=True, exist_ok=True)
    sqlite_path = db_dir / "data.sqlite"
    
    # Remove existing partial sqlite db
    if sqlite_path.exists():
        try:
            sqlite_path.unlink()
        except Exception as e:
            print(f"Failed to delete existing database: {e}")
            
    # Process headwords for line parent grouping
    headword_pattern = re.compile(r'<_*(?:-)*詞[^>]*>(.*?)</_*(?:-)*詞>')
    def strip_xml_tags(text):
        return re.sub(r'<[^>]+>', '', text)
        
    current_headword = None
    current_headword_line = None
    
    insert_data = []
    for idx, line in enumerate(db_lines):
        line_num = idx + 1
        m = headword_pattern.search(line)
        is_hw = 0
        if m:
            hw = strip_xml_tags(m.group(1))
            if hw:
                current_headword = hw
                current_headword_line = line_num
                is_hw = 1
        insert_data.append((
            line_num,
            current_headword,
            current_headword_line,
            line,
            is_hw
        ))
        
    # Write to SQLite
    conn = sqlite3.connect(str(sqlite_path))
    try:
        conn.execute("PRAGMA journal_mode = OFF;")
        conn.execute("PRAGMA synchronous = OFF;")
        conn.execute("PRAGMA cache_size = 100000;")
        
        conn.execute("""
        CREATE TABLE IF NOT EXISTS lines (
            line_number INTEGER PRIMARY KEY,
            headword TEXT,
            headword_line INTEGER,
            content TEXT,
            is_headword INTEGER
        );
        """)
        
        conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lines_headword ON lines(headword);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lines_headword_line ON lines(headword_line);")
        
        # Insert metadata parameters
        metadata_vals = [
            ("dbname", db.dbname),
            ("dbcname", db.dbcname),
            ("version", str(db.version)),
            ("linecount", str(db.linecount)),
            ("tagcount", str(db.tagcount)),
            ("tokencount", str(db.tokencount))
        ]
        conn.executemany("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", metadata_vals)
        
        # Batch insert lines in chunks
        chunk_size = 50000
        for i in range(0, len(insert_data), chunk_size):
            conn.executemany(
                "INSERT INTO lines (line_number, headword, headword_line, content, is_headword) VALUES (?, ?, ?, ?, ?)",
                insert_data[i:i+chunk_size]
            )
            
        conn.commit()
        print(f"SQLite DB built successfully for {dataset_name} in {time.time() - start_time:.2f}s.")
    except Exception as e:
        conn.rollback()
        if sqlite_path.exists():
            try:
                sqlite_path.unlink()
            except:
                pass
        raise e
    finally:
        conn.close()

def get_db_connection(dataset_name: str) -> sqlite3.Connection:
    """Gets a connection to the SQLite database, building it first if not present."""
    sqlite_path = get_sqlite_path(dataset_name)
    if not sqlite_path.exists():
        build_sqlite_db(dataset_name)
    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row
    return conn

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
    """Accepts uploaded .adb database, saves it in DB/files, and builds the SQLite database."""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"ok": False, "error": "Empty filename"}), 400

    filename_lower = file.filename.lower()
    if not filename_lower.endswith(".adb"):
        return jsonify({"ok": False, "error": "Accelon 데이터베이스 파일(.adb)만 업로드할 수 있습니다."}), 400

    ensure_dirs()
    dataset_name = safe_dataset_name(file.filename)
    adb_path = FILES_DIR / f"{dataset_name}.adb"

    try:
        file.save(adb_path)
        
        # Build the SQLite DB immediately
        build_sqlite_db(dataset_name)
        
        # Retrieve line count from SQLite
        conn = get_db_connection(dataset_name)
        row = conn.execute("SELECT COUNT(*) FROM lines").fetchone()
        line_count = row[0]
        conn.close()
        
        return jsonify({
            "ok": True,
            "dataset_name": dataset_name,
            "line_count": line_count
        })
    except Exception as e:
        if adb_path.exists():
            try:
                adb_path.unlink()
            except:
                pass
        sqlite_path = get_sqlite_path(dataset_name)
        if sqlite_path.exists():
            try:
                sqlite_path.unlink()
            except:
                pass
        return jsonify({"ok": False, "error": f"사전 데이터 구성 중 오류 발생: {str(e)}"}), 500


@app.route('/api/datasets')
def api_datasets():
    """Lists stems of all registered dictionaries inside DB/files/."""
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
        conn = get_db_connection(dataset_name)
        rows = conn.execute("SELECT key, value FROM metadata").fetchall()
        conn.close()
        
        meta = {r['key']: r['value'] for r in rows}
        return jsonify({
            "ok": True,
            "dbname": meta.get("dbname"),
            "dbcname": meta.get("dbcname"),
            "version": int(meta.get("version", 0)),
            "linecount": int(meta.get("linecount", 0)),
            "tagcount": int(meta.get("tagcount", 0)),
            "tokencount": int(meta.get("tokencount", 0))
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/search/<dataset_name>')
def api_search(dataset_name):
    """Performs space-separated AND search with fallback on a specific dictionary."""
    query = request.args.get('q', '').strip()
    mode = request.args.get('mode', 'all')
    limit = int(request.args.get('limit', '50'))
    
    if not query:
        return jsonify({"ok": True, "results": [], "query_terms": []})
        
    subqueries = [sq for sq in query.split() if sq]
    if not subqueries:
        return jsonify({"ok": True, "results": [], "query_terms": []})
        
    def perform_search_sqlite(search_subqueries):
        conn = get_db_connection(dataset_name)
        cur = conn.cursor()
        
        if mode == 'headword':
            sql = "SELECT line_number, headword, headword_line, content FROM lines WHERE " + " AND ".join(["headword LIKE ?" for _ in search_subqueries])
            params = [f"%{q}%" for q in search_subqueries]
        else:
            sql = "SELECT line_number, headword, headword_line, content FROM lines WHERE " + " AND ".join(["content LIKE ?" for _ in search_subqueries])
            params = [f"%{q}%" for q in search_subqueries]
            
        cur.execute(sql, params)
        
        results = []
        headword_pattern = re.compile(r'<_*(?:-)*詞[^>]*>(.*?)</_*(?:-)*詞>')
        quote_pattern = re.compile(r'[“‘「『]([^”’」』]+)[”’」』]')
        
        def strip_xml_tags(text):
            return re.sub(r'<[^>]+>', '', text)
            
        for row in cur:
            line_num = row['line_number']
            headword = row['headword']
            headword_line = row['headword_line']
            content = row['content']
            
            is_match = False
            if mode == 'all':
                is_match = True
            elif mode == 'headword':
                is_match = True
            elif mode == 'example':
                line_no_hw = headword_pattern.sub('', content)
                quotes = [strip_xml_tags(m.group(1)) for m in quote_pattern.finditer(line_no_hw)]
                is_match = any(all(subq in q for subq in search_subqueries) for q in quotes)
                
            if is_match:
                hw_display = headword if line_num == headword_line else f"{headword} (Line {line_num})"
                results.append({
                    'line_num': line_num,
                    'headword': hw_display or f"Line {line_num}",
                    'preview': strip_xml_tags(content)[:150]
                })
                if len(results) >= limit:
                    break
        conn.close()
        return results

    try:
        results = perform_search_sqlite(subqueries)
        
        # Try OpenCC fallback if no results found
        if not results:
            try:
                cc = OpenCC('t2s')
                converted_query = cc.convert(query)
                converted_subqueries = [sq for sq in converted_query.split() if sq]
                if converted_subqueries != subqueries:
                    results = perform_search_sqlite(converted_subqueries)
            except Exception as e:
                print(f"Fallback conversion error: {e}")
                
        # Generate terms list for query highlighting (including both Traditional/Simplified variants)
        highlight_terms = list(subqueries)
        try:
            cc_t2s = OpenCC('t2s')
            cc_s2t = OpenCC('s2t')
            for q in subqueries:
                s_val = cc_t2s.convert(q)
                t_val = cc_s2t.convert(q)
                if s_val not in highlight_terms:
                    highlight_terms.append(s_val)
                if t_val not in highlight_terms:
                    highlight_terms.append(t_val)
        except Exception as e:
            print(f"Highlight terms generation error: {e}")
            
        return jsonify({
            "ok": True,
            "results": results,
            "query_terms": highlight_terms
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/entry/<dataset_name>/<int:line_num>')
def api_entry(dataset_name, line_num):
    """Retrieves full entry paragraph (from matched headword to before next headword) using SQLite."""
    try:
        conn = get_db_connection(dataset_name)
        cur = conn.cursor()
        
        # Find headword_line for the requested line_num
        row = cur.execute(
            "SELECT headword, headword_line FROM lines WHERE line_number = ?",
            (line_num,)
        ).fetchone()
        
        if not row:
            conn.close()
            return jsonify({'error': 'Line not found'}), 404
            
        headword = row['headword']
        headword_line = row['headword_line']
        
        # Find next headword line
        next_hw_row = cur.execute(
            "SELECT MIN(line_number) as next_hw FROM lines WHERE line_number > ? AND is_headword = 1",
            (headword_line,)
        ).fetchone()
        
        next_headword_line = next_hw_row['next_hw']
        
        # Retrieve all lines belonging to this entry block
        if next_headword_line:
            lines_rows = cur.execute(
                "SELECT content FROM lines WHERE line_number >= ? AND line_number < ? ORDER BY line_number ASC",
                (headword_line, next_headword_line)
            ).fetchall()
        else:
            lines_rows = cur.execute(
                "SELECT content FROM lines WHERE line_number >= ? ORDER BY line_number ASC",
                (headword_line,)
            ).fetchall()
            
        conn.close()
        
        # Concatenate line contents
        full_content = "\n".join([r['content'] for r in lines_rows])
        hw_display = headword if line_num == headword_line else f"{headword} (Line {line_num})"
        
        return jsonify({
            'line_num': line_num,
            'headword': hw_display or f"Line {line_num}",
            'raw_content': full_content
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/resource/<dataset_name>/<string:filename>')
def api_resource(dataset_name, filename):
    """Streams a PNG image directly from the specified database file."""
    try:
        db = get_adb_instance(dataset_name)
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
