from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Dict, Optional, Any
from supabase import AsyncClient, create_async_client
from config.config import SUPABASE_URL, SUPABASE_KEY, EVOLUTION_API_URL, OPENAI_API_KEY, SUPABASE_JWT_SECRET
import jwt
import os
import aiohttp
from openai import AsyncOpenAI
from datetime import datetime, timedelta
from tools.supabase_tools import get_lead, upsert_lead
from tools.whatsapp_tools import send_whatsapp_message, send_whatsapp_audio, send_whatsapp_image, fetch_media_base64
from tools.audio_tools import text_to_speech
from tools.image_tools import analyze_image
from tools.asaas_tools import create_customer, create_payment_link, get_customer_by_cpf
from tools.klingo_tools import fetch_procedure_price 
from tools.extract_lead_info import extract_lead_info
from utils.image_processing import resize_image_to_thumbnail
from models.lead_data import LeadData
from bot_agents.triage_agent import initialize_triage_agent
from bot_agents.appointment_agent import start_appointment_reminder
from agents import Runner
from utils.logging_setup import setup_logging
import asyncio
import re
import json
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from openai import RateLimitError
from uuid import UUID
import base64


logger = setup_logging()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "https://evolution-front.6bdhzg.easypanel.host"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Log requests and responses
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"Request: {request.method} {request.url}")
    response = await call_next(request)
    logger.info(f"Response headers: {response.headers}")
    return response

# Authentication dependency
async def get_current_user(request: Request) -> Dict:
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    logger.debug(f"Received token: {token[:10]}... (full length: {len(token)})")
    if not token:
        logger.error("No token provided in Authorization header")
        raise HTTPException(status_code=401, detail="No token provided")
    try:
        jwt_secret = os.getenv("SUPABASE_JWT_SECRET")
        if not jwt_secret:
            logger.error("SUPABASE_JWT_SECRET is not set in environment variables")
            raise HTTPException(status_code=500, detail="Server configuration error: JWT secret missing")
        logger.debug(f"SUPABASE_JWT_SECRET (first 10 chars): {jwt_secret[:10]}... (length: {len(jwt_secret)})")
        payload = jwt.decode(
            token,
            jwt_secret,
            algorithms=["HS256"],
            options={"verify_aud": False, "leeway": 60}
        )
        logger.debug(f"Decoded JWT payload: {payload}")
        return {"user_id": payload["sub"], "email": payload["email"]}
    except jwt.ExpiredSignatureError as e:
        logger.error(f"JWT expired: {str(e)}")
        raise HTTPException(status_code=401, detail=f"Invalid token: JWT expired")
    except jwt.InvalidSignatureError as e:
        logger.error(f"Invalid JWT signature: {str(e)}")
        raise HTTPException(status_code=401, detail=f"Invalid token: Invalid signature")
    except Exception as e:
        logger.error(f"Invalid token: {str(e)}")
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

# Supabase client dependency
async def get_supabase_client() -> AsyncClient:
    return await create_async_client(SUPABASE_URL, SUPABASE_KEY)

# Pydantic models
class ClinicProfileUpdate(BaseModel):
    name: str
    asaas_api_key: Optional[str]
    klingo_api_key: Optional[str]
    address: Optional[str]
    recommendations: Optional[str]
    support_phone: Optional[str]
    asaas_enabled: bool
    klingo_enabled: bool
    attendance_agent_enabled: bool
    scheduling_agent_enabled: bool
    payment_agent_enabled: bool
    reminder_agent_enabled: bool
    initial_message_enabled: bool
    offered_services_enabled: bool

class AgentPromptUpdate(BaseModel):
    id: Optional[UUID]  # Permitir id como opcional para novos prompts
    name: str
    prompt: str
    variables: List[str]
    enabled: bool
    clinic_id: Optional[UUID] = None  # Adicionado para garantir compatibilidade

class OperatingHoursUpdate(BaseModel):
    day: str
    enabled: bool
    start_time: Optional[str]
    end_time: Optional[str]

class LeadUpdate(BaseModel):
    status: str

class CreateInstanceRequest(BaseModel):
    instance_name: str
    phone_number: str
    type: str

class ClinicCreate(BaseModel):
    name: str
    assistant_name: Optional[str]
    address: Optional[str]
    support_phone: Optional[str]

# Lista de prompts padrão
DEFAULT_PROMPTS = [
    {
        "name": "Attendance Agent",
        "prompt": "Atenda o cliente {client_name} com informações sobre {service_name}.",
        "variables": ["{client_name}", "{service_name}"],
        "enabled": True,
    },
    {
        "name": "Scheduling Agent",
        "prompt": "Agende uma consulta para {client_name} às {appointment_time} em {appointment_date}.",
        "variables": ["{client_name}", "{appointment_time}", "{appointment_date}"],
        "enabled": True,
    },
    {
        "name": "Payment Agent",
        "prompt": "Processar pagamento para {client_name} pelo serviço {service_name}.",
        "variables": ["{client_name}", "{service_name}"],
        "enabled": False,
    },
    {
        "name": "Reminder Agent",
        "prompt": "Lembre {client_name} da consulta às {appointment_time} em {appointment_date}.",
        "variables": ["{client_name}", "{appointment_time}", "{appointment_date}"],
        "enabled": True,
    },
    {
        "name": "Initial Message",
        "prompt": "Bem-vindo(a) ao {clinic_name}, {client_name}! {greeting} Como posso ajudar você hoje?",
        "variables": ["{clinic_name}", "{client_name}", "{greeting}"],
        "enabled": True,
    },
    {
        "name": "Offered Services",
        "prompt": "Nossos serviços incluem: {service_list}. Deseja mais informações?",
        "variables": ["{service_list}"],
        "enabled": True,
    },
]

# Existing thread handling
threads = {}
message_buffer = {}
BUFFER_TIMEOUT = 5
MAX_MESSAGES = 3
COMPLETE_KEYWORDS = ["consulta", "agendar", "exame", "marcar", "médico", "horário", "atendimento"]

def build_response_data(text: str, metadata: dict, intent: str = "scheduling") -> dict:
    base_metadata = {
        "intent": intent,
        "step": metadata.get("step", ""),
        "phone_number": metadata.get("phone_number", ""),
        "register_id": metadata.get("register_id"),
        "name": metadata.get("name"),
        "birth_date": metadata.get("birth_date"),
        "cpf": metadata.get("cpf"),
        "access_token": metadata.get("access_token"),
        "clinic_id": metadata.get("clinic_id")
    }
    base_metadata.update({k: v for k, v in metadata.items() if v is not None})
    return {"text": text, "metadata": base_metadata}

