FROM python:3.9-slim-bullseye

ENV PYTHONUNBUFFERED=TRUE

RUN mkdir -p /app
ADD . /app/
WORKDIR /app

RUN pip install --no-cache-dir -r requirements.txt
RUN opentelemetry-bootstrap -a install

ENV OTEL_SERVICE_NAME=ts-ai-anomaly-detector

EXPOSE 8080
CMD ["opentelemetry-instrument", "gunicorn", "-w", "2", "-b", "0.0.0.0:8080", "app:app"]
