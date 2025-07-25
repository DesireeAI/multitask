# tools/klingo_tools.py
from agents import function_tool
import httpx
import json
import os
from datetime import datetime, timedelta
from utils.logging_setup import setup_logging

logger = setup_logging()

# Carregar variáveis de ambiente
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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
        
        if not data or not isinstance(data, dict):
            logger.warning(f"[{user_id}] Empty or invalid Klingo response")
            return json.dumps({"error": "Nenhum horário disponível encontrado."})
        
        schedules = data.get("horarios", [])
        professionals = data.get("profissionais", [])
        
        if not schedules or not professionals:
            logger.warning(f"[{user_id}] No schedules or professionals found in Klingo response")
            return json.dumps({"error": "Nenhum horário disponível encontrado."})
        
        professional_map = {
            str(p["id"]): {
                "nome": p["nome"],
                "numero": p.get("numero", 0),
                "uf": p.get("uf", ""),
                "conselho": p.get("conselho", "")
            } for p in professionals
        }
        logger.debug(f"[{user_id}] Available professionals: {professional_map}")
        
        formatted_schedules = []
        if professional_id:
            for horario in schedules:
                if str(horario["profissional"]["id"]) == str(professional_id):
                    times = [
                        {"slot_id": slot_id, "time": time}
                        for slot_id, time in horario["horarios"].items()
                    ][:3]  # Limit to 3 times
                    formatted_schedules.append({
                        "doctor_id": str(horario["profissional"]["id"]),
                        "doctor_name": horario["profissional"]["nome"],
                        "doctor_number": professional_map[str(horario["profissional"]["id"])]["numero"],
                        "date": horario["data"],
                        "times": times
                    })
        else:
            doctor_ids = [str(p["id"]) for p in professionals[:3]]
            for horario in schedules:
                if str(horario["profissional"]["id"]) in doctor_ids:
                    times = [
                        {"slot_id": slot_id, "time": time}
                        for slot_id, time in horario["horarios"].items()
                    ][:3]  # Limit to 3 times
                    formatted_schedules.append({
                        "doctor_id": str(horario["profissional"]["id"]),
                        "doctor_name": horario["profissional"]["nome"],
                        "doctor_number": professional_map[str(horario["profissional"]["id"])]["numero"],
                        "date": horario["data"],
                        "times": times
                    })
        
        if not formatted_schedules:
            logger.warning(f"[{user_id}] No matching schedules for professional_id: {professional_id}")
            return json.dumps({"error": "Nenhum horário disponível para o médico selecionado."})
        
        if professional_id:
            dates = sorted(list(set(s["date"] for s in formatted_schedules)))[:3]
            formatted = {
                "doctor_id": formatted_schedules[0]["doctor_id"],
                "doctor_name": formatted_schedules[0]["doctor_name"],
                "doctor_number": formatted_schedules[0]["doctor_number"],
                "dates": dates,
                "schedules": {
                    s["date"]: {"times": s["times"]} for s in formatted_schedules
                }
            }
        else:
            formatted = {}
            for doc_id in set(s["doctor_id"] for s in formatted_schedules):
                doc_schedules = [s for s in formatted_schedules if s["doctor_id"] == doc_id]
                if doc_schedules:
                    doctor_name = doc_schedules[0]["doctor_name"]
                    dates = sorted(list(set(s["date"] for s in doc_schedules)))[:3]
                    formatted[doctor_name] = {
                        "doctor_id": doc_id,
                        "doctor_number": doc_schedules[0]["doctor_number"],
                        "dates": dates,
                        "schedules": {
                            s["date"]: {"times": s["times"]} for s in doc_schedules
                        }
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
async def identify_klingo_patient(phone_number: str, birth_date: str = "", remotejid: str = None) -> str:
    """
    Identify a patient in Klingo using phone number and optional birth date.
    Args:
        phone_number (str): The patient's phone number (e.g., '84992101119').
        birth_date (str, optional): The patient's birth date (format YYYY-MM-DD, e.g., '1989-10-10').
        remotejid (str, optional): The WhatsApp user ID for logging context.
    Returns:
        str: JSON string with patient data or error message.
    """
    if not KLINGO_APP_TOKEN:
        logger.error(f"[{remotejid}] Configuração do Klingo não está completa")
        return json.dumps({"error": "Configuração do Klingo não está completa"})

    # Validate phone_number
    if not phone_number.isdigit() or len(phone_number) != 11:
        logger.error(f"[{remotejid}] Formato de telefone inválido: {phone_number}")
        return json.dumps({"error": "Número de telefone deve ter 11 dígitos numéricos."})

    # Validate birth_date format if provided
    if birth_date:
        try:
            datetime.strptime(birth_date, "%Y-%m-%d")
        except ValueError:
            logger.error(f"[{remotejid}] Invalid birth_date format: {birth_date}")
            return json.dumps({"error": "Formato de data de nascimento inválido. Use AAAA-MM-DD."})

    payload = {"telefone": phone_number}
    if birth_date:
        payload["dt_nascimento"] = birth_date

    logger.debug(f"[{remotejid}] Enviando solicitação para Klingo API: URL=https://api-externa.klingo.app/api/paciente/identificar, Payload={json.dumps(payload, ensure_ascii=False)}")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api-externa.klingo.app/api/paciente/identificar",
                json=payload,
                headers={
                    "accept": "application/json",
                    "X-APP-TOKEN": KLINGO_APP_TOKEN
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"[{remotejid}] Resposta da Klingo API: Status={response.status_code}, Body={json.dumps(data, ensure_ascii=False)}")

            # Handle successful response with patient data
            if isinstance(data, dict) and "user" in data and "access_token" in data:
                return json.dumps({
                    "status": "success",
                    "patient_id": str(data["user"].get("id", "")),
                    "patient_name": data["user"].get("nome", ""),
                    "unit_name": data.get("unidade", {}).get("nome", ""),
                    "access_token": data.get("access_token", ""),
                    "token_type": data.get("token_type", "bearer")
                })
            else:
                logger.error(f"[{remotejid}] Unexpected response format: {json.dumps(data, ensure_ascii=False)}")
                return json.dumps({"error": "Formato de resposta inesperado do Klingo API"})

    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        error_detail = e.response.text
        if status_code == 401:
            error_message = "Erro de autenticação com a API Klingo. Verifique o token."
        elif status_code == 400:
            error_message = f"Parâmetros inválidos enviados à API Klingo: {error_detail}"
        elif status_code == 404:
            error_message = "Paciente não encontrado na base da Klingo."
        elif status_code == 419:
            error_message = f"Token inválido ou expirado: {error_detail}"
        else:
            error_message = f"Erro HTTP: {status_code} - {error_detail}"
        logger.error(f"[{remotejid}] {error_message}")
        return json.dumps({"error": error_message})
    except Exception as e:
        logger.error(f"[{remotejid}] Erro ao identificar paciente no Klingo: {str(e)}")
        return json.dumps({"error": f"Erro ao identificar paciente: {str(e)}"})
    
@function_tool
async def register_klingo_patient(
    name: str,
    gender: str,
    birth_date: str,
    phone_number: str,
    email: str = "",
    remotejid: str = None
) -> str:
    """
    Register a new patient in Klingo using provided details.

    Args:
        name (str): Full name of the patient.
        gender (str): Gender of the patient ('M' or 'F').
        birth_date (str): Birth date in YYYY-MM-DD format.
        phone_number (str): Phone number of the patient.
        email (str, optional): Email address of the patient.
        remotejid (str, optional): WhatsApp user ID for logging context.

    Returns:
        str: JSON string with register_id or error message.
    """
    if not KLINGO_APP_TOKEN:
        logger.error(f"[{remotejid}] Configuração do Klingo não está completa")
        return json.dumps({"error": "Configuração do Klingo não está completa"})

    try:
        datetime.strptime(birth_date, "%Y-%m-%d")
    except ValueError:
        logger.error(f"[{remotejid}] Invalid birth_date format: {birth_date}")
        return json.dumps({"error": "Formato de data de nascimento inválido. Use AAAA-MM-DD."})

    if gender not in ["M", "F"]:
        logger.error(f"[{remotejid}] Invalid gender: {gender}")
        return json.dumps({"error": "Sexo inválido. Use 'M' ou 'F'."})

    payload = {
        "paciente": {
            "nome": name,
            "sexo": gender,
            "dt_nasc": birth_date,
            "contatos": {
                "celular": phone_number,
                "telefone": phone_number
            }
        }
    }
    if email:
        payload["paciente"]["contatos"]["email"] = email

    logger.debug(f"[{remotejid}] Enviando solicitação de registro para Klingo API: URL=https://api-externa.klingo.app/api/externo/register, Payload={json.dumps(payload, ensure_ascii=False)}")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api-externa.klingo.app/api/externo/register",
                json=payload,
                headers={
                    "accept": "application/json",
                    "X-APP-TOKEN": KLINGO_APP_TOKEN
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"[{remotejid}] Resposta da Klingo API: Status={response.status_code}, Body={json.dumps(data, ensure_ascii=False)}")

            register_id = None
            if isinstance(data, list) and len(data) > 0 and "id" in data[0]:
                register_id = str(data[0]["id"])
            elif isinstance(data, dict) and "id" in data:
                register_id = str(data["id"])
            else:
                logger.error(f"[{remotejid}] Unexpected response format: {json.dumps(data, ensure_ascii=False)}")
                return json.dumps({"error": "Formato de resposta inesperado do Klingo API"})

            return json.dumps({
                "status": "success",
                "register_id": register_id
            })
    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        error_detail = e.response.text
        error_message = f"Erro HTTP: {status_code} - {error_detail}"
        logger.error(f"[{remotejid}] {error_message}")
        return json.dumps({"error": error_message})
    except Exception as e:
        error_message = f"Erro inesperado: {str(e)}"
        logger.error(f"[{remotejid}] {error_message}, Full exception: {repr(e)}")
        return json.dumps({"error": error_message})

@function_tool
async def login_klingo_patient(register_id: str, remotejid: str = None) -> str:
    """
    Authenticate a patient in Klingo using their register_id.
    Args:
        register_id (str): The register_id returned from register_klingo_patient.
        remotejid (str, optional): WhatsApp user ID for logging context.
    Returns:
        str: JSON string with access_token, token_type, and register_id or error message.
    """
    if not KLINGO_APP_TOKEN:
        logger.error(f"[{remotejid}] Configuração do Klingo não está completa")
        return json.dumps({"error": "Configuração do Klingo não está completa"})

    payload = {"id": register_id}
    logger.debug(f"[{remotejid}] Enviando solicitação de login para Klingo API: URL=https://api-externa.klingo.app/api/externo/login, Payload={json.dumps(payload, ensure_ascii=False)}")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api-externa.klingo.app/api/externo/login",
                json=payload,
                headers={
                    "accept": "application/json",
                    "X-APP-TOKEN": KLINGO_APP_TOKEN
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"[{remotejid}] Resposta da Klingo API: Status={response.status_code}, Body={json.dumps(data, ensure_ascii=False)}")

            # Verifica se a resposta é um dicionário com access_token ou uma lista
            if isinstance(data, dict) and "access_token" in data:
                return json.dumps({
                    "status": "success",
                    "access_token": data["access_token"],
                    "token_type": data.get("token_type", "bearer"),
                    "register_id": register_id
                })
            elif isinstance(data, list) and len(data) > 0 and "access_token" in data[0]:
                login_data = data[0]
                return json.dumps({
                    "status": "success",
                    "access_token": login_data["access_token"],
                    "token_type": login_data.get("token_type", "bearer"),
                    "register_id": register_id
                })
            else:
                logger.error(f"[{remotejid}] Unexpected response format: {json.dumps(data, ensure_ascii=False)}")
                return json.dumps({"error": "Formato de resposta inesperado do Klingo API"})

    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        error_detail = e.response.text
        error_message = f"Erro HTTP: {status_code} - {error_detail}"
        logger.error(f"[{remotejid}] {error_message}")
        return json.dumps({"error": error_message})
    except Exception as e:
        error_message = f"Erro inesperado: {str(e)}"
        logger.error(f"[{remotejid}] {error_message}, Full exception: {repr(e)}")
        return json.dumps({"error": error_message})
    
@function_tool
async def book_klingo_appointment(access_token: str, slot_id: str, doctor_id: str, doctor_name: str, doctor_number: int, email: str = "", remotejid: str = None) -> str:
    logger.info(f"[{remotejid}] Calling book_klingo_appointment with slot_id: {slot_id}, doctor_id: {doctor_id}")
    """
    Book an appointment in Klingo for a patient using the provided slot_id and access_token.
    
    Args:
        access_token (str): The patient's access token from login_klingo_patient or identify_klingo_patient.
        slot_id (str): The ID of the selected time slot (e.g., '2025-07-31|5|3315|1|13:00').
        doctor_id (str): The ID of the doctor (e.g., '5').
        doctor_name (str): The name of the doctor (e.g., 'Dr Carlos Borba').
        doctor_number (int): The doctor's number (e.g., 17137).
        email (str, optional): The patient's email for confirmation.
        remotejid (str, optional): The WhatsApp user ID for logging context.
    
    Returns:
        str: JSON string with appointment details or error message.
    """
    if not access_token or not slot_id or not doctor_id or not doctor_name or not doctor_number:
        logger.error(f"[{remotejid}] Parâmetros obrigatórios ausentes: access_token={access_token}, slot_id={slot_id}, doctor_id={doctor_id}, doctor_name={doctor_name}, doctor_number={doctor_number}")
        return json.dumps({"error": "Parâmetros obrigatórios ausentes"})

    payload = {
        "procedimento": "1376",
        "id": slot_id,
        "email": bool(email),
        "teleatendimento": False,
        "revisao": False,
        "remarcacao": "",
        "ordem_chegada": False,
        "lista": [123],
        "solicitante": {
            "conselho": "CRM",
            "uf": "BA",
            "numero": doctor_number,
            "nome": doctor_name,
            "cbos": "225275"
        },
        "confirmado": "confirmed",
        "id_externo": "22838",
        "obs": "agendado pelo agente de IA - teste",
        "duracao": 10,
        "id_ampliar": 0
    }

    logger.debug(f"[{remotejid}] Enviando solicitação para Klingo API: URL=https://api-externa.klingo.app/api/agenda/horario, Payload={json.dumps(payload, ensure_ascii=False)}")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api-externa.klingo.app/api/agenda/horario",
                json=payload,
                headers={
                    "accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"[{remotejid}] Resposta da Klingo API: Status={response.status_code}, Body={json.dumps(data, ensure_ascii=False)}")
            
            return json.dumps({
                "status": "success",
                "appointment_id": data.get("id", ""),
                "doctor_name": doctor_name,
                "slot_id": slot_id,
                "message": "Agendamento realizado com sucesso"
            })

    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        error_detail = e.response.text
        error_message = f"Erro HTTP: {status_code} - {error_detail}"
        logger.error(f"[{remotejid}] {error_message}")
        return json.dumps({"error": error_message})
    except Exception as e:
        error_message = f"Erro ao realizar agendamento: {str(e)}"
        logger.error(f"[{remotejid}] {error_message}")
        return json.dumps({"error": error_message})