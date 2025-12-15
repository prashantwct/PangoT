import sqlite3
import random
import math
from datetime import datetime, timedelta

# --- CONFIGURATION ---
CENTER_LAT = 19.0500
CENTER_LON = 73.0500
PANGOLINS = ["P01", "P02", "P03", "P04", "P05", "P06", "P07", "P08"]
OBSERVERS = ["MK", "PD", "Rahul", "Team_A"]

# --- HELPER: Calculate Bearing between two points ---
def get_bearing(lat1, lon1, lat2, lon2):
    dLon = (lon2 - lon1)
    x = math.cos(math.radians(lat2)) * math.sin(math.radians(dLon))
    y = math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) \
        - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(dLon))
    brng = math.atan2(x,y)
    brng = math.degrees(brng)
    return (brng + 360) % 360

# --- HELPER: Generate a random point nearby (within ~2km) ---
def get_nearby_point(lat, lon, dist_km=2):
    # Roughly: 1 deg lat = 110km, 1 deg lon = 110km * cos(lat)
    r_lat = random.uniform(-0.02, 0.02) # +/- 2km approx
    r_lon = random.uniform(-0.02, 0.02)
    return lat + r_lat, lon + r_lon

def seed_data():
    conn = sqlite3.connect('pangolin_data.db')
    c = conn.cursor()
    
    print("Generating 50 realistic entries...")

    # We will generate 25 "Sessions" (Pairs of readings) = 50 Total Entries
    for i in range(1, 26):
        # 1. Pick a random Animal and a "True" Location for it (The Secret Spot)
        pango = random.choice(PANGOLINS)
        true_pango_lat = get_nearby_point(CENTER_LAT, CENTER_LON)[0]
        true_pango_lon = get_nearby_point(CENTER_LAT, CENTER_LON)[1]
        
        # 2. Generate Metadata
        group_id = f"Auto_Sim_{i:02d}"
        obs_time = datetime.now() - timedelta(days=random.randint(0, 30), minutes=random.randint(0, 600))
        
        # 3. Create Observer A (Somewhere 500m - 1km away)
        obs_a_lat, obs_a_lon = get_nearby_point(true_pango_lat, true_pango_lon)
        # Calculate bearing TO the animal
        true_bearing_a = get_bearing(obs_a_lat, obs_a_lon, true_pango_lat, true_pango_lon)
        # Add "Human Error" (+/- 4 degrees)
        final_bearing_a = true_bearing_a + random.uniform(-4, 4)
        
        # 4. Create Observer B (Somewhere else)
        obs_b_lat, obs_b_lon = get_nearby_point(true_pango_lat, true_pango_lon)
        true_bearing_b = get_bearing(obs_b_lat, obs_b_lon, true_pango_lat, true_pango_lon)
        final_bearing_b = true_bearing_b + random.uniform(-4, 4)

        # 5. Insert into Database
        # Entry 1
        c.execute("""
            INSERT INTO raw_bearings 
            (group_id, pango_id, observer, obs_lat, obs_lon, bearing, timestamp) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (group_id, pango, random.choice(OBSERVERS), obs_a_lat, obs_a_lon, round(final_bearing_a, 1), obs_time))

        # Entry 2
        c.execute("""
            INSERT INTO raw_bearings 
            (group_id, pango_id, observer, obs_lat, obs_lon, bearing, timestamp) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (group_id, pango, random.choice(OBSERVERS), obs_b_lat, obs_b_lon, round(final_bearing_b, 1), obs_time))
        
        print(f" - Created Group {group_id}: {pango} near {true_pango_lat:.4f}, {true_pango_lon:.4f}")

    conn.commit()
    conn.close()
    print("\n✅ Success! Added 50 entries.")
    print("Go to your Dashboard and click 'Sync Now' or restart the app to see the Red Pins appear.")

if __name__ == "__main__":
    seed_data()