async def get_or_create_thread(user_id: str, push_name: Optional[str] = None, clinic_id: str = None) -> str:
    if not user_id or user_id == "unknown" or "@s.whatsapp.net" not in user_id:
        logger.error(f"Invalid user_id: {user_id}")
        raise ValueError("Invalid user_id")
    if user_id in threads:
        logger.debug(f"Reusing in-memory thread for user {user_id}: {threads[user_id]}")
        return threads[user_id]
    lead = await get_lead(user_id)
    if lead and "thread_id" in lead and lead["thread_id"]:
        threads[user_id] = lead["thread_id"]
        logger.debug(f"Reusing Supabase thread for user {user_id}: {lead['thread_id']}")
        if push_name and (not lead.get("nome_cliente") or not lead.get("pushname")):
            lead_data = LeadData(
                remotejid=user_id,
                nome_cliente=push_name,
                pushname=push_name,
                telefone=user_id.replace("@s.whatsapp.net", ""),
                data_cadastro=lead.get("data_cadastro", datetime.now().isoformat()),
                thread_id=lead["thread_id"],
                clinic_id=clinic_id
            )
            logger.debug(f"Updating nome_cliente and pushname for {user_id}: {push_name}")
            await upsert_lead(user_id, lead_data)
        return lead["thread_id"]
    thread = await client.beta.threads.create()
    threads[user_id] = thread.id
    logger.debug(f"Created new thread for user {user_id}: {thread.id}")
    lead_data = LeadData(
        remotejid=user_id,
        nome_cliente=push_name,
        pushname=push_name,
        telefone=user_id.replace("@s.whatsapp.net", ""),
        data_cadastro=datetime.now().isoformat(),
        thread_id=thread.id,
        clinic_id=clinic_id
    )
    logger.debug(f"Preparing to upsert lead data: {lead_data.dict(exclude_unset=True)}")
    await upsert_lead(user_id, lead_data)
    return thread.id

async def get_thread_history(thread_id: str, limit: int = 10) -> str:
    try:
        messages = await client.beta.threads.messages.list(thread_id=thread_id, limit=limit)
        history = []
        for msg in reversed(messages.data):
            role = msg.role
            content = msg.content[0].text.value if msg.content else ""
            history.append(f"{role.capitalize()}: {content}")
        return "\n".join(history) if history else "No previous messages."
    except Exception as e:
        logger.error(f"Error retrieving thread history for thread {thread_id}: {str(e)}")
        return "Error retrieving conversation history."

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10), retry=retry_if_exception_type(RateLimitError))
async def run_agent_with_retry(agent, input_data: str) -> dict:
    logger.debug(f"Running agent with input: {input_data}")
    response = await Runner.run(agent, input=input_data)
    response_data = str(response.final_output)
    if response_data.startswith("```json") and response_data.endswith("```"):
        response_data = response_data[7:-3].strip()
    try:
        return json.loads(response_data)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse agent response as JSON: {response_data}, Error: {str(e)}")
        raise

async def collect_messages(remote_jid: str, clinic_id: str, message: str, message_key_id: str, wait_time: float = 5.0, max_messages: int = 3) -> str:
    if remote_jid not in message_buffer:
        message_buffer[remote_jid] = {}
    if clinic_id not in message_buffer[remote_jid]:
        message_buffer[remote_jid][clinic_id] = {"messages": [], "timestamp": datetime.now(), "message_key_id": message_key_id}
    message_buffer[remote_jid][clinic_id]["messages"].append(message)
    message_buffer[remote_jid][clinic_id]["message_key_id"] = message_key_id
    logger.debug(f"[{remote_jid}] Buffered message for clinic {clinic_id}: '{message}', Current buffer: {message_buffer[remote_jid][clinic_id]['messages']}")

    if any(keyword in message.lower() for keyword in COMPLETE_KEYWORDS):
        logger.info(f"[{remote_jid}] Keyword detected in message '{message}', processing buffer immediately")
        return await flush_buffer(remote_jid, clinic_id)

    if len(message_buffer[remote_jid][clinic_id]["messages"]) >= max_messages:
        logger.info(f"[{remote_jid}] Max messages ({max_messages}) reached, processing buffer")
        return await flush_buffer(remote_jid, clinic_id)

    logger.debug(f"[{remote_jid}] Waiting {wait_time}s for more messages, buffer size: {len(message_buffer[remote_jid][clinic_id]['messages'])}")
    await asyncio.sleep(wait_time)
    if remote_jid in message_buffer and clinic_id in message_buffer[remote_jid]:
        if datetime.now() - message_buffer[remote_jid][clinic_id]["timestamp"] >= timedelta(seconds=wait_time):
            logger.info(f"[{remote_jid}] Buffer timeout ({wait_time}s) reached, processing buffer")
            return await flush_buffer(remote_jid, clinic_id)
    logger.debug(f"[{remote_jid}] No timeout or new messages, continuing to wait")
    return ""

async def flush_buffer(remote_jid: str, clinic_id: str) -> str:
    if remote_jid not in message_buffer or clinic_id not in message_buffer[remote_jid]:
        logger.debug(f"[{remote_jid}] No buffer found for clinic {clinic_id}")
        return ""
    messages = message_buffer[remote_jid][clinic_id]["messages"]
    combined = " ".join(messages)
    logger.info(f"[{remote_jid}] Flushing buffer for clinic {clinic_id}: '{combined}'")
    del message_buffer[remote_jid][clinic_id]
    if not message_buffer[remote_jid]:
        del message_buffer[remote_jid]
    return combined

