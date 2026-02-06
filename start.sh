#!/bin/bash

echo "🚀 Starting Deployment..."

# Update packages
apt-get update -y

# Install system dependencies
apt-get install -y ffmpeg aria2

# Upgrade pip
pip install --upgrade pip

# Install Python requirements
pip install -r requirements.txt

# Run FastAPI server
uvicorn Api:app --host 0.0.0.0 --port ${PORT:-8080}