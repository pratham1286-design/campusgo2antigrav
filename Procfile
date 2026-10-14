web: python backend/database.py && if [ "$SEED_DEMO_DATA" = "1" ]; then python backend/seed_data.py --if-empty; fi && gunicorn --config backend/gunicorn.conf.py
