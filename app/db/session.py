import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base

# PostgreSQL connection URL
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://user:password@localhost:5432/dispute_agent")

# Fallback: If URL uses 'db'/'postgres' host but running locally without docker network DNS,
# gracefully fallback to localhost.
import socket
for host in ["db", "postgres"]:
    if f"@{host}:" in DATABASE_URL:
        try:
            socket.gethostbyname(host)
        except socket.gaierror:
            DATABASE_URL = DATABASE_URL.replace(f"@{host}:", "@localhost:")
            break

connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()