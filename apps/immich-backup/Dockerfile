FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt /app/

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy scripts
COPY backup.py verify-restore.py /app/

# Make scripts executable
RUN chmod +x /app/backup.py /app/verify-restore.py

# Default command
CMD ["/app/backup.py"]

