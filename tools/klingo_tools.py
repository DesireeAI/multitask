from agents import function_tool
import httpx
import json
from datetime import datetime, timedelta
from utils.logging_setup import setup_logging
from config.config import SUPABASE_URL, SUPABASE_KEY
from supabase import acreate_client, AsyncClient  # Changed to acreate_client and AsyncClient
from typing import Optional

logger = setup_logging()

async def _get_klingo_app_token(clinic_id: str, remotejid: str) -> str:
    """
    Fetch the Klingo app token for a specific clinic from Supabase.
    """
    if not all([SUPABASE_URL, SUPABASE_KEY]):
        logger.error(f"[{remotejid}] Configurações do Supabase não estão completas")
        return ""
    try:
        client: AsyncClient = await acreate_client(SUPABASE_URL, SUPABASE_KEY)  # Use acreate_client
        response = await client.table("clinics").select("klingo_app_token").eq("clinic_id", clinic_id).execute()
        if response.data and len(response.data) > 0:
            token = response.data[0]["klingo_app_token"]
            if token:
                logger.debug(f"[{remotejid}] Found Klingo app token for clinic_id: {clinic_id}")
                return token
            else:
                logger.error(f"[{remotejid}] No Klingo app token found for clinic_id: {clinic_id}")
                return ""
        else:
            logger.error(f"[{remotejid}] Clinic not found for clinic_id: {clinic_id}")
            return ""
    except Exception as e:
        logger.error(f"[{remotejid}] Error fetching Klingo app token for clinic_id {clinic_id}: {str(e)}")
        return ""

@function_tool
async def fetch_klingo_schedule(
    cbos: str,
    id_consulta: int,
    id_convenio: int,
    clinic_id: str,
    remotejid: str,
    professional_id: Optional[str] = None
) -> str:
    """
    Fetch available consultation slots from Klingo API for the next 5 days, starting tomorrow.
    Args:
        cbos (str): Specialty code (e.g., '225275' for OTORRINOLARINGOLOGIA).
        id_consulta (int): Consultation procedure ID (e.g., 1376 for 'Consulta médica- Otorrino').
        id_convenio (int): Plan ID (e.g., 1 for particular).
        clinic_id (str): The ID of the clinic.
        remotejid (str): The WhatsApp user ID for logging.
        professional_id (str, optional): ID of the preferred doctor.
    Returns:
        str: JSON string with formatted schedule or error message.
    """
    # Calculate start_date (tomorrow) and end_date (5 days later)
    start_date = (datetime.now().date() + timedelta(days=1)).strftime("%Y-%m-%d")
    end_date = (datetime.now().date() + timedelta(days=5)).strftime("%Y-%m-%d")

    logger.debug(f"[{remotejid}] Calling fetch_klingo_schedule with cbos: {cbos}, exame: {id_consulta}, id_convenio: {id_convenio}, professional_id: {professional_id}, clinic_id: {clinic_id}, start_date: {start_date}, end_date: {end_date}")
    klingo_app_token = await _get_klingo_app_token(clinic_id, remotejid)
    if not klingo_app_token:
        return json.dumps({"error": "Não foi possível obter o token da API Klingo para a clínica"})

    try:
        url = (
            f"https://api-externa.klingo.app/api/agenda/horarios"
            f"?especialidade={cbos}&exame={id_consulta}&inicio={start_date}&fim={end_date}&plano={id_convenio}"
        )
        if professional_id:
            url += f"&profissional={professional_id}"
        headers = {
            "accept": "application/json",
            "X-APP-TOKEN": klingo_app_token
        }
        
        logger.debug(f"[{remotejid}] Sending Klingo API request: {url}")
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
        
        logger.debug(f"[{remotejid}] Klingo API response: {json.dumps(data, indent=2, ensure_ascii=False)}")
        
        if not data or not isinstance(data, dict):
            logger.warning(f"[{remotejid}] Empty or invalid Klingo response")
            return json.dumps({"error": "Nenhum horário disponível encontrado."})
        
        schedules = data.get("horarios", [])
        professionals = data.get("profissionais", [])
        
        if not schedules or not professionals:
            logger.warning(f"[{remotejid}] No schedules or professionals found in Klingo response")
            return json.dumps({"error": "Nenhum horário disponível encontrado."})
        
        professional_map = {
            str(p["id"]): {
                "nome": p["nome"],
                "numero": p.get("numero", 0),
                "uf": p.get("uf", ""),
                "conselho": p.get("conselho", "")
            } for p in professionals
        }
        logger.debug(f"[{remotejid}] Available professionals: {professional_map}")
        
        formatted_schedules = []
        for horario in schedules:
            if not professional_id or str(horario["profissional"]["id"]) == str(professional_id):
                times = [
                    {
                        "slot_id": slot_id,
                        "time": time,
                        "datetime": f"{horario['data']}T{time}:00"
                    }
                    for slot_id, time in horario["horarios"].items()
                ][:3]
                formatted_schedules.append({
                    "doctor_id": str(horario["profissional"]["id"]),
                    "doctor_name": horario["profissional"]["nome"],
                    "doctor_number": professional_map[str(horario["profissional"]["id"])]["numero"],
                    "date": horario["data"],
                    "times": times
                })
        
        if not formatted_schedules:
            logger.warning(f"[{remotejid}] No matching schedules for professional_id: {professional_id}")
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
        
        logger.info(f"[{remotejid}] Fetched Klingo schedule: {json.dumps(formatted, ensure_ascii=False)}")
        return json.dumps(formatted, ensure_ascii=False)
    
    except httpx.HTTPStatusError as e:
        logger.error(f"[{remotejid}] Klingo API error: {str(e)}, Status: {e.response.status_code}")
        return json.dumps({"error": f"Erro ao consultar agenda: {str(e)}"})
    except Exception as e:
        logger.error(f"[{remotejid}] Unexpected error in fetch_klingo_schedule: {str(e)}")
        return json.dumps({"error": f"Erro inesperado: {str(e)}"})

