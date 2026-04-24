# Use official Playwright Python image with all dependencies
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY scraper.py .

# Create directory for auth state (ephemeral on free tiers)
RUN mkdir -p /app/data

# Environment variables
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PYTHONUNBUFFERED=1

# Expose port
EXPOSE 8000

# Health check (optional but recommended)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/docs')" || exit 1

# Run the app
CMD ["uvicorn", "scraper:app", "--host", "0.0.0.0", "--port", "8000"]