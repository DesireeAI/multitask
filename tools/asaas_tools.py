# tools/asaas_tools.py
import httpx
import json
from config.config import SUPABASE_URL, SUPABASE_KEY
from utils.logging_setup import setup_logging
from supabase import create_client
from agents import function_tool
from tenacity import retry, stop_after_attempt, wait_exponential
from datetime import datetime
from dateutil.relativedelta import relativedelta

logger = setup_logging()

async def _get_asaas_api_key(clinic_id: str, remotejid: str) -> str:
    """
    Fetch the Asaas API key for a specific clinic from Supabase.
    Args:
        clinic_id (str): The ID of the clinic.
        remotejid (str): The WhatsApp user ID for logging context.
    Returns:
        str: The Asaas API key or empty string if not found.
    """
    if not all([SUPABASE_URL, SUPABASE_KEY]):
        logger.error(f"[{remotejid}] Configurações do Supabase não estão completas")
        return ""
    try:
        client = create_client(SUPABASE_URL, SUPABASE_KEY)
        response = await client.table("clinics").select("asaas_api_key").eq("clinic_id", clinic_id).execute()
        if response.data and len(response.data) > 0:
            api_key = response.data[0]["asaas_api_key"]
            if api_key:
                logger.debug(f"[{remotejid}] Found Asaas API key for clinic_id: {clinic_id}")
                return api_key
            else:
                logger.error(f"[{remotejid}] No Asaas API key found for clinic_id: {clinic_id}")
                return ""
        else:
            logger.error(f"[{remotejid}] Clinic not found for clinic_id: {clinic_id}")
            return ""
    except Exception as e:
        logger.error(f"[{remotejid}] Error fetching Asaas API key for clinic_id {clinic_id}: {str(e)}")
        return ""

@function_tool
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def get_customer_by_cpf(cpf_cnpj: str, remotejid: str, clinic_id: str = None) -> str:
    """
    Search for a customer in Asaas by CPF/CNPJ for agent use.
    Args:
        cpf_cnpj (str): The CPF or CNPJ to search for (e.g., '12345678900').
        remotejid (str): The WhatsApp user ID for logging context.
        clinic_id (str, optional): The ID of the clinic to fetch the Asaas API key.
    Returns:
        str: JSON string with customer data or error message.
    """
    asaas_api_key = await _get_asaas_api_key(clinic_id, remotejid) if clinic_id else None
    if not asaas_api_key:
        logger.error(f"[{remotejid}] Não foi possível obter a chave da API Asaas para a clínica {clinic_id}")
        return json.dumps({"error": "Não foi possível obter a chave da API Asaas para a clínica"})

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://sandbox.asaas.com/api/v3/customers",
                params={"cpfCnpj": cpf_cnpj},
                headers={
                    "accept": "application/json",
                    "access_token": asaas_api_key,
                    "content-type": "application/json"
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"[{remotejid}] Asaas customer search response for CPF {cpf_cnpj}: {data}")
            return json.dumps(data)
    except httpx.HTTPStatusError as e:
        logger.error(f"[{remotejid}] Erro HTTP ao buscar cliente no Asaas por CPF {cpf_cnpj}: {e.response.status_code} - {e.response.text}")
        return json.dumps({"error": f"Erro HTTP: {e.response.status_code} - {e.response.text}"})
    except Exception as e:
        logger.error(f"[{remotejid}] Erro ao buscar cliente no Asaas por CPF {cpf_cnpj}: {str(e)}")
        return json.dumps({"error": f"Erro ao buscar cliente: {str(e)}"})

