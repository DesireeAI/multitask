# utils/validation.py
from typing import Dict
from datetime import datetime
import uuid
from utils.logging_setup import setup_logging

logger = setup_logging()

def validate_lead_data(data: Dict) -> Dict:
    """
    Validate and filter lead data to ensure only valid columns are included.

    Args:
        data (Dict): The lead data to validate.

    Returns:
        Dict: Filtered dictionary containing only valid columns.
    """
    lead_schema = {
        "remotejid", "nome_cliente", "pushname", "telefone", "cidade", "estado",
        "email", "data_nascimento", "idioma", "thread_id", "data_cadastro", 
        "data_ultima_alteracao", "followup", "followup_data", "ult_contato",
        "cep", "endereco", "lead", "verificador", "cpf_cnpj",
        "asaas_customer_id", "payment_status", "consulta_type", "medico",
        "klingo_client_id", "klingo_access_key", "sintomas",
        "clinic_id", "appointment_datetime"  # Added appointment_datetime
    }
    valid_data = {k: v for k, v in data.items() if k in lead_schema and v is not None}

    # Validate clinic_id as UUID
    if "clinic_id" in valid_data:
        try:
            uuid.UUID(valid_data["clinic_id"])
        except ValueError:
            logger.warning(f"Invalid clinic_id format: {valid_data['clinic_id']}, removing")
            del valid_data["clinic_id"]

    # Validate appointment_datetime as ISO format
    if "appointment_datetime" in valid_data:
        try:
            datetime.fromisoformat(valid_data["appointment_datetime"])
        except ValueError:
            logger.warning(f"Invalid appointment_datetime format: {valid_data['appointment_datetime']}, removing")
            del valid_data["appointment_datetime"]

    # Validate payment_status
    if "payment_status" in valid_data and valid_data["payment_status"] not in ["pendente", "pago", "cancelado"]:
        logger.warning(f"Invalid payment_status value: {valid_data['payment_status']}, removing")
        del valid_data["payment_status"]

    # Validate consulta_type
    if "consulta_type" in valid_data and valid_data["consulta_type"] not in ["otorrino", "fonoaudiologia", "outros"]:
        logger.warning(f"Invalid consulta_type value: {valid_data['consulta_type']}, removing")
        del valid_data["consulta_type"]

    if len(valid_data) < len(data):
        logger.warning(f"Filtered out invalid lead columns: {set(data.keys()) - set(valid_data.keys())}")
    return valid_data