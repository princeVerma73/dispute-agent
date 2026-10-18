import logging
import os
import pandas as pd
from app.db.session import SessionLocal
from app.db.models import init_db, Rider, Trip, PayoutLine
from app.agent.calculator import normalize_rider_id, to_ist_date

logger = logging.getLogger(__name__)


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
        # Normalise rider IDs to canonical form (R007, R019, etc.)
        df_riders["rider_id"] = df_riders["rider_id"].apply(
            lambda x: normalize_rider_id(str(x))
        )
        df_riders.drop_duplicates(subset=["rider_id"], inplace=True)
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
        df_trips = pd.read_csv(trips_path)

        # Normalise rider IDs
        df_trips["rider_id"] = df_trips["rider_id"].apply(
            lambda x: normalize_rider_id(str(x))
        )

        # Check for duplicate trip_ids and log a warning if duplicate rows differ
        dup_mask = df_trips.duplicated(subset=["trip_id"], keep=False)
        if dup_mask.any():
            dup_df = df_trips[dup_mask]
            for tid, group in dup_df.groupby("trip_id"):
                non_id_cols = [c for c in group.columns if c != "trip_id"]
                if group[non_id_cols].drop_duplicates().shape[0] > 1:
                    logger.warning(
                        f"Duplicate rows for trip_id {tid} have differing data: {group.to_dict(orient='records')}"
                    )

        # Dedupe trips on trip_id only
        df_trips.drop_duplicates(subset=["trip_id"], keep="first", inplace=True)

        for _, row in df_trips.iterrows():
            raw_ts = str(row.get("started_at") or row.get("date", ""))
            ist_day = to_ist_date(raw_ts)  # YYYY-MM-DD in IST

            # Raw unconverted distance from CSV (normalize_distance called at calculation time)
            raw_dist = float(row.get("distance_km", 0.0))
            surge = float(row.get("surge_multiplier", 1.0))
            status = str(row.get("status", ""))

            db.merge(Trip(
                trip_id=str(row.get("trip_id")),
                rider_id=str(row.get("rider_id")),
                date=ist_day,                # store the IST calendar date
                distance_km_raw=raw_dist,    # unconverted raw distance from CSV
                status=status,
                surge_multiplier=surge,      # store surge so calculator can use it
            ))

    # 3. Seed Payout Lines
    payout_path = os.path.join(data_dir, "payout_lines.csv")
    if os.path.exists(payout_path):
        df_payout = pd.read_csv(payout_path)

        # Normalise rider IDs
        df_payout["rider_id"] = df_payout["rider_id"].apply(
            lambda x: normalize_rider_id(str(x))
        )

        df_payout.drop_duplicates(subset=["line_id"], inplace=True)
        for _, row in df_payout.iterrows():
            cat = str(row.get("line_type") or row.get("category", ""))
            date_val = str(row.get("payout_date") or row.get("date", ""))
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