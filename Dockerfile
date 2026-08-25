FROM python:3.11-slim

RUN useradd -m -u 1000 user
WORKDIR /app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .

USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

ENV PORT=10000
EXPOSE 10000

CMD gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 webapp:app
