# main.py
from fastapi import FastAPI, Request
import json
import re
from openai import AsyncOpenAI
from config.config import OPENAI_API_KEY, SUPABASE_URL, SUPABASE_KEY
from supabase import create_client, Client
from tools.supabase_tools import get_lead, upsert_lead
from tools.whatsapp_tools import send_whatsapp_message, send_whatsapp_audio, send_whatsapp_image, fetch_media_base64
from tools.audio_tools import text_to_speech
from tools.image_tools import analyze_image
from tools.extract_lead_info import extract_lead_info
from utils.image_processing import resize_image_to_thumbnail
from models.lead_data import LeadData
from bot_agents.triage_agent import triage_agent
from agents import Runner
from utils.logging_setup import setup_logging
from datetime import datetime
import os
from typing import Dict, Optional
import base64
from tools.asaas_tools import _get_customer_by_cpf, _create_customer, _create_payment_link
from tools.klingo_tools import fetch_klingo_schedule

logger = setup_logging()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()
threads = {}

logger.info("Running main.py version with Asaas and Klingo tools")

async def get_or_create_thread(user_id: str, push_name: Optional[str] = None) -> str:
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
                thread_id=lead["thread_id"]
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
        thread_id=thread.id
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

@app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        logger.info(f"Payload recebido: {data}")
        
        user_id = data.get("data", {}).get("key", {}).get("remoteJid", "")
        phone_number = user_id
        push_name = data.get("data", {}).get("pushName", None)
        logger.debug(f"Extracted phone_number: {phone_number}, user_id: {user_id}, pushName: {push_name}")

        if not phone_number or not user_id or user_id == "unknown" or "@s.whatsapp.net" not in user_id:
            logger.warning("Nenhum número de telefone ou user_id válido encontrado no payload")
            return {"status": "error", "message": "No valid phone number or user_id found"}

        thread_id = await get_or_create_thread(user_id, push_name=push_name)
        thread_history = await get_thread_history(thread_id)
        logger.debug(f"Thread history for {thread_id}: {thread_history}")

        message_data = data.get("data", {}).get("message", {})
        message = None
        is_audio_message = False
        is_image_message = False
        message_key_id = data.get("data", {}).get("key", {}).get("id", "")
        response_data = {"text": "Desculpe, houve um problema ao processar sua mensagem. Como posso ajudar?"}

        # Check for CPF and user-provided name in message
        cpf_cnpj = None
        user_provided_name = None
        if message_data.get("conversation"):
            message = message_data["conversation"]
            cpf_match = re.search(r'\b\d{3}\.\d{3}\.\d{3}-\d{2}\b|\b\d{11}\b', message)
            if cpf_match:
                cpf_cnpj = cpf_match.group(0).replace(".", "").replace("-", "")
            name_match = re.search(r'nome:\s*([^\n]+)', message, re.IGNORECASE)
            if name_match:
                user_provided_name = name_match.group(1).strip().capitalize()

        # Handle CPF for Asaas payment workflow
        if cpf_cnpj:
            try:
                lead = await get_lead(user_id)
                nome_cliente = user_provided_name or lead.get("nome_cliente", push_name or "Cliente OtorrinoMed")
                telefone = lead.get("telefone")
                if telefone and telefone.startswith("55") and len(telefone) == 12 and telefone.isdigit():
                    raw_phone = telefone[2:]
                    if len(raw_phone) == 10 and raw_phone.isdigit():
                        telefone = f"{raw_phone[:2]}9{raw_phone[2:]}"
                        logger.debug(f"[{user_id}] Transformed phone number from Supabase: {lead.get('telefone')} to {telefone}")
                if not telefone or not telefone.isdigit() or len(telefone) != 11:
                    raw_phone = user_id.replace("55", "").split("@")[0]
                    if len(raw_phone) == 10 and raw_phone.isdigit():
                        telefone = f"{raw_phone[:2]}9{raw_phone[2:]}"
                        logger.debug(f"[{user_id}] Derived phone number for Asaas: {telefone}")
                    else:
                        logger.error(f"[{user_id}] Invalid remotejid format for phone derivation: {raw_phone}")
                        response_data = {"text": "Erro: Não foi possível derivar o número de telefone. Por favor, contate o suporte: wa.me/5537987654321."}
                        await send_whatsapp_message(phone_number, response_data["text"], remotejid=user_id)
                        return {"status": "error", "message": "Invalid phone number derivation"}

                if telefone and (not telefone.isdigit() or len(telefone) != 11):
                    logger.error(f"[{user_id}] Invalid phone number format: {telefone}")
                    response_data = {"text": "Erro: Formato de telefone inválido. Por favor, contate o suporte: wa.me/5537987654321."}
                    await send_whatsapp_message(phone_number, response_data["text"], remotejid=user_id)
                    return {"status": "error", "message": f"Invalid phone number format: {telefone}"}

                customer_data = await _get_customer_by_cpf(cpf_cnpj, user_id)
                customer_json = json.loads(customer_data)
                if customer_json.get("data") and len(customer_json["data"]) > 0:
                    customer_id = customer_json["data"][0]["id"]
                    logger.info(f"[{user_id}] Customer found: {customer_id}")
                else:
                    logger.info(f"[{user_id}] No customer found, creating new customer for CPF {cpf_cnpj}")
                    customer_result = await _create_customer(cpf_cnpj, nome_cliente, None, telefone, user_id)
                    customer_json = json.loads(customer_result)
                    if "id" in customer_json:
                        customer_id = customer_json["id"]
                        logger.info(f"[{user_id}] Created customer: {customer_id}")
                    else:
                        logger.error(f"[{user_id}] Failed to create customer: {customer_json}")
                        response_data = {"text": "Erro ao criar cliente no Asaas. Por favor, tente novamente ou contate o suporte: wa.me/5537987654321."}
                        await send_whatsapp_message(phone_number, response_data["text"], remotejid=user_id)
                        return {"status": "error", "message": "Failed to create customer"}

                payment_result = await _create_payment_link(customer_id, 300.00, "Consulta OtorrinoMed", user_id)
                payment_json = json.loads(payment_result)
                if "invoiceUrl" in payment_json:
                    response_data = {"text": f"Seu CPF foi encontrado! Acesse o link de pagamento para sua consulta: {payment_json['invoiceUrl']}"}
                    await upsert_lead(user_id, LeadData(
                        remotejid=user_id,
                        cpf_cnpj=cpf_cnpj,
                        asaas_customer_id=customer_id,
                        nome_cliente=nome_cliente,
                        telefone=telefone
                    ))
                else:
                    logger.error(f"[{user_id}] Failed to create payment link: {payment_json}")
                    response_data = {"text": "Erro ao criar link de pagamento. Por favor, tente novamente ou contate o suporte: wa.me/5537987654321."}

                success = await send_whatsapp_message(phone_number, response_data["text"], remotejid=user_id)
                if success:
                    logger.info(f"[{user_id}] Mensagem enviada com sucesso")
                    await client.beta.threads.messages.create(
                        thread_id=thread_id,
                        role="assistant",
                        content=response_data["text"]
                    )
                    return {"status": "success", "message": "Processed and responded"}
                else:
                    logger.error(f"[{user_id}] Falha ao enviar resposta para o WhatsApp")
                    return {"status": "error", "message": "Failed to send response"}

            except Exception as e:
                logger.error(f"[{user_id}] Erro ao processar CPF {cpf_cnpj}: {str(e)}")
                response_data = {"text": "Erro ao processar seu CPF. Por favor, tente novamente ou contate o suporte: wa.me/5537987654321."}
                await send_whatsapp_message(phone_number, response_data["text"], remotejid=user_id)
                return {"status": "error", "message": f"Error processing CPF: {str(e)}"}

        # Prepare context for agent
        lead = await get_lead(user_id)
        nome_cliente = user_provided_name or lead.get("nome_cliente", push_name or "Cliente OtorrinoMed")
        # Derive phone number for agent context
        telefone = lead.get("telefone")
        if telefone and telefone.startswith("55") and len(telefone) == 12 and telefone.isdigit():
            raw_phone = telefone[2:]
            if len(raw_phone) == 10 and raw_phone.isdigit():
                telefone = f"{raw_phone[:2]}9{raw_phone[2:]}"
                logger.debug(f"[{user_id}] Transformed phone number for agent context: {lead.get('telefone')} to {telefone}")
        if not telefone or not telefone.isdigit() or len(telefone) != 11:
            raw_phone = user_id.replace("55", "").split("@")[0]
            if len(raw_phone) == 10 and raw_phone.isdigit():
                telefone = f"{raw_phone[:2]}9{raw_phone[2:]}"
                logger.debug(f"[{user_id}] Derived phone number for agent context: {telefone}")
            else:
                telefone = None
                logger.warning(f"[{user_id}] Unable to derive valid phone number for agent context")
        
        context = {
            "remotejid": user_id,
            "phone_number": telefone,
            "name": nome_cliente
        }
        if cpf_cnpj:
            context["cpf_cnpj"] = cpf_cnpj
        # Add scheduling metadata from Supabase
        if lead.get("scheduling_metadata"):
            context["scheduling_metadata"] = lead["scheduling_metadata"]

        # Handle audio messages
        if message_data.get("audioMessage"):
            is_audio_message = True
            media_result = await fetch_media_base64(message_key_id, "audio", remotejid=user_id)
            if "error" in media_result:
                logger.error(f"[{user_id}] Falha ao processar áudio: {media_result['error']}")
                response_data = {"text": f"Falha ao processar áudio: {media_result['error']}"}
            elif media_result.get("type") == "audio":
                message = media_result["transcription"]
                logger.info(f"Transcribed audio to: {message}")

        # Handle image messages
        elif message_data.get("imageMessage"):
            is_image_message = True
            logger.info(f"[{user_id}] Buscando imagem completa via fetch_media_base64")
            try:
                media_result = await fetch_media_base64(message_key_id, "image", remotejid=user_id)
                if "error" in media_result:
                    logger.error(f"[{user_id}] Falha ao buscar imagem completa: {media_result['error']}")
                    response_data = {"text": f"Falha ao buscar imagem completa: {media_result['error']}"}
                elif media_result.get("type") == "image":
                    base64_data = media_result["base64"]
                    mimetype = media_result["mimetype"]
                    logger.debug(f"[{user_id}] Imagem completa obtida, mimetype: {mimetype}, tamanho base64: {len(base64_data)}")
                    decoded_data = base64.b64decode(base64_data)
                    resized_base64 = await resize_image_to_thumbnail(decoded_data, max_size=512)
                    if not resized_base64:
                        logger.error(f"[{user_id}] Falha ao redimensionar imagem")
                        response_data = {"text": "Falha ao redimensionar imagem. Por favor, envie outra imagem ou descreva sua solicitação."}
                    else:
                        image_description = await analyze_image(content=resized_base64, mimetype=mimetype)
                        try:
                            image_data = json.loads(image_description)
                            if image_data.get("is_medical_document"):
                                lead_data = LeadData(remotejid=user_id)
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
                response_data = {"text": f"Erro ao processar imagem: {str(e)}"}

        # Handle general messages (non-CPF)
        if message and not cpf_cnpj:
            try:
                full_message = f"Histórico da conversa:\n{thread_history}\n\nNova mensagem: {message}"
                await client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=message
                )
                logger.debug(f"Added user message to thread {thread_id}: {message}")
                response = await Runner.run(triage_agent, input=full_message, context=context)
                logger.debug(f"Raw RunResult: {response}")
                response_data = str(response.final_output)
                logger.info(f"Raw agent response (final_output): {response_data}")
                try:
                    if response_data.startswith("```json") and response_data.endswith("```"):
                        response_data = response_data[7:-3].strip()
                    response_data = json.loads(response_data)
                    if not isinstance(response_data, dict):
                        logger.warning(f"Agent response is JSON but not a dict: {response_data}")
                        response_data = {"text": str(response_data)}
                except json.JSONDecodeError:
                    logger.warning(f"Resposta não é um JSON válido, tratando como texto puro: {response_data}")
                    response_data = {"text": response_data}
                
                # Handle scheduling steps
                if response_data.get("metadata", {}).get("intent") == "scheduling":
                    lead_data = LeadData(remotejid=user_id)
                    if response_data["metadata"].get("step") == "select_date":
                        # Check if user selected a date
                        selected_date = None
                        for date in response_data["metadata"]["schedules"].keys():
                            if date.lower() in message.lower():
                                selected_date = date
                                break
                        if selected_date:
                            times = response_data["metadata"]["schedules"][selected_date]["times"][:3]  # Limit to 3 times
                            slot_id = response_data["metadata"]["schedules"][selected_date]["slot_id"]
                            response_data = {
                                "text": (
                                    f"Ótimo, {nome_cliente}! Para o dia {selected_date}, os horários disponíveis com "
                                    f"{response_data['metadata']['doctor_name']} são: {', '.join(times)}. "
                                    "Por favor, escolha um horário."
                                ),
                                "metadata": {
                                    "intent": "scheduling",
                                    "step": "select_time",
                                    "doctor_id": response_data["metadata"]["doctor_id"],
                                    "doctor_name": response_data["metadata"]["doctor_name"],
                                    "selected_date": selected_date,
                                    "slot_id": slot_id,
                                    "times": times
                                }
                            }
                            # Store scheduling metadata in Supabase
                            lead_data.scheduling_metadata = response_data["metadata"]
                            await upsert_lead(user_id, lead_data)
                        else:
                            response_data["text"] = (
                                f"Por favor, {nome_cliente}, escolha uma das datas disponíveis: "
                                f"{', '.join(response_data['metadata']['schedules'].keys())}."
                            )
                    elif response_data["metadata"].get("step") == "select_doctor":
                        # Check if user selected a doctor
                        selected_doctor_id = None
                        for doctor_name, doctor_data in response_data["metadata"]["schedules"].items():
                            if doctor_name.lower() in message.lower():
                                selected_doctor_id = doctor_data["doctor_id"]
                                break
                        if selected_doctor_id:
                            schedule_result = await fetch_klingo_schedule(professional_id=selected_doctor_id, user_id=user_id)
                            schedule_data = json.loads(schedule_result)
                            if "error" in schedule_data:
                                response_data = {"text": f"Desculpe, {nome_cliente}, não foi possível encontrar horários para o médico selecionado. Tente outro médico ou entre em contato com o suporte: wa.me/5537987654321."}
                            else:
                                dates = schedule_data.get("dates", [])
                                if not dates:
                                    response_data = {"text": f"Desculpe, {nome_cliente}, não há horários disponíveis para {schedule_data['doctor_name']} nos próximos 5 dias. Deseja escolher outro médico?"}
                                else:
                                    response_data = {
                                        "text": (
                                            f"Olá, {nome_cliente}! Aqui estão as datas disponíveis para consulta com "
                                            f"{schedule_data['doctor_name']}:\n{', '.join(dates)}\n"
                                            "Por favor, escolha uma data."
                                        ),
                                        "metadata": {
                                            "intent": "scheduling",
                                            "step": "select_date",
                                            "doctor_id": selected_doctor_id,
                                            "doctor_name": schedule_data["doctor_name"],
                                            "schedules": schedule_data["schedules"]
                                        }
                                    }
                                    # Store scheduling metadata in Supabase
                                    lead_data.scheduling_metadata = response_data["metadata"]
                                    await upsert_lead(user_id, lead_data)
                        else:
                            response_data["text"] = (
                                f"Por favor, {nome_cliente}, escolha um dos médicos disponíveis: "
                                f"{', '.join(response_data['metadata']['schedules'].keys())}."
                            )
                    elif response_data["metadata"].get("step") == "select_time":
                        # Check if user selected a time
                        selected_time = None
                        for time in response_data["metadata"]["times"]:
                            if time.lower() in message.lower():
                                selected_time = time
                                break
                        if selected_time:
                            slot_id = response_data["metadata"]["slot_id"] + f"|{selected_time}"
                            response_data = {
                                "text": (
                                    f"Perfeito, {nome_cliente}! Sua consulta com {response_data['metadata']['doctor_name']} "
                                    f"está pré-agendada para {response_data['metadata']['selected_date']} às {selected_time}. "
                                    "Por favor, forneça seu CPF ou data de nascimento para confirmarmos sua identidade."
                                ),
                                "metadata": {
                                    "intent": "scheduling",
                                    "step": "identify_patient",
                                    "doctor_id": response_data["metadata"]["doctor_id"],
                                    "doctor_name": response_data["metadata"]["doctor_name"],
                                    "selected_date": response_data["metadata"]["selected_date"],
                                    "selected_time": selected_time,
                                    "slot_id": slot_id
                                }
                            }
                            # Store scheduling metadata in Supabase
                            lead_data.scheduling_metadata = response_data["metadata"]
                            await upsert_lead(user_id, lead_data)
                        else:
                            response_data["text"] = (
                                f"Por favor, {nome_cliente}, escolha um dos horários disponíveis: "
                                f"{', '.join(response_data['metadata']['times'])}."
                            )
                
            except Exception as e:
                logger.error(f"Failed to process message in thread {thread_id}: {str(e)}")
                response_data = {"text": f"Erro ao processar mensagem: {str(e)}"}

        # Extract and save lead information
        if message and not is_image_message:
            try:
                extracted_info = await extract_lead_info(message, remotejid=user_id)
                extracted_data = json.loads(extracted_info)
                if "error" not in extracted_data:
                    lead_data = LeadData(**extracted_data)
                    if user_provided_name:
                        lead_data.nome_cliente = user_provided_name
                    await upsert_lead(user_id, lead_data)
                    logger.debug(f"[{user_id}] Lead data saved: {lead_data.dict(exclude_unset=True)}")
            except Exception as e:
                logger.error(f"[{user_id}] Failed to extract or save lead info: {str(e)}")

        # Handle response sending
        success = False
        if is_audio_message and response_data.get("text"):
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
                response_data = {"text": "Desculpe, houve um problema ao gerar o áudio. Como posso ajudar?"}
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
                        response_data = {"text": ""}  # Clear text to avoid duplication
                    else:
                        logger.error(f"[{user_id}] Falha ao enviar imagem: {image_url}")
                        response_data = {"text": "Desculpe, houve um problema ao enviar a imagem."}
                if response_data.get("text"):  # Only send text if not empty
                    success = await send_whatsapp_message(phone_number, response_data["text"], remotejid=user_id)

        # Save assistant response
        try:
            if response_data.get("text"):
                await client.beta.threads.messages.create(
                    thread_id=thread_id,
                    role="assistant",
                    content=response_data.get("text", json.dumps(response_data))
                )
                logger.debug(f"Added assistant response to thread {thread_id}: {response_data}")
        except Exception as e:
            logger.error(f"Failed to add assistant response to thread {thread_id}: {str(e)}")
            response_data = {"text": f"Erro ao salvar resposta do assistente: {str(e)}"}
            success = await send_whatsapp_message(phone_number, response_data["text"], remotejid=user_id)

        # Update additional lead info
        lead_data = LeadData(remotejid=user_id)
        if message and "cidade:" in message.lower():
            lead_data.cidade = message.lower().split("cidade:")[1].strip().split()[0]
        if message and "estado:" in message.lower():
            lead_data.estado = message.lower().split("estado:")[1].strip().split()[0]
        if message and "email:" in message.lower():
            lead_data.email = message.lower().split("email:")[1].strip().split()[0]
        if user_provided_name:
            lead_data.nome_cliente = user_provided_name
        if any(field is not None for field in [lead_data.cidade, lead_data.estado, lead_data.email, lead_data.nome_cliente]):
            logger.debug(f"Updating lead with additional info: {lead_data.dict(exclude_unset=True)}")
            await upsert_lead(user_id, lead_data)

        if success:
            logger.info(f"[{user_id}] Mensagem enviada com sucesso")
            return {"status": "success", "message": "Processed and responded"}
        else:
            logger.error(f"[{user_id}] Falha ao enviar resposta para o WhatsApp")
            return {"status": "error", "message": "Failed to send response"}

    except Exception as e:
        logger.error(f"Erro ao processar webhook: {str(e)}")
        return {"status": "error", "message": f"Error processing webhook: {str(e)}"}