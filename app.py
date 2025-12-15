import os
import json
from flask import Flask, request, jsonify, render_template, Response, send_from_directory
from flask_wtf.csrf import CSRFProtect
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from dotenv import load_dotenv
import csv
import io
from datetime import datetime, timezone
from functools import wraps
import numpy as np
from pyproj import Transformer

# --- CONFIG & INIT ---
load_dotenv() 

app = Flask(__name__)

# Use the DATABASE_URL environment variable, falling back to SQLite for local dev
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///pangolin_data.db') 
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-key-fallback')

db = SQLAlchemy(app)
migrate = Migrate(app, db)
csrf = CSRFProtect(app)

# Credentials (from .env)
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'pango2025')
MAPBOX_TOKEN = os.getenv('MAPBOX_TOKEN', '')

# --- MODELS ---
class RawBearing(db.Model):
    __tablename__ = 'raw_bearings'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.String(80), index=True)
    pango_id = db.Column(db.String(10))
    observer = db.Column(db.String(10))
    obs_lat = db.Column(db.Float)
    obs_lon = db.Column(db.Float)
    bearing = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    gps_accuracy = db.Column(db.Float)
    
class CalculatedFix(db.Model):
    __tablename__ = 'calculated_fixes'
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.String(80), index=True)
    pango_id = db.Column(db.String(10))
    calc_lat = db.Column(db.Float)
    calc_lon = db.Column(db.Float)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    note = db.Column(db.String(255))
    
