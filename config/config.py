# config/settings.py
from dotenv import load_dotenv
import os

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL")
EVOLUTION_API_TOKEN = os.getenv("EVOLUTION_API_TOKEN")
EVOLUTION_INSTANCE_NAME = os.getenv("EVOLUTION_INSTANCE_NAME")
ASAAS_SANDBOX_URL = os.getenv("ASAAS_SANDBOX_URL")
ASAAS_API_KEY = os.getenv("ASAAS_API_KEY")