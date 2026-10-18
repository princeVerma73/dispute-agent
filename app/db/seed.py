import os
import pandas as pd
from app.db.session import SessionLocal
from app.db.models import init_db, Rider, Trip, PayoutLine

def seed_database():
    # Initialize database tables if they do not exist
    init_db()
    db = SessionLocal()

    # Skip seeding if data is already present
    if db.query(Rider).first():
        print("Database already seeded. Skipping.")
        db.close()
        return

    # Locate data directory path
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    data_dir = os.path.join(base_dir, "data")

    # 1. Seed Riders
    riders_path = os.path.join(data_dir, "riders.csv")
    if os.path.exists(riders_path):
        df_riders = pd.read_csv(riders_path)
        df_riders.drop_duplicates(subset=['rider_id'], inplace=True)  # ADDED FIX
        for _, row in df_riders.iterrows():
            db.merge(Rider(
                rider_id=str(row.get("rider_id")),
                name=str(row.get("name", "")),
                phone=str(row.get("phone", "")),
                city=str(row.get("city", ""))
            ))

    # 2. Seed Trips
    trips_path = os.path.join(data_dir, "trips.csv")
    if os.path.exists(trips_path):
        import math
        df_trips = pd.read_csv(trips_path)
        df_trips.drop_duplicates(subset=['trip_id'], inplace=True)  # ADDED FIX
        for _, row in df_trips.iterrows():
            dist = float(row.get("distance_km", 0.0))
            if dist > 100.0:
                dist = dist / 1000.0
            surge = float(row.get("surge_multiplier", 1.0))
            status = str(row.get("status", ""))
            if status == "completed":
                extra = max(0.0, dist - 2.0)
                calc_fare = float(math.floor((25.0 + extra * 6.0) * surge + 0.5))
            else:
                calc_fare = 0.0
            fare = float(row.get("fare", calc_fare)) if status == "completed" else 0.0
            date_val = str(row.get("date") or row.get("started_at", ""))
            db.merge(Trip(
                trip_id=str(row.get("trip_id")),
                rider_id=str(row.get("rider_id")),
                date=date_val,
                distance_km=dist,
                fare=fare,
                status=status
            ))

    # 3. Seed Payout Lines
    payout_path = os.path.join(data_dir, "payout_lines.csv")
    if os.path.exists(payout_path):
        df_payout = pd.read_csv(payout_path)
        df_payout.drop_duplicates(subset=['line_id'], inplace=True)  # ADDED FIX
        for _, row in df_payout.iterrows():
            cat = str(row.get("category") or row.get("line_type", ""))
            date_val = str(row.get("date") or row.get("payout_date", ""))
            db.merge(PayoutLine(
                line_id=str(row.get("line_id")),
                rider_id=str(row.get("rider_id")),
                trip_id=str(row.get("trip_id")) if pd.notna(row.get("trip_id")) else None,
                amount=float(row.get("amount", 0.0)),
                category=cat,
                date=date_val
            ))

    # Commit all seeded records and close session
    db.commit()
    db.close()
    print("Database seeding completed successfully.")

if __name__ == "__main__":
    seed_database()