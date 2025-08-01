from fastapi import FastAPI, Request
import json
import re
from openai import AsyncOpenAI
from config.config import OPENAI_API_KEY, SUPABASE_URL, SUPABASE_KEY
from supabase import create_client, Client, AsyncClient, acreate_client
from tools.supabase_tools import get_lead, upsert_lead
from tools.whatsapp_tools import send_whatsapp_message, send_whatsapp_audio, send_whatsapp_image, fetch_media_base64
from tools.audio_tools import text_to_speech
from tools.image_tools import analyze_image
from tools.extract_lead_info import extract_lead_info
from utils.image_processing import resize_image_to_thumbnail
from models.lead_data import LeadData
from bot_agents.triage_agent import triage_agent
from bot_agents.appointment_agent import start_appointment_reminder
from agents import Runner
from utils.logging_setup import setup_logging
from datetime import datetime
import os
import base64
from tools.asaas_tools import get_customer_by_cpf, create_customer, create_payment_link
from tools.klingo_tools import fetch_klingo_schedule, identify_klingo_patient, register_klingo_patient, fetch_procedure_price
from typing import Optional
import asyncio

logger = setup_logging()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)  # OpenAI client
app = FastAPI()
threads = {}

logger.info("Running main.py version with Asaas and Klingo tools")

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
    thread = await client.beta.threads.create()  # Using OpenAI client
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
        messages = await client.beta.threads.messages.list(thread_id=thread_id, limit=limit)  # Using OpenAI client
        history = []
        for msg in reversed(messages.data):
            role = msg.role
            content = msg.content[0].text.value if msg.content else ""
            history.append(f"{role.capitalize()}: {content}")
        return "\n".join(history) if history else "No previous messages."
    except Exception as e:
        logger.error(f"Error retrieving thread history for thread {thread_id}: {str(e)}")
        return "Error retrieving conversation history."

