FROM python:3.11-slim

# Το Hugging Face περιμένει μη-root χρήστη με UID 1000
RUN useradd -m -u 1000 user
WORKDIR /app

# Πρώτα τα requirements (για γρηγορότερα rebuilds όταν αλλάζει μόνο ο κώδικας)
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Μετά όλος ο κώδικας + τα δεδομένα (main.py, config.py, loader.py, normalize.py,
# pipeline.py, audit.py, webapp.py, και ο φάκελος data/ με τα Excel αρχεία)
COPY --chown=user . .

USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

EXPOSE 7860

# gunicorn αντί για τον ενσωματωμένο server του Flask — πιο σταθερό για δημόσια
# κίνηση. Το webapp:app σημαίνει "το αντικείμενο app μέσα στο webapp.py".
CMD ["gunicorn", "--bind", "0.0.0.0:7860", "--workers", "1", "--threads", "4", "--timeout", "120", "webapp:app"]