async def send_response(phone_number: str, user_id: str, response_data: dict, prefer_audio: bool, message_key_id: str, is_audio_message: bool, message: str, clinic_id: str) -> bool:
    success = False
    logger.debug(f"[{user_id}] Sending response: {response_data['text']}")
    if prefer_audio and response_data.get("text"):
        audio_path = await text_to_speech(response_data["text"])
        if not audio_path.startswith("Erro"):
            success = await send_whatsapp_audio(
                phone_number=phone_number,
                audio_path=audio_path,
                remotejid=user_id,
                message_key_id=message_key_id,
                message_text=message if not is_audio_message else None,
                clinic_id=clinic_id
            )
            if os.path.exists(audio_path):
                os.remove(audio_path)
                logger.debug(f"Removed audio file: {audio_path}")
        else:
            logger.error(f"[{user_id}] Failed to generate audio: {audio_path}")
            response_data = build_response_data(
                text="Desculpe, houve um problema ao gerar o áudio. Como posso ajudar?",
                metadata={"intent": "error", "phone_number": phone_number, "clinic_id": response_data["metadata"]["clinic_id"]},
                intent="error"
            )
            paragraphs = response_data["text"].split("\n\n")
            for paragraph in paragraphs:
                if paragraph.strip():
                    success = await send_whatsapp_message(phone_number, paragraph.strip(), remotejid=user_id, clinic_id=clinic_id)
                    if not success:
                        logger.error(f"[{user_id}] Falha ao enviar parágrafo: {paragraph}")
                        break
                    await asyncio.sleep(0.5)
            success = success and True
    else:
        if response_data.get("text"):
            image_url_match = re.match(r'!\[.*?\]\((.*?)\)', response_data.get("text", ""))
            if image_url_match:
                image_url = image_url_match.group(1)
                caption = response_data.get("text", "").split("]")[0][2:] or "Imagem"
                success = await send_whatsapp_image(
                    phone_number=phone_number,
                    image_url=image_url,
                    caption=caption,
                    remotejid=user_id,
                    message_key_id=message_key_id,
                    message_text=message if not is_audio_message else None,
                    clinic_id=clinic_id
                )
                if success:
                    response_data = {"text": "", "metadata": response_data["metadata"]}
                else:
                    logger.error(f"[{user_id}] Falha ao enviar imagem: {image_url}")
                    response_data = build_response_data(
                        text="Desculpe, houve um problema ao enviar a imagem.",
                        metadata={"intent": "error", "phone_number": phone_number, "clinic_id": response_data["metadata"]["clinic_id"]},
                        intent="error"
                    )
            if response_data.get("text"):
                if "\n\n" in response_data["text"]:
                    segments = response_data["text"].split("\n\n")
                elif "\n" in response_data["text"]:
                    segments = response_data["text"].split("\n")
                else:
                    segments = re.split(r'(?<=[.!?])\s+', response_data["text"].strip())
                for i, segment in enumerate(segments):
                    segment = segment.strip()
                    if segment:
                        logger.info(f"[{user_id}] Sending segment {i+1}/{len(segments)}: {segment}")
                        success = await send_whatsapp_message(phone_number, segment, remotejid=user_id, clinic_id=clinic_id)
                        if not success:
                            logger.error(f"[{user_id}] Failed to send segment {i+1}: {segment}")
                            break
                        await asyncio.sleep(0.5)
                success = success and True
    return success

# Dashboard endpoints
@app.get("/leads")
async def get_leads(user: dict = Depends(get_current_user), supabase: AsyncClient = Depends(get_supabase_client)):
    try:
        clinic_user = await supabase.table("clinic_users").select("clinic_id").eq("user_id", user["user_id"]).execute()
        if not clinic_user.data:
            raise HTTPException(status_code=403, detail="User not associated with any clinic")
        clinic_id = clinic_user.data[0]["clinic_id"]
        response = await supabase.table("clients").select("*").eq("clinic_id", clinic_id).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/leads/{remotejid}")
async def update_lead(remotejid: str, data: LeadUpdate, user: dict = Depends(get_current_user), supabase: AsyncClient = Depends(get_supabase_client)):
    try:
        clinic_user = await supabase.table("clinic_users").select("clinic_id").eq("user_id", user["user_id"]).execute()
        if not clinic_user.data:
            raise HTTPException(status_code=403, detail="User not associated with any clinic")
        clinic_id = clinic_user.data[0]["clinic_id"]
        response = await supabase.table("clients").update(data.dict()).eq("remotejid", remotejid).eq("clinic_id", clinic_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail="Lead not found")
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/clinic/create")
async def create_clinic(data: ClinicCreate, user: dict = Depends(get_current_user), supabase: AsyncClient = Depends(get_supabase_client)):
    try:
        # Criar a clínica
        clinic_response = await supabase.table("clinics").insert({
            "name": data.name,
            "assistant_name": data.assistant_name,
            "address": data.address,
            "support_phone": data.support_phone,
        }).execute()
        if not clinic_response.data:
            raise HTTPException(status_code=400, detail="Failed to create clinic")
        clinic_id = clinic_response.data[0]["clinic_id"]

        # Associar o usuário à clínica
        await supabase.table("clinic_users").insert({
            "user_id": user["user_id"],
            "clinic_id": clinic_id,
        }).execute()

        # Inicializar prompts padrão
        default_prompts = [
            {
                "clinic_id": clinic_id,
                "name": prompt["name"],
                "prompt": prompt["prompt"],
                "variables": prompt["variables"],
                "enabled": prompt["enabled"],
            }
            for prompt in DEFAULT_PROMPTS
        ]
        await supabase.table("agent_prompts").insert(default_prompts).execute()

        return clinic_response.data
    except Exception as e:
        logger.error(f"Error creating clinic: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/clinic/profile")
async def get_clinic_profile(user: dict = Depends(get_current_user), supabase: AsyncClient = Depends(get_supabase_client)):
    try:
        clinic_user = await supabase.table("clinic_users").select("clinic_id").eq("user_id", user["user_id"]).execute()
        if not clinic_user.data:
            raise HTTPException(status_code=403, detail="User not associated with any clinic")
        clinic_id = clinic_user.data[0]["clinic_id"]
        response = await supabase.table("clinics").select("*").eq("clinic_id", clinic_id).single().execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/clinic/profile")