@function_tool
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def create_customer(cpf_cnpj: str, name: str = None, email: str = None, phone: str = None, remotejid: str = None, clinic_id: str = None) -> str:
    """
    Create a new customer in Asaas for agent use.
    Args:
        cpf_cnpj (str): The CPF or CNPJ of the customer (e.g., '01439172420').
        name (str, optional): The customer's name (e.g., 'maria pereira'). Defaults to 'Cliente OtorrinoMed' if not provided.
        email (str, optional): The customer's email (e.g., 'maria@gmail.com').
        phone (str, optional): The customer's phone number (e.g., '84996248451').
        remotejid (str, optional): The WhatsApp user ID for logging context.
        clinic_id (str, optional): The ID of the clinic to fetch the Asaas API key.
    Returns:
        str: JSON string with created customer data or error message.
    """
    asaas_api_key = await _get_asaas_api_key(clinic_id, remotejid) if clinic_id else None
    if not asaas_api_key:
        logger.error(f"[{remotejid}] Não foi possível obter a chave da API Asaas para a clínica {clinic_id}")
        return json.dumps({"error": "Não foi possível obter a chave da API Asaas para a clínica"})

    cleaned_cpf_cnpj = ''.join(filter(str.isdigit, cpf_cnpj))
    if len(cleaned_cpf_cnpj) not in [11, 14]:
        logger.error(f"[{remotejid}] Invalid CPF/CNPJ format: {cpf_cnpj}")
        return json.dumps({"error": f"CPF/CNPJ inválido: {cpf_cnpj}. Deve ter 11 dígitos (CPF) ou 14 dígitos (CNPJ)."})

    existing_customer = await get_customer_by_cpf(cleaned_cpf_cnpj, remotejid, clinic_id)
    existing_data = json.loads(existing_customer)
    if existing_data.get("data") and len(existing_data["data"]) > 0:
        logger.info(f"[{remotejid}] Customer already exists for CPF {cpf_cnpj}: {existing_data['data'][0]['id']}")
        return json.dumps({"id": existing_data["data"][0]["id"], "name": existing_data["data"][0]["name"], "cpfCnpj": existing_data["data"][0]["cpfCnpj"]})

    formatted_phone = phone
    if not formatted_phone and remotejid:
        raw_phone = remotejid.replace("55", "").split("@")[0]
        if len(raw_phone) == 10 and raw_phone.isdigit():
            formatted_phone = f"{raw_phone[:2]}9{raw_phone[2:]}"
            logger.debug(f"[{remotejid}] Derived phone number from remotejid: {formatted_phone}")
        else:
            logger.error(f"[{remotejid}] Invalid remotejid format for phone derivation: {raw_phone}")
            return json.dumps({"error": f"Número de telefone inválido derivado do remotejid: {raw_phone}. Deve ter 10 dígitos para derivação."})

    if formatted_phone and (not formatted_phone.isdigit() or len(formatted_phone) != 11):
        logger.error(f"[{remotejid}] Invalid phone number format: {formatted_phone}")
        return json.dumps({"error": f"Número de telefone inválido: {formatted_phone}. Deve ter 11 dígitos (DDD + 9 + número)."})

    if formatted_phone == cleaned_cpf_cnpj:
        logger.error(f"[{remotejid}] Phone number matches CPF/CNPJ: {formatted_phone}")
        return json.dumps({"error": f"Número de telefone inválido: não pode ser igual ao CPF/CNPJ."})

    try:
        payload = {
            "cpfCnpj": cleaned_cpf_cnpj,
            "name": name or "Cliente OtorrinoMed",
            "email": email,
            "phone": formatted_phone,
            "mobilePhone": formatted_phone,
            "notificationDisabled": False
        }
        logger.debug(f"[{remotejid}] Customer creation payload: {payload}")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://sandbox.asaas.com/api/v3/customers",
                json={k: v for k, v in payload.items() if v is not None},
                headers={
                    "accept": "application/json",
                    "access_token": asaas_api_key,
                    "content-type": "application/json"
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"[{remotejid}] Asaas customer creation response for CPF {cpf_cnpj}: {data}")
            return json.dumps({"id": data.get("id", ""), "name": data.get("name", ""), "cpfCnpj": data.get("cpfCnpj", "")})
    except httpx.HTTPStatusError as e:
        logger.error(f"[{remotejid}] Erro HTTP ao criar cliente no Asaas para CPF {cpf_cnpj}: {e.response.status_code} - {e.response.text}")
        return json.dumps({"error": f"Erro HTTP: {e.response.status_code} - {e.response.text}"})
    except Exception as e:
        logger.error(f"[{remotejid}] Erro ao criar cliente no Asaas para CPF {cpf_cnpj}: {str(e)}")
        return json.dumps({"error": f"Erro ao criar cliente: {str(e)}"})

@function_tool
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def create_payment_link(customer_id: str, amount: float, description: str, remotejid: str = None, clinic_id: str = None) -> str:
    """
    Create a payment in Asaas for a customer using the /payments endpoint for agent use.
    Args:
        customer_id (str): The Asaas customer ID (e.g., 'cus_000006860997').
        amount (float): The payment amount in BRL (e.g., 300.00).
        description (str): Description of the payment (e.g., 'Consulta OtorrinoMed').
        remotejid (str, optional): The WhatsApp user ID for logging context.
        clinic_id (str, optional): The ID of the clinic to fetch the Asaas API key.
    Returns:
        str: JSON string with payment data (including invoiceUrl) or error message.
    """
    asaas_api_key = await _get_asaas_api_key(clinic_id, remotejid) if clinic_id else None
    if not asaas_api_key:
        logger.error(f"[{remotejid}] Não foi possível obter a chave da API Asaas para a clínica {clinic_id}")
        return json.dumps({"error": "Não foi possível obter a chave da API Asaas para a clínica"})

    try:
        payload = {
        "customer": customer_id,
        "billingType": "UNDEFINED",
        "value": amount,  # Use the passed amount
        "dueDate": (datetime.now() + relativedelta(days=7)).strftime("%Y-%m-%d"),
        "description": description
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://sandbox.asaas.com/api/v3/payments",
                json=payload,
                headers={
                    "accept": "application/json",
                    "access_token": asaas_api_key,
                    "content-type": "application/json"
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"[{remotejid}] Asaas payment creation response for customer {customer_id}: {data}")
            return json.dumps({"invoiceUrl": data.get("invoiceUrl", ""), "id": data.get("id", ""), "status": data.get("status", "")})
    except httpx.HTTPStatusError as e:
        logger.error(f"[{remotejid}] Erro HTTP ao criar pagamento para customer {customer_id}: {e.response.status_code} - {e.response.text}")
        return json.dumps({"error": f"Erro HTTP: {e.response.status_code} - {e.response.text}"})
    except Exception as e:
        logger.error(f"[{remotejid}] Erro ao criar pagamento para customer {customer_id}: {str(e)}")
        return json.dumps({"error": f"Erro ao criar pagamento: {str(e)}"})