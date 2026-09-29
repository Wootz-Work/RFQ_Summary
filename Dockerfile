FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered output for live log streaming
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PORT=8000

WORKDIR /app

# Install system utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency specifications first to leverage Docker layer caching
COPY requirements.txt setup.py pyproject.toml ./

# Upgrade pip and install Python dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy source code, prompts, and scripts
COPY src/ ./src/
COPY prompts/ ./prompts/
COPY scripts/ ./scripts/

# Install the local package in editable mode
RUN pip install --no-cache-dir -e .

# Expose default application port
EXPOSE 8000

# Start the application
CMD ["python", "-m", "rfq_summary.run"]
