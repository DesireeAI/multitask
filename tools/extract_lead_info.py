import re
import json
from typing import Dict, Optional
from models.lead_data import LeadData
from utils.logging_setup import setup_logging
from datetime import datetime
from tools.supabase_tools import upsert_lead

logger = setup_logging()

async def extract_lead_info(message: str, remotejid: Optional[str] = None, pushName: Optional[str] = None, metadata: Optional[Dict] = None) -> str:
    """Extract patient information from a message and metadata, return as JSON."""
    logger.debug(f"Executing extract_lead_info for message: {message}, remotejid: {remotejid}, pushName: {pushName}, metadata: {metadata}")
    try:
        lead_data = LeadData(remotejid=remotejid)
        extracted_data = {"remotejid": remotejid}
        metadata = metadata or {}

        # Use metadata fields if available
        if metadata.get("name"):
            lead_data.nome_cliente = metadata["name"].capitalize()
            extracted_data["nome_cliente"] = lead_data.nome_cliente
        if metadata.get("cpf"):
            lead_data.cpf_cnpj = ''.join(filter(str.isdigit, metadata["cpf"]))
            extracted_data["cpf_cnpj"] = lead_data.cpf_cnpj
        if metadata.get("birth_date"):
            lead_data.data_nascimento = metadata["birth_date"]
            extracted_data["data_nascimento"] = lead_data.data_nascimento
        if metadata.get("email"):
            lead_data.email = metadata["email"]
            extracted_data["email"] = lead_data.email
        if metadata.get("gender"):
            lead_data.gender = metadata["gender"].upper()
            extracted_data["gender"] = lead_data.gender
        if metadata.get("doctor_name"):  # Safe handling of doctor_name
            lead_data.medico = metadata["doctor_name"]
            extracted_data["medico"] = lead_data.medico
        if metadata.get("appointment_datetime"):
            lead_data.data_horario = metadata["appointment_datetime"]
            extracted_data["data_horario"] = lead_data.data_horario
        if metadata.get("especialidade"):
            lead_data.consulta_type = "otorrino" if "otorrino" in metadata["especialidade"].lower() else "fonoaudiologia"
            extracted_data["consulta_type"] = lead_data.consulta_type

        # Regex patterns for structured fields
        patterns = {
            "email": r'[\w\.-]+@[\w\.-]+\.\w+',
            "cep": r'\d{5}-?\d{3}',
            "data_nascimento": r'\b(\d{2}/\d{2}/\d{4})\b',
            "cpf_cnpj": r'\b(\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})\b'
        }

        # Extract structured fields from message if not in metadata
        for field, pattern in patterns.items():
            if field not in extracted_data:
                match = re.search(pattern, message, re.IGNORECASE)
                if match:
                    value = match.group(0)
                    if field == "cpf_cnpj":
                        value = ''.join(filter(str.isdigit, value))
                    extracted_data[field] = value
                    setattr(lead_data, field, value)

        # Extract nome_cliente from message if not in metadata
        if not lead_data.nome_cliente:
            name_match = re.search(r'nome:\s*([^\n,]+)', message, re.IGNORECASE)
            if name_match:
                lead_data.nome_cliente = name_match.group(1).strip().capitalize()
                extracted_data["nome_cliente"] = lead_data.nome_cliente
            elif "," in message:
                parts = [part.strip() for part in message.split(",")]
                if parts and parts[0]:
                    lead_data.nome_cliente = parts[0].capitalize()
                    extracted_data["nome_cliente"] = lead_data.nome_cliente
            elif pushName:
                lead_data.nome_cliente = pushName
                extracted_data["nome_cliente"] = pushName

        # Set idioma as default
        lead_data.idioma = "português"
        extracted_data["idioma"] = "português"

        # Update ult_contato
        extracted_data["ult_contato"] = datetime.now().isoformat()
        lead_data.ult_contato = extracted_data["ult_contato"]

        # Save to Supabase
        await upsert_lead(remotejid, lead_data)
        logger.info(f"[{remotejid}] Extracted lead info: {extracted_data}")
        return json.dumps(extracted_data)
    except Exception as e:
        logger.error(f"[{remotejid}] Erro ao extrair informações do lead: {str(e)}")
        return json.dumps({"error": f"Erro ao extrair informações: {str(e)}"})