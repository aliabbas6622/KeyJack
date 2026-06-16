import os
from dotenv import load_dotenv

load_dotenv()

MASTER_API_KEY = os.getenv("MASTER_API_KEY", "ali-super-secret-master-key")
PORT = int(os.getenv("PORT", 8000))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./keyjack.db")