async def update_clinic_profile(data: ClinicProfileUpdate, user: dict = Depends(get_current_user), supabase: AsyncClient = Depends(get_supabase_client)):
    try:
        clinic_user = await supabase.table("clinic_users").select("clinic_id").eq("user_id", user["user_id"]).execute()
        if not clinic_user.data:
            raise HTTPException(status_code=403, detail="User not associated with any clinic")
        clinic_id = clinic_user.data[0]["clinic_id"]
        response = await supabase.table("clinics").update(data.dict(exclude_unset=True)).eq("clinic_id", clinic_id).execute()
        if not response.data:
            raise HTTPException(status_code=404, detail="Clinic not found")
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/clinic/prompts")
async def get_clinic_prompts(user: dict = Depends(get_current_user), supabase: AsyncClient = Depends(get_supabase_client)):
    try:
        clinic_user = await supabase.table("clinic_users").select("clinic_id").eq("user_id", user["user_id"]).execute()
        if not clinic_user.data:
            raise HTTPException(status_code=403, detail="User not associated with any clinic")
        clinic_id = clinic_user.data[0]["clinic_id"]

        # Buscar prompts existentes
        response = await supabase.table("agent_prompts").select("*").eq("clinic_id", clinic_id).execute()
        existing_prompts = response.data or []

        # Verificar quais prompts padrão estão faltando
        existing_prompt_names = {prompt["name"] for prompt in existing_prompts}
        missing_prompts = [prompt for prompt in DEFAULT_PROMPTS if prompt["name"] not in existing_prompt_names]

        # Criar prompts faltantes
        if missing_prompts:
            new_prompts = [
                {
                    "clinic_id": clinic_id,
                    "name": prompt["name"],
                    "prompt": prompt["prompt"],
                    "variables": prompt["variables"],
                    "enabled": prompt["enabled"],
                }
                for prompt in missing_prompts
            ]
            await supabase.table("agent_prompts").insert(new_prompts).execute()

            # Buscar novamente todos os prompts após a inserção
            response = await supabase.table("agent_prompts").select("*").eq("clinic_id", clinic_id).execute()

        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/clinic/prompts")
async def update_clinic_prompts(prompts: List[AgentPromptUpdate], user: dict = Depends(get_current_user), supabase: AsyncClient = Depends(get_supabase_client)):
    try:
        clinic_user = await supabase.table("clinic_users").select("clinic_id").eq("user_id", user["user_id"]).execute()
        if not clinic_user.data:
            raise HTTPException(status_code=403, detail="User not associated with any clinic")
        clinic_id = clinic_user.data[0]["clinic_id"]

        # Garantir que todos os prompts tenham o clinic_id correto
        cleaned_prompts = [
            {
                **prompt.dict(exclude_unset=True),
                "clinic_id": clinic_id,
                "id": str(prompt.id) if prompt.id else None,  # Permitir que o Supabase gere UUID para novos prompts
            }
            for prompt in prompts
        ]

        # Realizar upsert
        response = await supabase.table("agent_prompts").upsert(
            cleaned_prompts,
            on_conflict="id"
        ).execute()

        if not response.data:
            raise HTTPException(status_code=400, detail="Failed to update prompts")

        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/clinic/hours")
async def get_operating_hours(user: dict = Depends(get_current_user), supabase: AsyncClient = Depends(get_supabase_client)):
    try:
        clinic_user = await supabase.table("clinic_users").select("clinic_id").eq("user_id", user["user_id"]).execute()
        if not clinic_user.data:
            raise HTTPException(status_code=403, detail="User not associated with any clinic")
        clinic_id = clinic_user.data[0]["clinic_id"]
        response = await supabase.table("operating_hours").select("*").eq("clinic_id", clinic_id).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/clinic/hours")
async def update_operating_hours(hours: List[OperatingHoursUpdate], user: dict = Depends(get_current_user), supabase: AsyncClient = Depends(get_supabase_client)):
    try:
        clinic_user = await supabase.table("clinic_users").select("clinic_id").eq("user_id", user["user_id"]).execute()
        if not clinic_user.data:
            raise HTTPException(status_code=403, detail="User not associated with any clinic")
        clinic_id = clinic_user.data[0]["clinic_id"]
        response = await supabase.table("operating_hours").upsert(
            [{**h.dict(), "clinic_id": clinic_id} for h in hours],
            on_conflict="clinic_id,day"
        ).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/whatsapp/instances")
async def get_whatsapp_instances(user: dict = Depends(get_current_user), supabase: AsyncClient = Depends(get_supabase_client)):
    try:
        clinic_user = await supabase.table("clinic_users").select("clinic_id").eq("user_id", user["user_id"]).execute()
        if not clinic_user.data:
            raise HTTPException(status_code=403, detail="User not associated with any clinic")
        clinic_id = clinic_user.data[0]["clinic_id"]
        response = await supabase.table("clinic_instances").select("*").eq("clinic_id", clinic_id).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/whatsapp/instances/{instance_id}")
async def get_whatsapp_instance(instance_id: str, user: dict = Depends(get_current_user), supabase: AsyncClient = Depends(get_supabase_client)):
    try:
        clinic_user = await supabase.table("clinic_users").select("clinic_id").eq("user_id", user["user_id"]).execute()
        if not clinic_user.data:
            raise HTTPException(status_code=403, detail="User not associated with any clinic")
        clinic_id = clinic_user.data[0]["clinic_id"]
        response = await supabase.table("clinic_instances").select("*").eq("id", instance_id).eq("clinic_id", clinic_id).single().execute()
        if not response.data:
            raise HTTPException(status_code=404, detail="Instance not found")
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.options("/create-instance")
async def options_create_instance():
    return JSONResponse(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "http://localhost:5173",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Access-Control-Allow-Credentials": "true"
        }
    )

