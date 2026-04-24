# Dockerfile
FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

WORKDIR /app

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ✅ Chromium + deps already included in base image - NO install needed!

# Copy app code
COPY . .

# Render forwards to $PORT (default: 8000) [[7]]
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]