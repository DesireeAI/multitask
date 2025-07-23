# tools/klingo_tools.py
from agents import function_tool
import httpx
import json
import os
from datetime import datetime, timedelta
from utils.logging_setup import setup_logging

logger = setup_logging()

# Carregar variáveis de ambiente (opcional, se usar .env)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv não é necessário se as variáveis já estão no ambiente

# Obter o token da variável de ambiente
KLINGO_APP_TOKEN = os.getenv("KLINGO_APP_TOKEN")
if not KLINGO_APP_TOKEN:
    logger.error("KLINGO_APP_TOKEN não está configurado nas variáveis de ambiente.")
    raise ValueError("KLINGO_APP_TOKEN é necessário para acessar a API do Klingo.")

@function_tool
async def fetch_klingo_schedule(professional_id: str = None, user_id: str = None) -> str:
    """
    Fetch available consultation slots from Klingo API for a specific doctor or up to 3 doctors.

    Args:
        professional_id: Optional ID of the preferred doctor (e.g., '5' for Dr Carlos Borba).
        user_id: User identifier for logging purposes.

    Returns:
        JSON string with formatted schedule or error message.
    """
    logger.debug(f"[{user_id}] Calling fetch_klingo_schedule with professional_id: {professional_id}")
    try:
        start_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        end_date = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
        
        url = (
            f"https://api-externa.klingo.app/api/agenda/horarios"
            f"?especialidade=225275&exame=1376&inicio={start_date}&fim={end_date}&plano=1"
        )
        headers = {
            "accept": "application/json",
            "X-APP-TOKEN": KLINGO_APP_TOKEN
        }
        
        logger.debug(f"[{user_id}] Sending Klingo API request: {url}")
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
        
        logger.debug(f"[{user_id}] Klingo API response: {json.dumps(data, indent=2, ensure_ascii=False)}")
        
        # Check if response is a valid dictionary
        if not data or not isinstance(data, dict):
            logger.warning(f"[{user_id}] Empty or invalid Klingo response")
            return json.dumps({"error": "Nenhum horário disponível encontrado."})
        
        # Extract schedules and professionals
        schedules = data.get("horarios", [])
        professionals = data.get("profissionais", [])
        
        if not schedules or not professionals:
            logger.warning(f"[{user_id}] No schedules or professionals found in Klingo response")
            return json.dumps({"error": "Nenhum horário disponível encontrado."})
        
        # Map professional IDs to names for easier lookup
        professional_map = {str(p["id"]): p["nome"] for p in professionals}
        logger.debug(f"[{user_id}] Available professionals: {professional_map}")
        
        formatted_schedules = []
        if professional_id:
            # Filter schedules for the specified professional
            for horario in schedules:
                if str(horario["profissional"]["id"]) == str(professional_id):
                    formatted_schedules.append({
                        "doctor_id": str(horario["profissional"]["id"]),
                        "doctor_name": horario["profissional"]["nome"],
                        "date": horario["data"],
                        "times": list(horario["horarios"].values()),
                        "slot_id": horario["id"]
                    })
        else:
            # Get schedules for up to 3 professionals
            doctor_ids = [str(p["id"]) for p in professionals[:3]]
            for horario in schedules:
                if str(horario["profissional"]["id"]) in doctor_ids:
                    formatted_schedules.append({
                        "doctor_id": str(horario["profissional"]["id"]),
                        "doctor_name": horario["profissional"]["nome"],
                        "date": horario["data"],
                        "times": list(horario["horarios"].values()),
                        "slot_id": horario["id"]
                    })
        
        if not formatted_schedules:
            logger.warning(f"[{user_id}] No matching schedules for professional_id: {professional_id}")
            return json.dumps({"error": "Nenhum horário disponível para o médico selecionado."})
        
        if professional_id:
            # Format for a single doctor
            dates = sorted(list(set(s["date"] for s in formatted_schedules)))[:3]
            formatted = {
                "doctor_name": formatted_schedules[0]["doctor_name"],
                "dates": dates,
                "schedules": {s["date"]: {"times": s["times"], "slot_id": s["slot_id"]} for s in formatted_schedules}
            }
        else:
            # Format for multiple doctors
            formatted = {}
            for doc_id in set(s["doctor_id"] for s in formatted_schedules):
                doc_schedules = [s for s in formatted_schedules if s["doctor_id"] == doc_id]
                if doc_schedules:
                    doctor_name = doc_schedules[0]["doctor_name"]
                    dates = sorted(list(set(s["date"] for s in doc_schedules)))[:3]
                    formatted[doctor_name] = {
                        "doctor_id": doc_id,
                        "dates": dates,
                        "schedules": {s["date"]: {"times": s["times"], "slot_id": s["slot_id"]} for s in doc_schedules}
                    }
        
        logger.info(f"[{user_id}] Fetched Klingo schedule: {json.dumps(formatted, ensure_ascii=False)}")
        return json.dumps(formatted, ensure_ascii=False)
    
    except httpx.HTTPStatusError as e:
        logger.error(f"[{user_id}] Klingo API error: {str(e)}, Status: {e.response.status_code}")
        return json.dumps({"error": f"Erro ao consultar agenda: {str(e)}"})
    except Exception as e:
        logger.error(f"[{user_id}] Unexpected error in fetch_klingo_schedule: {str(e)}")
        return json.dumps({"error": f"Erro inesperado: {str(e)}"})

