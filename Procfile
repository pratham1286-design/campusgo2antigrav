web: if [ "$SEED_DEMO_DATA" = "1" ]; then python backend/seed_data.py --if-empty; fi && gunicorn --chdir backend --workers 1 --threads 4 --bind 0.0.0.0:$PORT app:app
