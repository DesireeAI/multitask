from dotenv import load_dotenv
import os

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL")
EVOLUTION_ADMIN_API_KEY = os.getenv("EVOLUTION_ADMIN_API_KEY")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET")