@function_tool
async def identify_klingo_patient(phone_number: str, birth_date: str = None, cpf: str = None) -> str:
    logger.debug(f"identify_klingo_patient called with phone_number: {phone_number}, birth_date: {birth_date}, cpf: {cpf}")
    """
    Identify a patient in the Klingo system using phone number and either birth date or CPF.

    Args:
        phone_number: Patient's phone number (10 digits, e.g., '8496248451').
        birth_date: Patient's birth date in YYYY-MM-DD format (e.g., '1989-10-10').
        cpf: Patient's CPF (11 digits, e.g., '12345678901').

    Returns:
        JSON string with patient identification result or error message.
    """
    # Validate phone_number
    if not phone_number or not phone_number.isdigit() or len(phone_number) != 10:
        logger.warning(f"Invalid phone_number: {phone_number}")
        return json.dumps({"status": "error", "message": f"Número de telefone inválido: {phone_number}. Deve conter 10 dígitos (ex.: 8496248451)."})

    # Validate that at least one of birth_date or cpf is provided
    if not birth_date and not cpf:
        logger.warning(f"No birth_date or cpf provided for phone_number: {phone_number}")
        return json.dumps({"status": "error", "message": "Data de nascimento ou CPF são necessários para identificação."})

    try:
        url = "https://api-externa.klingo.app/api/paciente/identificar"
        headers = {
            "accept": "application/json",
            "X-APP-TOKEN": KLINGO_APP_TOKEN,
            "Content-Type": "application/json"
        }
        payload = {"telefone": phone_number}
        if birth_date:
            payload["dt_nascimento"] = birth_date
        if cpf:
            payload["cpf"] = cpf

        logger.debug(f"Sending Klingo API request: {url}, payload: {payload}")
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        logger.debug(f"Klingo API response: {json.dumps(data, indent=2, ensure_ascii=False)}")

        if isinstance(data, list) and len(data) > 0:
            patient = data[0]
            return json.dumps({
                "status": "success",
                "access_token": patient.get("access_token"),
                "patient_id": patient.get("user", {}).get("id_paciente"),
                "patient_name": patient.get("user", {}).get("nome"),
                "unit_name": patient.get("unidade", {}).get("nome")
            })
        else:
            logger.info(f"Patient not identified for phone_number: {phone_number}")
            return json.dumps({
                "status": "not_identified",
                "code": data.get("code", "PACIENTE_NAO_IDENTIFICADO"),
                "message": data.get("msg", "Paciente não identificado.")
            })

    except httpx.HTTPStatusError as e:
        logger.error(f"Klingo API error: {str(e)}, Status: {e.response.status_code}, Response: {e.response.text}")
        return json.dumps({"status": "error", "message": f"Erro ao identificar paciente: {str(e)}"})
    except Exception as e:
        logger.error(f"Unexpected error in identify_klingo_patient: {str(e)}")
        return json.dumps({"status": "error", "message": f"Erro inesperado: {str(e)}"})