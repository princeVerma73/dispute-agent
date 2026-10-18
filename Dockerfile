FROM python:3.11-slim

WORKDIR /app

# Install curl for container healthchecking and debugging
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Automatically run database seeding from CSVs on startup before launching Uvicorn
CMD ["sh", "-c", "python -m app.db.seed && uvicorn app.main:app --host 0.0.0.0 --port 8000"]