@app.on_event("startup")
async def startup_event():
    logger.info("Disparando tarefa de lembrete de agendamentos...")
    asyncio.create_task(start_appointment_reminder())

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        logger.info(f"Payload recebido: {data}")

        sender_number = data.get("sender", "")
        if not sender_number or "@s.whatsapp.net" not in sender_number:
            logger.error(f"Invalid sender number: {sender_number}")
            return {"status": "error", "message": "Invalid sender number"}

        # Criar cliente Supabase assíncrono
        supabase_client: AsyncClient = await acreate_client(SUPABASE_URL, SUPABASE_KEY)  # Renamed to supabase_client
        response = await supabase_client.table("whatsapp_numbers").select("clinic_id").eq("phone_number", sender_number).execute()
        if not response.data:
            logger.error(f"No clinic found for phone number: {sender_number}")
            return {"status": "error", "message": "Clinic not found"}
        clinic_id = response.data[0]["clinic_id"]
        logger.info(f"Clinic found: {clinic_id} for sender number: {sender_number}")

        # Set clinic_id for RLS
        try:
            rpc_response = await supabase_client.rpc("set_current_clinic_id", {"clinic_id": clinic_id}).execute()
            logger.debug(f"RPC response: {rpc_response}, type: {type(rpc_response)}")
        except Exception as e:
            logger.error(f"Error in RPC call: {str(e)}")
            return {"status": "error", "message": f"Error setting clinic_id: {str(e)}"}

        user_id = data.get("data", {}).get("key", {}).get("remoteJid", "")
        phone_number = user_id
        push_name = data.get("data", {}).get("pushName", None)
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
        message_key_id = data.get("data", {}).get("key", {}).get("id", "")
        response_data = {"text": "Desculpe, houve um problema ao processar sua mensagem. Como posso ajudar?", "metadata": {"intent": "error", "clinic_id": clinic_id}}

        if message_data.get("conversation"):
            message = message_data["conversation"]
            name_match = re.search(r'nome:\s*([^\n]+)', message, re.IGNORECASE)
            if name_match:
                user_provided_name = name_match.group(1).strip().capitalize()

        if message_data.get("audioMessage"):
            is_audio_message = True
            media_result = await fetch_media_base64(message_key_id, "audio", remotejid=user_id)
            if "error" in media_result:
                logger.error(f"[{user_id}] Falha ao processar áudio: {media_result['error']}")
                response_data = {"text": f"Falha ao processar áudio: {media_result['error']}", "metadata": {"intent": "error", "clinic_id": clinic_id}}
            elif media_result.get("type") == "audio":
                message = media_result["transcription"]
                logger.info(f"Transcribed audio to: {message}")

        elif message_data.get("imageMessage"):
            is_image_message = True
            logger.info(f"[{user_id}] Buscando imagem completa via fetch_media_base64")
            try:
                media_result = await fetch_media_base64(message_key_id, "image", remotejid=user_id)
                if "error" in media_result:
                    logger.error(f"[{user_id}] Falha ao buscar imagem completa: {media_result['error']}")
                    response_data = {"text": f"Falha ao buscar imagem completa: {media_result['error']}", "metadata": {"intent": "error", "clinic_id": clinic_id}}
                elif media_result.get("type") == "image":
                    base64_data = media_result["base64"]
                    mimetype = media_result["mimetype"]
                    logger.debug(f"[{user_id}] Imagem completa obtida, mimetype: {mimetype}, tamanho base64: {len(base64_data)}")
                    decoded_data = base64.b64decode(base64_data)
                    resized_base64 = await resize_image_to_thumbnail(decoded_data, max_size=512)
                    if not resized_base64:
                        logger.error(f"[{user_id}] Falha ao redimensionar imagem")
                        response_data = {"text": "Falha ao redimensionar imagem. Por favor, envie outra imagem ou descreva sua solicitação.", "metadata": {"intent": "error", "clinic_id": clinic_id}}
                    else:
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
                response_data = {"text": f"Erro ao processar imagem: {str(e)}", "metadata": {"intent": "error", "clinic_id": clinic_id}}

        if message:
            try:
                full_message = f"Histórico da conversa:\n{thread_history}\n\nNova mensagem: {message}\nPhone: {klingo_phone}\nClinicID: {clinic_id}"
                await client.beta.threads.messages.create(  # Using OpenAI client
                    thread_id=thread_id,
                    role="user",
                    content=message
                )
                logger.debug(f"[{user_id}] Added user message to thread {thread_id}: {message}")
                logger.debug(f"[{user_id}] Calling triage_agent with input: {full_message}, user_id: {user_id}, phone_number: {klingo_phone}")
                response = await Runner.run(triage_agent, input=full_message)
                logger.debug(f"[{user_id}] Raw RunResult: {response}")
                response_data = str(response.final_output)
                logger.info(f"[{user_id}] Raw agent response (final_output): {response_data}")
                try:
                    if response_data.startswith("```json") and response_data.endswith("```"):
                        response_data = response_data[7:-3].strip()
                    response_data = json.loads(response_data)
                    if not isinstance(response_data, dict):
                        logger.warning(f"[{user_id}] Agent response is JSON but not a dict: {response_data}")
                        response_data = build_response_data(
                            text=str(response_data),
                            metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                            intent="error"
                        )
                except json.JSONDecodeError:
                    logger.warning(f"[{user_id}] Resposta não é um JSON válido, tratando como texto puro: {response_data}")
                    response_data = build_response_data(
                        text=response_data,
                        metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                        intent="error"
                    )

                if "phone_number" not in response_data.get("metadata", {}):
                    response_data["metadata"]["phone_number"] = klingo_phone
                if "clinic_id" not in response_data.get("metadata", {}):
                    response_data["metadata"]["clinic_id"] = clinic_id

                lead_data = LeadData(remotejid=user_id, telefone=klingo_phone, clinic_id=clinic_id)
                if response_data.get("metadata", {}).get("name"):
                    lead_data.nome_cliente = response_data["metadata"]["name"]
                if response_data.get("metadata", {}).get("birth_date"):
                    lead_data.data_nascimento = response_data["metadata"]["birth_date"]
                if response_data.get("metadata", {}).get("register_id"):
                    lead_data.klingo_client_id = response_data["metadata"]["register_id"]
                if response_data.get("metadata", {}).get("access_token"):
                    lead_data.klingo_access_key = response_data["metadata"]["access_token"]
                    logger.debug(f"[{user_id}] Saving access_token to Supabase: {response_data['metadata']['access_token']}")
                if any(lead_data.dict(exclude_unset=True).values()):
                    logger.debug(f"[{user_id}] Updating lead data: {lead_data.dict(exclude_unset=True)}")
                    await upsert_lead(user_id, lead_data)

                if response_data.get("metadata", {}).get("intent") == "payment" and response_data.get("metadata", {}).get("step") == "process_payment":
                    cpf_cnpj = response_data["metadata"].get("cpf")
                    if cpf_cnpj:
                        try:
                            nome_cliente = user_provided_name or lead_data.nome_cliente or push_name or "Cliente"
                            customer_data = await _get_customer_by_cpf(cpf_cnpj, user_id, clinic_id)
                            customer_json = json.loads(customer_data)
                            if customer_json.get("data") and len(customer_json["data"]) > 0:
                                customer_id = customer_json["data"][0]["id"]
                                logger.info(f"[{user_id}] Customer found: {customer_id}")
                            else:
                                logger.info(f"[{user_id}] No customer found, creating new customer for CPF {cpf_cnpj}")
                                customer_result = await _create_customer(cpf_cnpj, nome_cliente, None, klingo_phone or phone_number.replace("@s.whatsapp.net", ""), user_id, clinic_id)
                                customer_json = json.loads(customer_result)
                                if "id" in customer_json:
                                    customer_id = customer_json["id"]
                                    logger.info(f"[{user_id}] Created customer: {customer_id}")
                                else:
                                    logger.error(f"[{user_id}] Failed to create customer: {customer_json}")
                                    response_data = build_response_data(
                                        text="Erro ao criar cliente no Asaas. Por favor, tente novamente ou contate o suporte: wa.me/5537987654321.",
                                        metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                                        intent="error"
                                    )
                                    await send_whatsapp_message(phone_number, response_data["text"], remotejid=user_id)
                                    return {"status": "error", "message": "Failed to create customer"}

                            amount = await fetch_procedure_price(
                                id_plano=response_data["metadata"].get("plano", 1),
                                id_medico=response_data["metadata"].get("doctor_id"),
                                clinic_id=clinic_id,
                                remotejid=user_id
                            )
                            payment_result = await _create_payment_link(
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
                                    text="Erro ao criar link de pagamento. Por favor, tente novamente ou contate o suporte: wa.me/5537987654321.",
                                    metadata={"intent": "error", "phone_number": kinklingo_phonego_phone, "clinic_id": clinic_id},
                                    intent="error"
                                )

                        except Exception as e:
                            logger.error(f"[{user_id}] Erro ao processar CPF {cpf_cnpj}: {str(e)}")
                            response_data = build_response_data(
                                text="Erro ao processar seu CPF. Por favor, tente novamente ou contate o suporte: wa.me/5537987654321.",
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
                    text=f"Erro ao processar mensagem: {str(e)}. Por favor, tente novamente ou contate o suporte: wa.me/5537987654321.",
                    metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                    intent="error"
                )

        prefer_audio = is_audio_message or (message and "responda em áudio" in message.lower())
        if prefer_audio and response_data.get("text"):
            audio_path = await text_to_speech(response_data["text"])
            if not audio_path.startswith("Erro"):
                success = await send_whatsapp_audio(
                    phone_number=phone_number,
                    audio_path=audio_path,
                    remotejid=user_id,
                    message_key_id=message_key_id,
                    message_text=message if not is_audio_message else None
                )
                if os.path.exists(audio_path):
                    os.remove(audio_path)
                    logger.debug(f"Removed audio file: {audio_path}")
            else:
                logger.error(f"Failed to generate audio: {audio_path}")
                response_data = build_response_data(
                    text="Desculpe, houve um problema ao gerar o áudio. Como posso ajudar?",
                    metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                    intent="error"
                )
                success = await send_whatsapp_message(phone_number, response_data["text"], remotejid=user_id)
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
                        message_text=message if not is_audio_message else None
                    )
                    if success:
                        response_data = {"text": "", "metadata": response_data["metadata"]}
                    else:
                        logger.error(f"[{user_id}] Falha ao enviar imagem: {image_url}")
                        response_data = build_response_data(
                            text="Desculpe, houve um problema ao enviar a imagem.",
                            metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                            intent="error"
                        )
                if response_data.get("text"):
                    success = await send_whatsapp_message(phone_number, response_data["text"], remotejid=user_id)
                    if not success:
                        logger.error(f"[{user_id}] Falha ao enviar mensagem para o WhatsApp: {response_data['text']}")

        try:
            if response_data.get("text"):
                await client.beta.threads.messages.create(  # Using OpenAI client
                    thread_id=thread_id,
                    role="assistant",
                    content=response_data["text"]
                )
                logger.debug(f"Added assistant response to thread {thread_id}: {response_data}")
        except Exception as e:
            logger.error(f"Failed to add assistant response to thread {thread_id}: {str(e)}")
            response_data = build_response_data(
                text=f"Erro ao salvar resposta do assistente: {str(e)}. Por favor, tente novamente ou contate o suporte: wa.me/5537987654321.",
                metadata={"intent": "error", "phone_number": klingo_phone, "clinic_id": clinic_id},
                intent="error"
            )
            success = await send_whatsapp_message(phone_number, response_data["text"], remotejid=user_id)

        if success:
            logger.info(f"[{user_id}] Mensagem enviada com sucesso")
            return {"status": "success", "message": "Processed and responded"}
        else:
            logger.error(f"[{user_id}] Falha ao enviar resposta para o WhatsApp")
            return {"status": "error", "message": "Failed to send response"}

    except Exception as e:
        logger.error(f"Erro ao processar webhook: {str(e)}")
        return {"status": "error", "message": f"Error processing webhook: {str(e)}"}