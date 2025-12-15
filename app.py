from flask import Flask, request, jsonify, render_template, Response, send_from_directory
import sqlite3
import csv
import io
from datetime import datetime
from functools import wraps
import numpy as np
from pyproj import Transformer

app = Flask(__name__)

# --- CONFIG ---
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "pango2025" 

# --- COORDINATE SYSTEMS ---
to_xy = Transformer.from_crs("EPSG:4326", "EPSG:32644", always_xy=True)
to_ll = Transformer.from_crs("EPSG:32644", "EPSG:4326", always_xy=True)

# --- DATABASE SETUP ---
def init_db():
    conn = sqlite3.connect('pangolin_data.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS raw_bearings 
                 (id INTEGER PRIMARY KEY, group_id TEXT, pango_id TEXT, 
                  observer TEXT, obs_lat REAL, obs_lon REAL, bearing REAL, 
                  timestamp DATETIME)''')     
    c.execute('''CREATE TABLE IF NOT EXISTS calculated_fixes 
                 (id INTEGER PRIMARY KEY, group_id TEXT, pango_id TEXT, 
                  calc_lat REAL, calc_lon REAL, timestamp DATETIME, note TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS animals 
                 (id TEXT PRIMARY KEY, created_at DATETIME)''')
    try:
        c.execute("ALTER TABLE raw_bearings ADD COLUMN gps_accuracy REAL")
    except sqlite3.OperationalError: pass
    
    c.execute("SELECT count(*) FROM animals")
    if c.fetchone()[0] == 0:
        defaults = [(f"P{i:02d}", datetime.now()) for i in range(1, 17)]
        c.executemany("INSERT INTO animals VALUES (?,?)", defaults)
        conn.commit()
    conn.commit()
    conn.close()

init_db()

# --- MATH HELPERS ---
def bearing_to_unit_vector(b):
    rad = np.deg2rad(b)
    return np.array([np.sin(rad), np.cos(rad)])

def perform_triangulation(readings):
    try:
        points_xy = []
        bearings = []
        for r in readings:
            lat, lon, brng = r
            x, y = to_xy.transform(lon, lat)
            points_xy.append((x, y))
            bearings.append(brng)

        A, B = [], []
        for (x, y), b in zip(points_xy, bearings):
            dx, dy = bearing_to_unit_vector(b)
            A.append([dy, -dx])
            B.append(dy * x - dx * y)
            
        A, B = np.array(A), np.array(B)
        sol, residuals, rank, s = np.linalg.lstsq(A, B, rcond=None)
        calc_lon, calc_lat = to_ll.transform(sol[0], sol[1])
        return (calc_lat, calc_lon)
    except Exception as e:
        return f"Math Error: {str(e)}"

# --- AUTH ---
def check_auth(username, password):
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD

def authenticate():
    return Response('Login Required', 401, {'WWW-Authenticate': 'Basic realm="Login Required"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# --- ROUTES ---
@app.route('/')
def home(): return render_template('index.html')

@app.route('/manifest.json')
def manifest(): return send_from_directory('.', 'manifest.json')

@app.route('/sw.js')
def service_worker(): return send_from_directory('.', 'sw.js')

@app.route('/get_animals')
def get_animals():
    conn = sqlite3.connect('pangolin_data.db')
    c = conn.cursor()
    c.execute("SELECT id FROM animals ORDER BY id")
    res = [row[0] for row in c.fetchall()]
    conn.close()
    return jsonify(res)

@app.route('/add_animal', methods=['POST'])
def add_animal():
    new_id = request.json.get('id')
    conn = sqlite3.connect('pangolin_data.db')
    try:
        conn.execute("INSERT INTO animals VALUES (?,?)", (new_id, datetime.now()))
        conn.commit()
        return jsonify({"status": "added"})
    except: return jsonify({"status": "exists"})
    finally: conn.close()

@app.route('/sync', methods=['POST'])
def sync_data():
    try:
        incoming_data = request.json 
        results = []
        conn = sqlite3.connect('pangolin_data.db')
        c = conn.cursor()
        
        for item in incoming_data:
            c.execute("""INSERT INTO raw_bearings 
                         (group_id, pango_id, observer, obs_lat, obs_lon, bearing, gps_accuracy, timestamp) 
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                      (item['group_id'], item['pango_id'], item.get('observer','--'), 
                       item['lat'], item['lon'], item['bearing'], item.get('accuracy', 0), item['time']))
        conn.commit()

        unique_groups = set(item['group_id'] for item in incoming_data)
        for gid in unique_groups:
            c.execute("SELECT obs_lat, obs_lon, bearing, pango_id FROM raw_bearings WHERE group_id = ?", (gid,))
            readings = c.fetchall()
            
            if len(readings) < 2:
                results.append(f"⏳ {gid}: Saved {len(readings)}/2 readings")
                continue

            math_input = [(r[0], r[1], r[2]) for r in readings]
            res = perform_triangulation(math_input)
            
            # Only update if NOT verified (optional logic, but here we just overwrite)
            c.execute("DELETE FROM calculated_fixes WHERE group_id = ?", (gid,))
            if isinstance(res, tuple):
                c.execute("INSERT INTO calculated_fixes (group_id, pango_id, calc_lat, calc_lon, timestamp, note) VALUES (?, ?, ?, ?, ?, ?)",
                          (gid, readings[0][3], res[0], res[1], datetime.now(), "Least Squares"))
                results.append(f"✅ {gid}: Fix Calculated! ({res[0]:.5f}, {res[1]:.5f})")
            else:
                results.append(f"⚠️ {gid}: {res}")

        conn.commit()
        conn.close()
        return jsonify({"status": "success", "messages": results})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- DASHBOARD & EDIT API ---

@app.route('/dashboard')
@requires_auth
def dashboard(): return render_template('dashboard.html')

@app.route('/api/data')
@requires_auth
def api_data():
    conn = sqlite3.connect('pangolin_data.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM raw_bearings")
    raw = [dict(row) for row in c.fetchall()]
    # Include ID so we can edit/delete specific rows
    c.execute("SELECT id, group_id, pango_id, calc_lat, calc_lon, timestamp, note FROM calculated_fixes")
    fixes = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify({"raw": raw, "fixes": fixes})

@app.route('/api/delete_fix/<int:fix_id>', methods=['DELETE'])
@requires_auth
def delete_fix(fix_id):
    conn = sqlite3.connect('pangolin_data.db')
    try:
        conn.execute("DELETE FROM calculated_fixes WHERE id = ?", (fix_id,))
        conn.commit()
        return jsonify({"status": "deleted"})
    finally:
        conn.close()

@app.route('/api/update_fix/<int:fix_id>', methods=['POST'])
@requires_auth
def update_fix(fix_id):
    data = request.json
    conn = sqlite3.connect('pangolin_data.db')
    try:
        conn.execute("UPDATE calculated_fixes SET pango_id = ?, note = ? WHERE id = ?", 
                     (data['pango_id'], data['note'], fix_id))
        conn.commit()
        return jsonify({"status": "updated"})
    finally:
        conn.close()

@app.route('/download_csv')
@requires_auth
def download_csv():
    conn = sqlite3.connect('pangolin_data.db')
    c = conn.cursor()
    c.execute("SELECT * FROM raw_bearings")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Group_ID', 'Pangolin_ID', 'Observer', 'Lat', 'Lon', 'Bearing', 'Time', 'GPS_Accuracy'])
    writer.writerows(c.fetchall())
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-disposition": "attachment; filename=pangolin_raw_data.csv"})

@app.route('/download_fixes')
@requires_auth
def download_fixes():
    conn = sqlite3.connect('pangolin_data.db')
    c = conn.cursor()
    c.execute("SELECT group_id, pango_id, calc_lat, calc_lon, timestamp, note FROM calculated_fixes")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Group_ID', 'Pangolin_ID', 'Lat', 'Lon', 'Time', 'Note'])
    writer.writerows(c.fetchall())
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-disposition": "attachment; filename=pangolin_final_locations.csv"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)