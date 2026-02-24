FROM python:3.11-slim

WORKDIR /app

# Browser dependencies for captcha solving
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    fonts-liberation \
    libnss3 \
    libxss1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
# Install Playwright browsers for captcha
RUN python -m playwright install chromium 2>/dev/null || true

COPY . .

EXPOSE 8000

CMD ["python", "main.py"]
