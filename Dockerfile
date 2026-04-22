FROM python:3.11-slim

WORKDIR /app

# Prevent Python from writing .pyc files and enable unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all files
COPY . .

# Make start script executable
RUN chmod +x /app/start.sh

# Expose ports (Railway only uses one, but this is fine)
EXPOSE 8000 8501

# Start both bot + dashboard
CMD ["bash", "/app/start.sh"]