@app.post("/create-instance")
async def create_instance_endpoint(data: CreateInstanceRequest, user: dict = Depends(get_current_user), supabase: AsyncClient = Depends(get_supabase_client)):
    try:
        clinic_user = await supabase.table("clinic_users").select("clinic_id").eq("user_id", user["user_id"]).execute()
        if not clinic_user.data:
            logger.error(f"No clinic found for user {user['user_id']}")
            raise HTTPException(status_code=403, detail="User not associated with any clinic")
        clinic_id = clinic_user.data[0]["clinic_id"]
        if not all([EVOLUTION_API_URL, os.getenv("EVOLUTION_ADMIN_API_KEY")]):
            logger.error("EVOLUTION_API_URL or EVOLUTION_ADMIN_API_KEY not configured")
            raise HTTPException(status_code=500, detail="Evolution API configuration missing")
        
        instance_name = data.instance_name
        cleaned_phone_number = ''.join(filter(str.isdigit, data.phone_number))
        whatsapp_formatted_number = f"{cleaned_phone_number}@s.whatsapp.net"
        
        webhook_url = os.getenv("WEBHOOK_URL")
        if not webhook_url:
            logger.error("WEBHOOK_URL not configured in .env")
            raise HTTPException(status_code=500, detail="Webhook URL not configured")
        
        payload = {
            "instanceName": instance_name,
            "number": cleaned_phone_number,
            "qrcode": True,
            "integration": data.type,
            "rejectCall": True,
            "groupsIgnore": True,
            "alwaysOnline": True,
            "readMessages": True,
            "webhook": {
                "url": webhook_url,
                "byEvents": False,
                "base64": True,
                "headers": {
                    "authorization": f"Bearer {os.getenv('WEBHOOK_AUTH_TOKEN', '')}",
                    "Content-Type": "application/json"
                },
                "events": ["MESSAGES_UPSERT"]
            }
        }
        headers = {
            "apikey": os.getenv("EVOLUTION_ADMIN_API_KEY"),
            "Content-Type": "application/json"
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(f"{EVOLUTION_API_URL}/instance/create", json=payload) as response:
                response_text = await response.text()
                logger.debug(f"Instance creation response: {response.status} - {response_text}")
                if not response.ok:
                    logger.error(f"Failed to create instance: {response.status} - {response_text}")
                    raise HTTPException(status_code=500, detail=f"Failed to create instance: {response.status} - {response_text[:100]}...")
                content_type = response.headers.get("content-type", "")
                if not content_type.startswith("application/json"):
                    logger.error(f"Non-JSON response from Evolution API: {response_text[:100]}...")
                    raise HTTPException(status_code=500, detail=f"Non-JSON response from Evolution API: {response_text[:100]}...")
                response_data = await response.json()
                logger.debug(f"Evolution API response: {json.dumps(response_data, indent=2)}")
        
        # Use instanceId from Evolution API response
        api_key = response_data.get("instance", {}).get("instanceId")
        if not api_key:
            logger.error(f"No instanceId in Evolution API response: {json.dumps(response_data)}")
            raise HTTPException(status_code=500, detail="No instanceId in Evolution API response")
        
        qr_code = response_data.get("qrcode", {}).get("base64", "")
        logger.debug(f"QR code data: length={len(qr_code)}, starts_with_data_image={qr_code.startswith('data:image/')}")
        
        instance_data = {
            "clinic_id": clinic_id,
            "instance_name": instance_name,
            "api_key": api_key,  # Use instanceId
            "phone_number": data.phone_number,
            "status": "connecting",
            "qr_code": qr_code,
        }
        response = await supabase.table("clinic_instances").insert(instance_data).execute()
        logger.info(f"Instance created for clinic {clinic_id}: {instance_name} with api_key {api_key}")
        
        whatsapp_number_data = {
            "phone_number": whatsapp_formatted_number,
            "clinic_id": clinic_id
        }
        try:
            await supabase.table("whatsapp_numbers").insert(whatsapp_number_data).execute()
            logger.info(f"WhatsApp number {whatsapp_formatted_number} added for clinic {clinic_id}")
        except Exception as e:
            if "duplicate key value" not in str(e).lower():
                logger.error(f"Error inserting into whatsapp_numbers: {str(e)}")
            else:
                logger.info(f"WhatsApp number {whatsapp_formatted_number} already exists for clinic {clinic_id}")
        
        return response.data[0]
    except Exception as e:
        logger.error(f"Error creating instance: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error creating instance: {str(e)}")

@app.delete("/delete-instance/{instance_id}")
async def delete_instance_endpoint(instance_id: str, user: dict = Depends(get_current_user), supabase: AsyncClient = Depends(get_supabase_client)):
    try:
        clinic_user = await supabase.table("clinic_users").select("clinic_id").eq("user_id", user["user_id"]).execute()
        if not clinic_user.data:
            raise HTTPException(status_code=403, detail="User not associated with any clinic")
        clinic_id = clinic_user.data[0]["clinic_id"]
        
        instance = await supabase.table("clinic_instances").select("*").eq("api_key", instance_id).eq("clinic_id", clinic_id).single().execute()
        if not instance.data:
            raise HTTPException(status_code=404, detail="Instance not found")
        
        headers = {
            "apikey": os.getenv("EVOLUTION_ADMIN_API_KEY"),
            "Content-Type": "application/json"
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.delete(f"{EVOLUTION_API_URL}/instance/delete/{instance_id}") as response:
                response_text = await response.text()
                logger.debug(f"Instance deletion response: {response.status} - {response_text}")
                if response.status not in (200, 204):
                    logger.error(f"Failed to delete instance: {response.status} - {response_text}")
                    raise HTTPException(status_code=500, detail=f"Failed to delete instance: {response_text}")
        
        await supabase.table("clinic_instances").delete().eq("api_key", instance_id).eq("clinic_id", clinic_id).execute()
        logger.info(f"Instance {instance_id} deleted from clinic_instances")
        
        cleaned_phone_number = ''.join(filter(str.isdigit, instance.data["phone_number"]))
        whatsapp_formatted_number = f"{cleaned_phone_number}@s.whatsapp.net"
        await supabase.table("whatsapp_numbers").delete().eq("phone_number", whatsapp_formatted_number).eq("clinic_id", clinic_id).execute()
        logger.info(f"WhatsApp number {whatsapp_formatted_number} deleted for clinic {clinic_id}")
        
        return {"status": "success", "message": f"Instance {instance_id} deleted successfully"}
    except Exception as e:
        logger.error(f"Error deleting instance: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error deleting instance: {str(e)}")

@app.post("/verify-instance")
async def verify_instance_endpoint(data: dict, user: dict = Depends(get_current_user), supabase: AsyncClient = Depends(get_supabase_client)):
    try:
        api_key = data.get("api_key")
        if not api_key:
            raise HTTPException(status_code=400, detail="api_key is required")
        
        clinic_user = await supabase.table("clinic_users").select("clinic_id").eq("user_id", user["user_id"]).execute()
        if not clinic_user.data:
            raise HTTPException(status_code=403, detail="User not associated with any clinic")
        clinic_id = clinic_user.data[0]["clinic_id"]
        
        instance = await supabase.table("clinic_instances").select("*").eq("api_key", api_key).eq("clinic_id", clinic_id).single().execute()
        if not instance.data:
            raise HTTPException(status_code=404, detail="Instance not found")
        
        headers = {
            "apikey": os.getenv("EVOLUTION_ADMIN_API_KEY"),
            "Content-Type": "application/json"
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(f"{EVOLUTION_API_URL}/instance/connectionState/{api_key}") as response:
                response_text = await response.text()
                logger.debug(f"Instance verification response: {response.status} - {response_text}")
                if response.status not in (200, 201):
                    logger.error(f"Failed to verify instance: {response.status} - {response_text}")
                    raise HTTPException(status_code=500, detail=f"Failed to verify instance: {response_text}")
                response_data = await response.json()
        
        evolution_status = response_data.get("instance", {}).get("state", "disconnected")
        status = "disconnected"
        if evolution_status == "open":
            status = "connected"
        elif evolution_status == "connecting":
            status = "connecting"
        
        update_data = {"status": status}
        if status == "connected" or (instance.data.get("created_at") and 
                                    (datetime.now() - datetime.fromisoformat(instance.data["created_at"].replace("Z", "+00:00"))).total_seconds() > 60):
            update_data["qr_code"] = None
        
        await supabase.table("clinic_instances").update(update_data).eq("api_key", api_key).eq("clinic_id", clinic_id).execute()
        
        return {"api_key": api_key, "status": status}
    except Exception as e:
        logger.error(f"Error verifying instance: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error verifying instance: {str(e)}")

@app.on_event("startup")
async def startup_event():
    logger.info("Disparando tarefa de lembrete de agendamentos...")
    asyncio.create_task(start_appointment_reminder())

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        logger.info(f"Payload recebido: {json.dumps(data, ensure_ascii=False)}")

        sender_number = data.get("sender", "")
        if not sender_number or "@s.whatsapp.net" not in sender_number:
            logger.error(f"Invalid sender number: {sender_number}")
            return {"status": "error", "message": "Invalid sender number"}

        supabase_client: AsyncClient = await create_async_client(SUPABASE_URL, SUPABASE_KEY)
        response = await supabase_client.table("whatsapp_numbers").select("clinic_id").eq("phone_number", sender_number).execute()
        if not response.data:
            logger.error(f"No clinic found for phone number: {sender_number}")
            return {"status": "error", "message": "Clinic not found"}
        clinic_id = response.data[0]["clinic_id"]
        logger.info(f"Clinic found: {clinic_id} for sender number: {sender_number}")

        try:
            rpc_response = await supabase_client.rpc("set_current_clinic_id", {"clinic_id": clinic_id}).execute()
            logger.debug(f"RPC response: {rpc_response}")
        except Exception as e:
            logger.error(f"Error in RPC call: {str(e)}")
            return {"status": "error", "message": f"Error setting clinic_id: {str(e)}"}

        triage_agent_instance = await initialize_triage_agent(clinic_id)
        user_id = data.get("data", {}).get("key", {}).get("remoteJid", "")
        phone_number = user_id
        push_name = data.get("data", {}).get("pushName", None)
        message_key_id = data.get("data", {}).get("key", {}).get("id", "")
        logger.debug(f"Extracted phone_number: {phone_number}, user_id: {user_id}, pushName: {push_name}")

        if not phone_number or not user_id or user_id == "unknown" or "@s.whatsapp.net" not in user_id:
            logger.warning("Nenhum número de telefone ou user_id válido encontrado no payload")
            return {"status": "error", "message": "No valid phone number or user_id found"}

        klingo_phone = user_id.replace("55", "").split("@")[0]
        if klingo_phone.isdigit():
            if len(klingo_phone) == 10:
                klingo_phone = f"{klingo_phone[:2]}9{klingo_phone[2:]}"
            elif len(klingo_phone) != 11:
                logger.error(f"[{user_id}] Invalid phone number length: {klingo_phone}")
                klingo_phone = None
        else:
            logger.error(f"[{user_id}] Invalid remotejid format for Klingo phone derivation: {klingo_phone}")
            klingo_phone = None

        thread_id = await get_or_create_thread(user_id, push_name=push_name, clinic_id=clinic_id)
        thread_history = await get_thread_history(thread_id)
        logger.debug(f"Thread history for {thread_id}: {thread_history}")

        message_data = data.get("data", {}).get("message", {})
        message = None
        is_audio_message = False
        is_image_message = False
        prefer_audio = False
        response_data = build_response_data(
            text="Desculpe, houve um problema ao processar sua mensagem. Como posso ajudar?",
            metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
            intent="error"
        )

        if message_data.get("conversation"):
            message = message_data["conversation"]
            prefer_audio = "responda em áudio" in message.lower()
            name_match = re.search(r'nome:\s*([^\n]+)', message, re.IGNORECASE)
            if name_match:
                user_provided_name = name_match.group(1).strip().capitalize()
            message = await collect_messages(user_id, clinic_id, message, message_key_id, wait_time=BUFFER_TIMEOUT, max_messages=MAX_MESSAGES)
            if not message:
                logger.info(f"[{user_id}] Waiting for more messages, returning early")
                return {"status": "success", "message": "Waiting for more messages"}
        elif message_data.get("audioMessage"):
            is_audio_message = True
            media_result = await fetch_media_base64(message_key_id, "audio", user_id, clinic_id)
            if "error" in media_result:
                logger.error(f"[{user_id}] Falha ao processar áudio: {media_result['error']}")
                response_data = build_response_data(
                    text=f"Falha ao processar áudio: {media_result['error']}",
                    metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                    intent="error"
                )
                success = await send_response(phone_number, user_id, response_data, prefer_audio=False, message_key_id=message_key_id, is_audio_message=True, message=None, clinic_id=clinic_id)
                return {"status": "success" if success else "error", "message": "Processed and responded" if success else "Failed to send response"}
            elif media_result.get("type") == "audio":
                message = media_result["transcription"]
                logger.info(f"Transcribed audio to: {message}")
                prefer_audio = True
        elif message_data.get("imageMessage"):
            is_image_message = True
            logger.info(f"[{user_id}] Buscando imagem completa via fetch_media_base64")
            try:
                media_result = await fetch_media_base64(message_key_id, "image", user_id, clinic_id)
                if "error" in media_result:
                    logger.error(f"[{user_id}] Falha ao buscar imagem completa: {media_result['error']}")
                    response_data = build_response_data(
                        text=f"Falha ao buscar imagem completa: {media_result['error']}",
                        metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                        intent="error"
                    )
                    success = await send_response(phone_number, user_id, response_data, prefer_audio=False, message_key_id=message_key_id, is_audio_message=False, message=None, clinic_id=clinic_id)
                    return {"status": "success" if success else "error", "message": "Processed and responded" if success else "Failed to send response"}
                elif media_result.get("type") == "image":
                    base64_data = media_result["base64"]
                    mimetype = media_result["mimetype"]
                    logger.debug(f"[{user_id}] Imagem completa obtida, mimetype: {mimetype}, tamanho base64: {len(base64_data)}")
                    decoded_data = base64.b64decode(base64_data)
                    resized_base64 = await resize_image_to_thumbnail(decoded_data, max_size=512)
                    if not resized_base64:
                        logger.error(f"[{user_id}] Falha ao redimensionar imagem")
                        response_data = build_response_data(
                            text="Falha ao redimensionar imagem. Por favor, envie outra imagem ou descreva sua solicitação.",
                            metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                            intent="error"
                        )
                        success = await send_response(phone_number, user_id, response_data, prefer_audio=False, message_key_id=message_key_id, is_audio_message=False, message=None, clinic_id=clinic_id)
                        return {"status": "success" if success else "error", "message": "Processed and responded" if success else "Failed to send response"}
                    image_description = await analyze_image(content=resized_base64, mimetype=mimetype)
                    try:
                        image_data = json.loads(image_description)
                        if image_data.get("is_medical_document"):
                            lead_data = LeadData(remotejid=user_id, clinic_id=clinic_id)
                            if image_data.get("patient_name") != "Não identificado":
                                lead_data.nome_cliente = image_data["patient_name"]
                            if image_data.get("doctor_name") != "Não identificado":
                                lead_data.medico = image_data["doctor_name"]
                            if image_data.get("medications") != ["Não identificado"]:
                                lead_data.sintomas = ", ".join(image_data["medications"])
                            lead_data.ult_contato = datetime.now().isoformat()
                            await upsert_lead(user_id, lead_data)
                            logger.debug(f"[{user_id}] Prescription data saved: {lead_data.dict(exclude_unset=True)}")
                            message = (
                                f"Prescrição recebida:\n"
                                f"- Paciente: {image_data.get('patient_name', 'Não identificado')}\n"
                                f"- Médico: {image_data.get('doctor_name', 'Não identificado')}\n"
                                f"- Medicamentos: {', '.join(image_data.get('medications', ['Não identificado']))}\n"
                                f"- Data: {image_data.get('document_date', 'Não identificado')}\n"
                                f"Por favor, confirme os dados e informe a cidade/estado e data/horário preferido para agendamento."
                            )
                        else:
                            message = image_data.get("details", "Imagem não reconhecida como documento médico.")
                    except json.JSONDecodeError as e:
                        logger.error(f"[{user_id}] Resposta de análise de imagem não é JSON: {image_description}, Erro: {str(e)}")
                        message = "Erro ao processar a imagem. Por favor, envie uma prescrição válida."
                    message = f"{message}\n\nHistórico da conversa:\n{thread_history}"
                    logger.info(f"[{user_id}] Imagem analisada, mensagem gerada: {message}")

            except Exception as e:
                logger.error(f"[{user_id}] Erro ao processar imagem: {str(e)}")
                response_data = build_response_data(
                    text=f"Erro ao processar imagem: {str(e)}",
                    metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                    intent="error"
                )
                success = await send_response(phone_number, user_id, response_data, prefer_audio=False, message_key_id=message_key_id, is_audio_message=False, message=None, clinic_id=clinic_id)
                return {"status": "success" if success else "error", "message": "Processed and responded" if success else "Failed to send response"}

        if not message:
            response_data = build_response_data(
                text="Oi! Bem-vindo(a) à nossa clínica. Como posso ajudar com seu agendamento ou dúvidas sobre consultas?",
                metadata={"intent": "greeting", "phone_number": klingo_phone, "clinic_id": clinic_id, "step": "greet"},
                intent="greeting"
            )
            success = await send_response(phone_number, user_id, response_data, prefer_audio=False, message_key_id=message_key_id, is_audio_message=False, message=None, clinic_id=clinic_id)
            if success:
                await client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="assistant",
                    content=response_data["text"]
                )
            return {"status": "success" if success else "error", "message": "Processed and responded" if success else "Failed to send response"}

        try:
            current_date = datetime.now().strftime("%Y-%m-%d")
            logger.debug(f"[{user_id}] Computed current_date: {current_date}")
            full_message = {
                "message": message,
                "phone": klingo_phone,
                "clinic_id": clinic_id,
                "history": thread_history,
                "current_date": current_date
            }
            logger.debug(f"[{user_id}] Full message to agent: {json.dumps(full_message, ensure_ascii=False)}")
            await client.beta.threads.messages.create(
                thread_id=thread_id,
                role="user",
                content=message
            )
            logger.debug(f"[{user_id}] Added user message to thread {thread_id}: {message}")
            response_data = await run_agent_with_retry(triage_agent_instance, json.dumps(full_message))
            logger.debug(f"[{user_id}] Agent response: {json.dumps(response_data, ensure_ascii=False)}")
            if not isinstance(response_data, dict) or "text" not in response_data or "metadata" not in response_data:
                logger.warning(f"[{user_id}] Invalid agent response format: {response_data}")
                response_data = build_response_data(
                    text=str(response_data),
                    metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                    intent="error"
                )

            if "phone_number" not in response_data["metadata"]:
                response_data["metadata"]["phone_number"] = klingo_phone
            if "clinic_id" not in response_data["metadata"]:
                response_data["metadata"]["clinic_id"] = clinic_id

            lead_data = LeadData(remotejid=user_id, telefone=klingo_phone, clinic_id=clinic_id)
            user_provided_name = None
            name_match = re.search(r'nome:\s*([^\n]+)', message, re.IGNORECASE)
            if name_match:
                user_provided_name = name_match.group(1).strip().capitalize()
            if response_data["metadata"].get("name"):
                lead_data.nome_cliente = response_data["metadata"]["name"]
            if response_data["metadata"].get("birth_date"):
                lead_data.data_nascimento = response_data["metadata"]["birth_date"]
            if response_data["metadata"].get("register_id"):
                lead_data.klingo_client_id = response_data["metadata"]["register_id"]
            if response_data["metadata"].get("access_token"):
                lead_data.klingo_access_key = response_data["metadata"]["access_token"]
                logger.debug(f"[{user_id}] Saving access_token to Supabase: {response_data['metadata']['access_token']}")
            if any(lead_data.dict(exclude_unset=True).values()):
                logger.debug(f"[{user_id}] Updating lead data: {lead_data.dict(exclude_unset=True)}")
                await upsert_lead(user_id, lead_data)

            if response_data["metadata"].get("intent") == "payment" and response_data["metadata"].get("step") == "process_payment":
                cpf_cnpj = response_data["metadata"].get("cpf")
                if cpf_cnpj:
                    try:
                        nome_cliente = user_provided_name or lead_data.nome_cliente or push_name or "Cliente"
                        customer_data = await get_customer_by_cpf(cpf_cnpj, user_id, clinic_id)
                        customer_json = json.loads(customer_data)
                        if customer_json.get("data") and len(customer_json["data"]) > 0:
                            customer_id = customer_json["data"][0]["id"]
                            logger.info(f"[{user_id}] Customer found: {customer_id}")
                        else:
                            logger.info(f"[{user_id}] No customer found, creating new customer for CPF {cpf_cnpj}")
                            customer_result = await create_customer(cpf_cnpj, nome_cliente, None, klingo_phone or phone_number.replace("@s.whatsapp.net", ""), user_id, clinic_id)
                            customer_json = json.loads(customer_result)
                            if "id" in customer_json:
                                customer_id = customer_json["id"]
                                logger.info(f"[{user_id}] Created customer: {customer_id}")
                            else:
                                logger.error(f"[{user_id}] Failed to create customer: {customer_json}")
                                response_data = build_response_data(
                                    text="Erro ao criar cliente no Asaas. Por favor, tente novamente ou contate o suporte.",
                                    metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                                    intent="error"
                                )
                                success = await send_response(phone_number, user_id, response_data, prefer_audio=False, message_key_id=message_key_id, is_audio_message=False, message=None, clinic_id=clinic_id)
                                return {"status": "success" if success else "error", "message": "Processed and responded" if success else "Failed to send response"}

                        amount = await fetch_procedure_price(
                            id_plano=response_data["metadata"].get("plano", 1),
                            id_medico=response_data["metadata"].get("doctor_id"),
                            clinic_id=clinic_id,
                            remotejid=user_id
                        )
                        payment_result = await create_payment_link(
                            customer_id=customer_id,
                            amount=amount,
                            description=f"Consulta com {response_data['metadata'].get('doctor_name', 'Médico')} em {response_data['metadata'].get('selected_date', 'Data')}",
                            remotejid=user_id,
                            clinic_id=clinic_id
                        )
                        payment_json = json.loads(payment_result)
                        if "invoiceUrl" in payment_json:
                            response_data = build_response_data(
                                text=f"Seu CPF foi encontrado! Acesse o link de pagamento para sua consulta: {payment_json['invoiceUrl']}",
                                metadata={
                                    "intent": "payment",
                                    "step": "payment_link_sent",
                                    "phone_number": klingo_phone,
                                    "register_id": response_data["metadata"].get("register_id"),
                                    "access_token": response_data["metadata"].get("access_token"),
                                    "customer_id": customer_id,
                                    "payment_status": payment_json.get("status"),
                                    "invoice_url": payment_json["invoiceUrl"],
                                    "clinic_id": clinic_id
                                },
                                intent="payment"
                            )
                            lead_data.cpf_cnpj = cpf_cnpj
                            lead_data.asaas_customer_id = customer_id
                            lead_data.payment_status = payment_json.get("status")
                            await upsert_lead(user_id, lead_data)
                        else:
                            logger.error(f"[{user_id}] Failed to create payment link: {payment_json}")
                            response_data = build_response_data(
                                text="Erro ao criar link de pagamento. Por favor, tente novamente ou contate o suporte.",
                                metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                                intent="error"
                            )

                    except Exception as e:
                        logger.error(f"[{user_id}] Erro ao processar CPF {cpf_cnpj}: {str(e)}")
                        response_data = build_response_data(
                            text="Erro ao processar seu CPF. Por favor, tente novamente ou contate o suporte.",
                            metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                            intent="error"
                        )

            try:
                extracted_info = await extract_lead_info(message, remotejid=user_id)
                extracted_data = json.loads(extracted_info)
                if "error" not in extracted_data:
                    lead_data = LeadData(**extracted_data, clinic_id=clinic_id)
                    if user_provided_name:
                        lead_data.nome_cliente = user_provided_name
                    await upsert_lead(user_id, lead_data)
                    logger.debug(f"[{user_id}] Lead data saved: {lead_data.dict(exclude_unset=True)}")
            except Exception as e:
                logger.error(f"[{user_id}] Failed to extract or save lead info: {str(e)}")

        except Exception as e:
            logger.error(f"[{user_id}] Failed to process message in thread {thread_id}: {str(e)}")
            response_data = build_response_data(
                text=f"Erro ao processar mensagem: {str(e)}. Por favor, tente novamente ou contate o suporte.",
                metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                intent="error"
            )

        success = await send_response(phone_number, user_id, response_data, prefer_audio, message_key_id, is_audio_message, message, clinic_id=clinic_id)

        try:
            if response_data.get("text"):
                await client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="assistant",
                    content=response_data["text"]
                )
                logger.debug(f"[{user_id}] Added assistant response to thread {thread_id}: {response_data}")
        except Exception as e:
            logger.error(f"[{user_id}] Failed to add assistant response to thread {thread_id}: {str(e)}")
            response_data = build_response_data(
                text=f"Erro ao salvar resposta do assistente: {str(e)}. Por favor, tente novamente ou contate o suporte.",
                metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                intent="error"
            )
            success = await send_response(phone_number, user_id, response_data, prefer_audio=False, message_key_id=message_key_id, is_audio_message=False, message=None, clinic_id=clinic_id)

        if success:
            logger.info(f"[{user_id}] Mensagem enviada com sucesso, message_key_id: {message_key_id}")
            return {"status": "success", "message": "Processed and responded"}
        else:
            logger.error(f"[{user_id}] Falha ao enviar resposta para o WhatsApp, message_key_id: {message_key_id}")
            return {"status": "error", "message": "Failed to send response"}

    except Exception as e:
        logger.error(f"Erro ao processar webhook: {str(e)}")
        return {"status": "error", "message": f"Error processing webhook: {str(e)}"}