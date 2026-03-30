FROM python:3.11-slim

WORKDIR /app
COPY gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY gateway /app/gateway

ENV PORT=8080
EXPOSE 8080

CMD ["python", "gateway/app.py"]
