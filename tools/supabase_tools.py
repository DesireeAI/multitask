from typing import Dict, Optional
from datetime import datetime
from supabase import acreate_client, AsyncClient  # Changed to acreate_client and AsyncClient
from config.config import SUPABASE_URL, SUPABASE_KEY
from models.lead_data import LeadData
from utils.validation import validate_lead_data
from utils.logging_setup import setup_logging
from pydantic import BaseModel
from agents import function_tool
import uuid
from typing import Dict

logger = setup_logging()

class LeadDataInput(BaseModel):
    nome_cliente: Optional[str] = None
    telefone: Optional[str] = None
    cpf_cnpj: Optional[str] = None
    asaas_customer_id: Optional[str] = None
    thread_id: Optional[str] = None
    data_cadastro: Optional[str] = None
    idioma: Optional[str] = None
    ult_contato: Optional[str] = None
    pushname: Optional[str] = None
    data_nascimento: Optional[str] = None
    klingo_client_id: Optional[str] = None
    klingo_access_key: Optional[str] = None
    cidade: Optional[str] = None
    estado: Optional[str] = None
    email: Optional[str] = None
    followup: Optional[bool] = None
    followup_data: Optional[str] = None
    cep: Optional[str] = None
    endereco: Optional[str] = None
    lead: Optional[int] = None
    verificador: Optional[int] = None
    payment_status: Optional[str] = None
    consulta_type: Optional[str] = None
    medico: Optional[str] = None
    sintomas: Optional[str] = None
    clinic_id: Optional[str] = None
    appointment_id: Optional[str] = None

async def upsert_lead(remotejid: str, data: LeadData, clinic_id: str = None) -> Dict:
    if not all([SUPABASE_URL, SUPABASE_KEY]):
        logger.error(f"[{remotejid}] Configurações do Supabase não estão completas")
        return {}
    if not remotejid or remotejid == "unknown" or "@s.whatsapp.net" not in remotejid:
        logger.error(f"[{remotejid}] Invalid remotejid for upsert: {remotejid}")
        return {}
    try:
        client: AsyncClient = await acreate_client(SUPABASE_URL, SUPABASE_KEY)  # Use acreate_client
        valid_data = validate_lead_data(data.dict(exclude_unset=True))
        valid_data["remotejid"] = remotejid
        valid_data["data_ultima_alteracao"] = datetime.now().isoformat()
        if clinic_id:
            try:
                uuid.UUID(clinic_id)
                valid_data["clinic_id"] = clinic_id
            except ValueError:
                logger.error(f"[{remotejid}] Invalid clinic_id format: {clinic_id}")
                return {}
        logger.debug(f"[{remotejid}] Upserting lead data for remotejid {remotejid}: {valid_data}")
        response = await client.table("clients").upsert(
            valid_data, on_conflict="remotejid", returning="representation"
        ).execute()
        logger.debug(f"[{remotejid}] Upsert response: {response}, type: {type(response)}")
        logger.info(f"[{remotejid}] Upserted lead for remotejid: {remotejid}, data: {valid_data}")
        return response.data[0] if response.data else {}
    except Exception as e:
        logger.error(f"[{remotejid}] Error upserting lead for remotejid {remotejid}: {e}")
        return {}

async def get_lead(remotejid: str) -> Dict:
    if not all([SUPABASE_URL, SUPABASE_KEY]):
        logger.error(f"[{remotejid}] Configurações do Supabase não estão completas")
        return {}
    if not remotejid or remotejid == "unknown" or "@s.whatsapp.net" not in remotejid:
        logger.error(f"[{remotejid}] Invalid remotejid for get_lead: {remotejid}")
        return {}
    try:
        client: AsyncClient = await acreate_client(SUPABASE_URL, SUPABASE_KEY)  # Use acreate_client
        response = await client.table("clients").select("*").eq("remotejid", remotejid).execute()
        logger.debug(f"[{remotejid}] Get response: {response}, type: {type(response)}")
        lead_data = response.data[0] if response.data else {}
        logger.debug(f"[{remotejid}] Retrieved lead for remotejid {remotejid}: {lead_data}")
        return lead_data
    except Exception as e:
        logger.error(f"[{remotejid}] Error retrieving lead for remotejid {remotejid}: {e}")
        return {}

