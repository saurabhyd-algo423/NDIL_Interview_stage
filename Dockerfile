# Dockerfile — Builds the production container image: installs Python deps, copies backend + frontend code, and runs the Flask app (app.py) on port 5000.
# Use official Python runtime as a parent image
FROM python:3.10-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies (e.g. for building reportlab, azure-speech-sdk, etc.)
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libffi-dev \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy only the requirements first to leverage Docker cache
COPY requirements.txt .

# Fix invalid pip requirements format (change '=' to '==' if present)
RUN sed -i 's/=\([0-9]\)/==\1/g' requirements.txt

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the backend code
COPY backend/ ./backend/

# Copy the frontend code (served by the backend Flask app)
COPY frontend/ ./frontend/

# Expose port 5000 (Flask default)
EXPOSE 5000

# Set working directory to the backend so app.py can resolve sibling paths
WORKDIR /app/backend

# Command to run the application
CMD ["python", "app.py"]