class Animal(db.Model):
    __tablename__ = 'animals'
    id = db.Column(db.String(10), primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# Helper function to convert SQLAlchemy models to a serializable dictionary
def to_dict(model):
    data = {}
    for column in model.__table__.columns:
        value = getattr(model, column.name)
        # Convert datetime objects to ISO format string for JSON
        if isinstance(value, datetime):
            data[column.name] = value.isoformat()
        else:
            data[column.name] = value
    return data

# --- MATH HELPERS ---
to_xy = Transformer.from_crs("EPSG:4326", "EPSG:32644", always_xy=True)
to_ll = Transformer.from_crs("EPSG:32644", "EPSG:4326", always_xy=True)

def bearing_to_unit_vector(b):
    rad = np.deg2rad(b)
    return np.array([np.sin(rad), np.cos(rad)])

def perform_triangulation(readings):
    """
    Returns: (lat, lon, error_metric)
    error_metric is the 'Residual Sum of Squares' root mean.
    """
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
        
        # Least Squares Calculation
        sol, residuals, rank, s = np.linalg.lstsq(A, B, rcond=None)
        
        calc_lon, calc_lat = to_ll.transform(sol[0], sol[1])
        
        # Calculate Confidence/Error
        error_score = 0.0
        if len(residuals) > 0:
            # Root Mean Square of Residuals (approximates avg error distance in meters)
            error_score = np.sqrt(residuals[0] / len(readings)) 
        
        return (calc_lat, calc_lon, error_score)
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
    # SQLAlchemy: Fetch animals
    res = [a.id for a in Animal.query.order_by(Animal.id).all()]
    
    # Seed default animals if the table is empty
    if not res:
        defaults = [(f"P{i:02d}", datetime.utcnow()) for i in range(1, 17)]
        # Use bulk_insert for efficiency
        db.session.bulk_insert_mappings(Animal, [{'id': d[0], 'created_at': d[1]} for d in defaults])
        db.session.commit()
        res = [d[0] for d in defaults]
        
    return jsonify(res)

@app.route('/add_animal', methods=['POST'])
@csrf.exempt
def add_animal():
    new_id = request.json.get('id')
    if not new_id:
        return jsonify({"status": "error", "message": "ID required"}), 400
        
    try:
        new_animal = Animal(id=new_id, created_at=datetime.utcnow())
        db.session.add(new_animal)
        db.session.commit()
        return jsonify({"status": "added"})
    except Exception as e:
        db.session.rollback()
        # Check for unique constraint violation (common SQL error when ID exists)
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            return jsonify({"status": "exists"}), 409
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/sync', methods=['POST'])
@csrf.exempt
def sync_data():
    try:
        incoming_data = request.json 
        results = []
        unique_groups = set(item['group_id'] for item in incoming_data)
        
        # 1. Insert Raw Data
        raw_bearings_to_add = []
        for item in incoming_data:
            # Convert ISO string timestamp to datetime object
            ts = datetime.fromisoformat(item['time'].replace('Z', '+00:00'))
            
            raw_bearings_to_add.append(RawBearing(
                group_id=item['group_id'], 
                pango_id=item['pango_id'], 
                observer=item.get('observer','--'), 
                obs_lat=item['lat'], 
                obs_lon=item['lon'], 
                bearing=item['bearing'], 
                gps_accuracy=item.get('accuracy', 0), 
                timestamp=ts
            ))
        db.session.add_all(raw_bearings_to_add)
        db.session.commit()

        # 2. Process Groups
        for gid in unique_groups:
            # SQLAlchemy: Fetch all readings for the group
            readings_query = RawBearing.query.filter_by(group_id=gid).all()
            
            readings_list = [(r.obs_lat, r.obs_lon, r.bearing) for r in readings_query]
            
            if len(readings_list) < 2:
                results.append(f"⏳ {gid}: Saved {len(readings_list)}/2 readings")
                continue

            # 3. Calculate with Error Metric
            res = perform_triangulation(readings_list)
            
            # Clean up old fix before inserting new one
            CalculatedFix.query.filter_by(group_id=gid).delete()
            
            if isinstance(res, tuple):
                lat, lon, err = res
                
                note = "Least Squares"
                if len(readings_list) > 2:
                    note += f" (Err: {err:.1f}m)"
                else:
                    note += " (2-Line Fix)"
                    
                new_fix = CalculatedFix(
                    group_id=gid, 
                    pango_id=readings_query[0].pango_id, # Use pango_id from first reading
                    calc_lat=lat, 
                    calc_lon=lon, 
                    timestamp=datetime.utcnow(), 
                    note=note
                )
                db.session.add(new_fix)
                results.append(f"✅ {gid}: Fix Calculated! Err: {err:.2f}m")
            else:
                results.append(f"⚠️ {gid}: {res}")

        db.session.commit()
        return jsonify({"status": "success", "messages": results})
    except Exception as e:
        db.session.rollback()
        print(f"Sync Error: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

# --- SECURE DASHBOARD ROUTES ---

@app.route('/dashboard')
@requires_auth
def dashboard(): 
    return render_template('dashboard.html', mapbox_token=MAPBOX_TOKEN)

@app.route('/api/data')
@requires_auth
def api_data():
    # SQLAlchemy: Fetch all data and convert to dicts
    raw = [to_dict(r) for r in RawBearing.query.all()]
    fixes = [to_dict(f) for f in CalculatedFix.query.all()]
    
    return jsonify({"raw": raw, "fixes": fixes})

@app.route('/api/delete_fix/<int:fix_id>', methods=['DELETE'])
@requires_auth
@csrf.exempt 
def delete_fix(fix_id):
    try:
        fix_to_delete = CalculatedFix.query.get(fix_id)
        if fix_to_delete:
            db.session.delete(fix_to_delete)
            db.session.commit()
            return jsonify({"status": "deleted"})
        return jsonify({"status": "not found"}), 404
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/update_fix/<int:fix_id>', methods=['POST'])
@requires_auth
@csrf.exempt
def update_fix(fix_id):
    data = request.json
    try:
        fix_to_update = CalculatedFix.query.get(fix_id)
        if fix_to_update:
            fix_to_update.pango_id = data.get('pango_id', fix_to_update.pango_id)
            fix_to_update.note = data.get('note', fix_to_update.note)
            db.session.commit()
            return jsonify({"status": "updated"})
        return jsonify({"status": "not found"}), 404
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# --- CSV DOWNLOADS ---
def create_csv_response(query_results, header_fields, filename):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(header_fields)
    
    for row in query_results:
        # Extract values in the order of the header fields
        data_row = []
        for field in header_fields:
            value = getattr(row, field)
            if isinstance(value, datetime):
                value = value.isoformat()
            data_row.append(value)
        writer.writerow(data_row)
        
    return Response(
        output.getvalue(), 
        mimetype="text/csv", 
        headers={"Content-disposition": f"attachment; filename={filename}"}
    )

@app.route('/download_csv')
@requires_auth
def download_csv():
    results = RawBearing.query.order_by(RawBearing.timestamp).all()
    header = ['id', 'group_id', 'pango_id', 'observer', 'obs_lat', 'obs_lon', 'bearing', 'timestamp', 'gps_accuracy']
    return create_csv_response(results, header, "pangolin_raw_data.csv")

@app.route('/download_fixes')
@requires_auth
def download_fixes():
    results = CalculatedFix.query.order_by(CalculatedFix.timestamp).all()
    header = ['group_id', 'pango_id', 'calc_lat', 'calc_lon', 'timestamp', 'note']
    return create_csv_response(results, header, "pangolin_final_locations.csv")

if __name__ == '__main__':
    # Context must be pushed to allow SQLAlchemy to interact with app config outside a request
    with app.app_context():
        # This will create tables if running against SQLite, or is skipped if tables exist on Neon
        db.create_all()
        
    app.run(host='0.0.0.0', port=5000, debug=True)