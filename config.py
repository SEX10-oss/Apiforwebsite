# config.py
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "YOUR_POSTGRES_DATABASE_URL")
WORKER_SECRET_TOKEN = os.getenv("WORKER_SECRET_TOKEN", "YOUR_WEBSITE_SECRET_TOKEN")