@function_tool
async def book_klingo_appointment(
    access_token: str,
    slot_id: str,
    profissional_number: str,
    doctor_name: str,
    doctor_number: int,
    email: str = "",
    remotejid: str = None,
    clinic_id: str = None,
    id_consulta: int = 1376
) -> str:
    """
    Book an appointment in Klingo for a patient using the provided slot_id and access_token.
    """
    logger.info(f"[{remotejid}] Calling book_klingo_appointment with slot_id: {slot_id}, doctor_id: {profissional_number}, clinic_id: {clinic_id}, exame: {id_consulta}")
    if not access_token or not slot_id or not profissional_number or not doctor_name or not doctor_number:
        logger.error(f"[{remotejid}] Parâmetros obrigatórios ausentes: access_token={access_token}, slot_id={slot_id}, profissional_number={profissional_number}, doctor_name={doctor_name}, doctor_number={doctor_number}")
        return json.dumps({"error": "Parâmetros obrigatórios ausentes"})

    try:
        appointment_datetime = slot_id.split("|")[0] + "T" + slot_id.split("|")[-1] + ":00"
        datetime.fromisoformat(appointment_datetime)
    except (IndexError, ValueError):
        logger.error(f"[{remotejid}] Invalid slot_id format: {slot_id}")
        return json.dumps({"error": "Formato de slot_id inválido"})

    payload = {
        "procedimento": str(id_consulta),
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
                "appointment_datetime": appointment_datetime,
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

@function_tool
async def identify_klingo_patient(phone_number: str, birth_date: str = "", remotejid: str = None, clinic_id: str = None) -> str:
    """
    Identify a patient in Klingo using phone number and optional birth date.
    """
    klingo_app_token = await _get_klingo_app_token(clinic_id, remotejid)
    if not klingo_app_token:
        return json.dumps({"error": "Não foi possível obter o token da API Klingo para a clínica"})

    if not phone_number.isdigit() or len(phone_number) != 11:
        logger.error(f"[{remotejid}] Formato de telefone inválido: {phone_number}")
        return json.dumps({"error": "Número de telefone deve ter 11 dígitos numéricos."})

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
                    "X-APP-TOKEN": klingo_app_token
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"[{remotejid}] Resposta da Klingo API: Status={response.status_code}, Body={json.dumps(data, ensure_ascii=False)}")

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
async def register_klingo_patient(name: str, gender: str, birth_date: str, phone_number: str, email: str = "", remotejid: str = None, clinic_id: str = None) -> str:
    """
    Register a new patient in Klingo using provided details.
    """
    klingo_app_token = await _get_klingo_app_token(clinic_id, remotejid)
    if not klingo_app_token:
        return json.dumps({"error": "Não foi possível obter o token da API Klingo para a clínica"})

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
                    "X-APP-TOKEN": klingo_app_token
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
async def login_klingo_patient(register_id: str, remotejid: str = None, clinic_id: str = None) -> str:
    """
    Authenticate a patient in Klingo using their register_id.
    """
    klingo_app_token = await _get_klingo_app_token(clinic_id, remotejid)
    if not klingo_app_token:
        return json.dumps({"error": "Não foi possível obter o token da API Klingo para a clínica"})

    payload = {"id": register_id}
    logger.debug(f"[{remotejid}] Enviando solicitação de login para Klingo API: URL=https://api-externa.klingo.app/api/externo/login, Payload={json.dumps(payload, ensure_ascii=False)}")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api-externa.klingo.app/api/externo/login",
                json=payload,
                headers={
                    "accept": "application/json",
                    "X-APP-TOKEN": klingo_app_token
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"[{remotejid}] Resposta da Klingo API: Status={response.status_code}, Body={json.dumps(data, ensure_ascii=False)}")

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
async def fetch_procedure_price(
    id_plano: int,
    id_medico: Optional[int] = None,
    id_unidade: Optional[int] = None,
    clinic_id: str = None,
    remotejid: str = None
) -> float:
    """
    Fetch procedure price from Klingo API.
    """
    client: AsyncClient = await acreate_client(SUPABASE_URL, SUPABASE_KEY)  # Use acreate_client
    response = await client.table("clinics").select("klingo_app_token").eq("clinic_id", clinic_id).execute()
    if not response.data:
        logger.error(f"[{remotejid}] No klingo_app_token found for clinic_id: {clinic_id}")
        return 300.0
    klingo_app_token = response.data[0]["klingo_app_token"]

    try:
        async with httpx.AsyncClient() as client:
            params = {"id_plano": id_plano}
            if id_medico:
                params["id_medico"] = id_medico
            if id_unidade:
                params["id_unidade"] = id_unidade
            response = await client.get(
                f"https://api-externa.klingo.app/api/precos",
                params=params,
                headers={
                    "accept": "application/json",
                    "X-APP-TOKEN": klingo_app_token
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            for procedure in data["data"]:
                if procedure["valor"] is not None:
                    logger.debug(f"[{remotejid}] Found price for procedure {procedure['id']}: {procedure['valor']}")
                    return float(procedure["valor"])
            logger.warning(f"[{remotejid}] No valid price found for id_plano {id_plano}, defaulting to 300.0")
            return 300.0
    except httpx.HTTPStatusError as e:
        logger.error(f"[{remotejid}] HTTP error fetching price for id_plano {id_plano}: {e.response.status_code}")
        return 300.0
    except Exception as e:
        logger.error(f"[{remotejid}] Error fetching price for id_plano {id_plano}: {str(e)}")
        return 300.0

@function_tool
async def fetch_klingo_specialties(clinic_id: str, remotejid: str) -> str:
    """
    Fetch available specialties from Klingo API.
    """
    klingo_app_token = await _get_klingo_app_token(clinic_id, remotejid)
    if not klingo_app_token:
        return json.dumps({"error": "Não foi possível obter o token da API Klingo"})
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api-externa.klingo.app/api/agenda/especialidades",
                headers={
                    "accept": "application/json",
                    "X-APP-TOKEN": klingo_app_token
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"[{remotejid}] Fetched specialties: {data}")
            return json.dumps(data)
    except httpx.HTTPStatusError as e:
        logger.error(f"[{remotejid}] HTTP error fetching specialties: {e.response.status_code}")
        return json.dumps({"error": f"Erro HTTP: {e.response.status_code}"})
    except Exception as e:
        logger.error(f"[{remotejid}] Error fetching specialties: {str(e)}")
        return json.dumps({"error": f"Erro: {str(e)}"})

@function_tool
async def fetch_klingo_convenios(clinic_id: str, remotejid: str) -> str:
    """
    Fetch available health plans (convênios) from Klingo API.
    """
    klingo_app_token = await _get_klingo_app_token(clinic_id, remotejid)
    if not klingo_app_token:
        return json.dumps({"error": "Não foi possível obter o token da API Klingo"})
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://api-externa.klingo.app/api/convenios",
                headers={
                    "accept": "application/json",
                    "X-APP-TOKEN": klingo_app_token
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"[{remotejid}] Fetched convênios: {data}")
            return json.dumps(data)
    except httpx.HTTPStatusError as e:
        logger.error(f"[{remotejid}] HTTP error fetching convênios: {e.response.status_code}")
        return json.dumps({"error": f"Erro HTTP: {e.response.status_code}"})
    except Exception as e:
        logger.error(f"[{remotejid}] Error fetching convênios: {str(e)}")
        return json.dumps({"error": f"Erro: {str(e)}"})

@function_tool
async def fetch_klingo_consultas(clinic_id: str, remotejid: str, cbos: str = None) -> str:
    """
    Fetch available consultation procedures from Klingo API.
    """
    klingo_app_token = await _get_klingo_app_token(clinic_id, remotejid)
    if not klingo_app_token:
        return json.dumps({"error": "Não foi possível obter o token da API Klingo"})
    
    try:
        async with httpx.AsyncClient() as client:
            params = {}
            if cbos:
                params["cbos"] = cbos
            response = await client.get(
                "https://api-externa.klingo.app/api/agenda/consultas",
                params=params,
                headers={
                    "accept": "application/json",
                    "X-APP-TOKEN": klingo_app_token
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"[{remotejid}] Fetched consultas: {data}")
            return json.dumps(data)
    except httpx.HTTPStatusError as e:
        logger.error(f"[{remotejid}] HTTP error fetching consultas: {e.response.status_code}")
        return json.dumps({"error": f"Erro HTTP: {e.response.status_code}"})
    except Exception as e:
        logger.error(f"[{remotejid}] Error fetching consultas: {str(e)}")
        return json.dumps({"error": f"Erro: {str(e)}"})

@function_tool
async def fetch_klingo_profissionais(clinic_id: str, remotejid: str, cbos: str = None) -> str:
    """
    Fetch available professionals from Klingo API based on the provided clinic_id and optional cbos.
    The cbos parameter should be provided by the agent when the user's desired specialty is identified.
    Returns a JSON string containing the list of professionals.
    """
    klingo_app_token = await _get_klingo_app_token(clinic_id, remotejid)
    if not klingo_app_token:
        return json.dumps({"error": "Não foi possível obter o token da API Klingo"})
    
    try:
        async with httpx.AsyncClient() as client:
            params = {}
            if cbos:
                params["cbos"] = cbos
            response = await client.get(
                "https://api-externa.klingo.app/api/profissionais",
                params=params,
                headers={
                    "accept": "application/json",
                    "X-APP-TOKEN": klingo_app_token
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            logger.debug(f"[{remotejid}] Fetched profissionais: {data}")
            return json.dumps(data)
    except httpx.HTTPStatusError as e:
        logger.error(f"[{remotejid}] HTTP error fetching profissionais: {e.response.status_code}")
        return json.dumps({"error": f"Erro HTTP: {e.response.status_code}"})
    except Exception as e:
        logger.error(f"[{remotejid}] Error fetching profissionais: {str(e)}")
        return json.dumps({"error": f"Erro: {str(e)}"})