@function_tool
async def upsert_lead_agent(remotejid: str, data: LeadDataInput = None) -> Dict:
    """
    Upsert lead data into Supabase for agent use.
    Args:
        remotejid (str): The WhatsApp user ID (e.g., '558496248451@s.whatsapp.net').
        data (LeadDataInput, optional): Lead data to upsert, including 'nome_cliente', 'telefone', 'cpf_cnpj', etc.
    Returns:
        Dict: The upserted lead data or empty dict on error.
    """
    return await upsert_lead(remotejid, LeadData(**data.dict()) if data else LeadData(remotejid=remotejid))

@function_tool
async def get_lead_agent(remotejid: str) -> Dict:
    """
    Retrieve lead data from Supabase for agent use.
    Args:
        remotejid (str): The WhatsApp user ID (e.g., '558496248451@s.whatsapp.net').
    Returns:
        Dict: Lead data including all fields from the clients table, or empty dict if not found.
    """
    return await get_lead(remotejid)

async def get_clinic_config(clinic_id: str) -> Dict:
    """
    Retrieve clinic configuration and agent prompts from Supabase.
    Args:
        clinic_id (str): UUID of the clinic.
    Returns:
        Dict: Clinic configuration including name, assistant_name, address, recommendations, support_phone,
              and agent prompts (triage_agent, initial_message, offered_services).
    """
    try:
        client: AsyncClient = await acreate_client(SUPABASE_URL, SUPABASE_KEY)
        
        # Buscar configurações da clínica
        clinic_response = await client.table("clinics").select(
            "name, assistant_name, address, recommendations, support_phone"
        ).eq("clinic_id", clinic_id).execute()
        
        # Buscar prompts relevantes
        prompts_response = await client.table("agent_prompts").select(
            "name, prompt, variables, enabled"
        ).eq("clinic_id", clinic_id).in_("name", ["Triage Agent", "Initial Message", "Offered Services"]).execute()
        
        logger.debug(f"[{clinic_id}] Clinic config response: {clinic_response}")
        logger.debug(f"[{clinic_id}] Agent prompts response: {prompts_response}")
        
        # Configurações padrão
        config = {
            "name": "Clínica Padrão",
            "assistant_name": "Assistente",
            "address": "Endereço não informado",
            "recommendations": "Nenhuma recomendação específica.",
            "support_phone": "Não informado",
            "prompts": {
                "triage_agent": {
                    "prompt": "Atenda o cliente {client_name} e identifique a intenção (agendamento, pagamento, dúvida).",
                    "variables": ["{client_name}"],
                    "enabled": True
                },
                "initial_message": {
                    "prompt": "Bem-vindo(a) ao {clinic_name}, {client_name}! {greeting} Como posso ajudar você hoje?",
                    "variables": ["{clinic_name}", "{client_name}", "{greeting}"],
                    "enabled": True
                },
                "offered_services": {
                    "prompt": "Nossos serviços incluem: {service_list}. Deseja mais informações?",
                    "variables": ["{service_list}"],
                    "enabled": True
                }
            }
        }
        
        # Atualizar com dados da clínica
        if clinic_response.data:
            config.update(clinic_response.data[0])
        
        # Atualizar com prompts personalizados
        if prompts_response.data:
            for prompt_data in prompts_response.data:
                agent_key = prompt_data["name"].lower().replace(" ", "_")
                config["prompts"][agent_key] = {
                    "prompt": prompt_data["prompt"],
                    "variables": prompt_data["variables"],
                    "enabled": prompt_data["enabled"]
                }
        
        return config
    except Exception as e:
        logger.error(f"Error fetching clinic config for clinic_id {clinic_id}: {str(e)}")
        return config