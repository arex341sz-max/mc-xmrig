FROM python:3.11-slim

RUN pip install requests psycopg2-binary

COPY logger.py .

CMD ["python", "logger.py"]
