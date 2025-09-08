FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN apt-get update && apt-get install -y curl build-essential ca-certificates git gcc libxml2 libxslt1.1 libxslt1-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY . .

ENV FLASK_APP=app.py
EXPOSE 5000
CMD ["python", "app.py"]
