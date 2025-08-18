import aiohttp
import re
import json
import base64
import os
import tempfile
import hashlib
from typing import Optional, Dict, Any
from config.config import EVOLUTION_API_URL, SUPABASE_URL, SUPABASE_KEY
from supabase import AsyncClient, acreate_client
from utils.image_processing import resize_image_to_thumbnail
from utils.logging_setup import setup_logging
from openai import AsyncOpenAI
from config.config import OPENAI_API_KEY
from tenacity import retry, stop_after_attempt, wait_exponential

logger = setup_logging()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

async def get_instance_details(clinic_id: str = None, phone_number: str = None, supabase: AsyncClient = None) -> Dict[str, str]:
    """Fetch Evolution API instance details from Supabase."""
    if not (clinic_id or phone_number):
        logger.error("No clinic_id or phone_number provided")
        raise ValueError("clinic_id or phone_number required")
    
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("SUPABASE_URL or SUPABASE_KEY not configured")
        raise ValueError("SUPABASE_URL or SUPABASE_KEY not configured")
    
    if not supabase:
        try:
            supabase = await acreate_client(SUPABASE_URL, SUPABASE_KEY)
        except Exception as e:
            logger.error(f"Failed to create Supabase client: {str(e)}")
            raise
    
    query = supabase.table("clinic_instances").select("instance_name, api_key, status")
    if clinic_id:
        query = query.eq("clinic_id", clinic_id)
    elif phone_number:
        query = query.eq("phone_number", phone_number)
    
    try:
        response = await query.eq("status", "connected").single().execute()
        if not response.data:
            logger.error(f"No active instance found for {'clinic_id' if clinic_id else 'phone_number'}")
            raise ValueError(f"No active instance found for {'clinic_id' if clinic_id else 'phone_number'}")
        logger.debug(f"Found instance details: {response.data}")
        return {
            "instance_name": response.data["instance_name"],
            "api_key": response.data["api_key"]
        }
    except Exception as e:
        logger.error(f"Error fetching instance details: {str(e)}")
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def send_whatsapp_message(phone_number: str, message: str, remotejid: Optional[str] = None, message_key_id: Optional[str] = None, message_text: Optional[str] = None, clinic_id: Optional[str] = None, supabase: AsyncClient = None) -> bool:
    if not EVOLUTION_API_URL:
        logger.error("EVOLUTION_API_URL is not configured")
        return False
    
    try:
        instance_details = await get_instance_details(clinic_id, phone_number, supabase)
        instance_name = instance_details["instance_name"]
        api_key = instance_details["api_key"]
    except Exception as e:
        logger.error(f"[{remotejid}] Failed to get instance details: {str(e)}")
        return False
    
    if not all([instance_name, api_key]):
        logger.error("Instance name or API key missing")
        return False
    
    # Manter o formato original do phone_number (ex.: 558496248451@s.whatsapp.net)
    remotejid = remotejid or phone_number
    message = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1: \2', message)
    payload = {
        "number": phone_number,  # Usar o número completo, incluindo @s.whatsapp.net
        "text": message,
        "options": {"delay": 0, "presence": "composing"}
    }
    if message_key_id and message_text:
        payload["quoted"] = {
            "key": {"id": message_key_id},
            "message": {"conversation": message_text}
        }
    url = f"{EVOLUTION_API_URL}/message/sendText/{instance_name}"
    headers = {"apikey": api_key, "Content-Type": "application/json"}
    logger.debug(f"[{remotejid}] Sending message to: {phone_number}, payload: {json.dumps(payload, indent=2)}")
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, json=payload) as response:
                response_text = await response.text()
                logger.debug(f"[{remotejid}] Response from sendText: {response.status} - {response_text}")
                if response.status not in (200, 201):
                    logger.error(f"[{remotejid}] Failed to send: {response.status} - {response_text}")
                    return False
                logger.info(f"[{remotejid}] Message sent successfully")
                return True
    except NameError as ne:
        logger.error(f"[{remotejid}] NameError in send_whatsapp_message: {str(ne)}")
        raise
    except Exception as e:
        logger.error(f"[{remotejid}] Error sending message: {str(e)}")
        return False

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def send_whatsapp_audio(phone_number: str, audio_path: str, remotejid: Optional[str] = None, message_key_id: Optional[str] = None, message_text: Optional[str] = None, clinic_id: Optional[str] = None, supabase: AsyncClient = None) -> bool:
    if not EVOLUTION_API_URL:
        logger.error("EVOLUTION_API_URL is not configured")
        return False
    
    instance_details = await get_instance_details(clinic_id, phone_number, supabase)
    instance_name = instance_details["instance_name"]
    api_key = instance_details["api_key"]
    
    if not all([instance_name, api_key]):
        logger.error("Instance name or API key missing")
        return False
    
    phone_number = phone_number.replace("@s.whatsapp.net", "") if "@s.whatsapp.net" in phone_number else phone_number
    remotejid = remotejid or f"{phone_number}@s.whatsapp.net"
    try:
        if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
            logger.error(f"[{remotejid}] Invalid or empty audio file: {audio_path}")
            return False
        with open(audio_path, "rb") as audio_file:
            audio_data = base64.b64encode(audio_file.read()).decode("utf-8")
        payload = {
            "number": phone_number,
            "media": audio_data,
            "mediatype": "audio",
            "mimetype": "audio/mpeg; codecs=opus",
            "options": {
                "delay": 0,
                "presence": "recording",
                "linkPreview": False,
                "mentionsEveryOne": False,
                "mentioned": [remotejid] if remotejid else []
            }
        }
        if message_key_id and message_text:
            payload["quoted"] = {
                "key": {"id": message_key_id},
                "message": {"conversation": message_text}
            }
        url = f"{EVOLUTION_API_URL}/message/sendMedia/{instance_name}"
        headers = {"apikey": api_key, "Content-Type": "application/json"}
        logger.debug(f"[{remotejid}] Sending audio, payload: {json.dumps(payload, indent=2, ensure_ascii=False)}")
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, json=payload) as response:
                response_text = await response.text()
                logger.debug(f"[{remotejid}] Response from sendMedia: {response.status} - {response_text}")
                if response.status not in (200, 201):
                    logger.error(f"[{remotejid}] Failed to send audio: {response.status} - {response_text}")
                    return False
                logger.info(f"[{remotejid}] Audio sent successfully")
                return True
    except Exception as e:
        logger.error(f"[{remotejid}] Error sending audio: {e}")
        return False

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def send_whatsapp_image(phone_number: str, image_url: str, caption: str, remotejid: Optional[str] = None, message_key_id: Optional[str] = None, message_text: Optional[str] = None, clinic_id: Optional[str] = None, supabase: AsyncClient = None) -> bool:
    if not EVOLUTION_API_URL:
        logger.error("EVOLUTION_API_URL is not configured")
        return False
    
    instance_details = await get_instance_details(clinic_id, phone_number, supabase)
    instance_name = instance_details["instance_name"]
    api_key = instance_details["api_key"]
    
    if not all([instance_name, api_key]):
        logger.error("Instance name or API key missing")
        return False
    
    phone_number = phone_number.replace("@s.whatsapp.net", "") if "@s.whatsapp.net" in phone_number else phone_number
    remotejid = remotejid or f"{phone_number}@s.whatsapp.net"
    try:
        payload = {
            "number": phone_number,
            "mediatype": "image",
            "mimetype": "image/jpeg",
            "media": image_url,
            "caption": caption,
            "options": {
                "delay": 0,
                "presence": "composing",
                "linkPreview": False,
                "mentionsEveryOne": False,
                "mentioned": [remotejid] if remotejid else []
            }
        }
        if message_key_id and message_text:
            payload["quoted"] = {
                "key": {"id": message_key_id},
                "message": {"conversation": message_text}
            }
        url = f"{EVOLUTION_API_URL}/message/sendMedia/{instance_name}"
        headers = {"apikey": api_key, "Content-Type": "application/json"}
        logger.debug(f"[{remotejid}] Sending image, payload: {json.dumps(payload, indent=2, ensure_ascii=False)}")
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, json=payload) as response:
                response_text = await response.text()
                logger.debug(f"[{remotejid}] Response from sendMedia: {response.status} - {response_text}")
                if response.status not in (200, 201):
                    logger.error(f"[{remotejid}] Failed to send image: {response.status} - {response_text}")
                    return False
                logger.info(f"[{remotejid}] Image sent successfully")
                return True
    except Exception as e:
        logger.error(f"[{remotejid}] Error sending image: {e}")
        return False

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def fetch_media_base64(message_key_id: str, media_type: str, remotejid: Optional[str] = None, clinic_id: Optional[str] = None, supabase: AsyncClient = None) -> Dict[str, Any]:
    if not EVOLUTION_API_URL:
        logger.error("EVOLUTION_API_URL is not configured")
        return {"error": "EVOLUTION_API_URL is not configured"}
    
    instance_details = await get_instance_details(clinic_id, remotejid, supabase)
    instance_name = instance_details["instance_name"]
    api_key = instance_details["api_key"]
    
    if not all([instance_name, api_key]):
        logger.error("Instance name or API key missing")
        return {"error": "Instance name or API key missing"}
    
    url = f"{EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{instance_name}"
    headers = {"apikey": api_key, "Content-Type": "application/json"}
    payload = {
        "message": {
            "key": {
                "id": message_key_id
            }
        },
        "convertToMp4": media_type == "image"
    }
    logger.debug(f"[{remotejid}] Fetching base64 for {media_type} with message_key_id: {message_key_id}, payload: {json.dumps(payload, indent=2)}")
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(url, json=payload) as response:
                response_text = await response.text()
                logger.debug(f"[{remotejid}] Response from getBase64FromMediaMessage: {response.status} - {response_text}")
                if response.status not in (200, 201):
                    logger.error(f"[{remotejid}] Failed to fetch base64: {response.status} - {response_text}")
                    return {"error": f"Failed to fetch base64: {response.status}"}
                response_data = await response.json()
                base64_data = response_data.get("base64")
                if not base64_data:
                    logger.error(f"[{remotejid}] No base64 data returned by API")
                    return {"error": "No base64 data returned"}

                logger.debug(f"[{remotejid}] First 50 chars of base64: {base64_data[:50]}")
                
                try:
                    decoded_data = base64.b64decode(base64_data, validate=True)
                    if media_type == "image":
                        if decoded_data.startswith(b'\xff\xd8\xff'):
                            mimetype = "image/jpeg"
                        elif decoded_data.startswith(b'\x89PNG\r\n\x1a\n'):
                            mimetype = "image/png"
                        else:
                            logger.warning(f"[{remotejid}] Unknown image format")
                            return {"error": "Unknown image format"}
                        thumbnail_data = await resize_image_to_thumbnail(decoded_data)
                        if not thumbnail_data:
                            logger.warning(f"[{remotejid}] Failed to generate thumbnail, using original image")
                            thumbnail_data = base64_data
                        logger.info(f"[{remotejid}] Image base64 obtained successfully, mimetype: {mimetype}")
                        return {"type": "image", "base64": thumbnail_data, "mimetype": mimetype}
                    elif media_type == "audio":
                        if decoded_data.startswith(b'OggS'):
                            mimetype = "audio/ogg"
                        elif decoded_data.startswith(b'ID3') or decoded_data.startswith(b'\xff\xfb'):
                            mimetype = "audio/mpeg"
                        else:
                            logger.warning(f"[{remotejid}] Unknown audio format")
                            return {"error": "Unknown audio format"}
                        temp_path = os.path.join(tempfile.gettempdir(), f"audio_temp_{hashlib.md5(base64_data.encode()).hexdigest()}.ogg")
                        with open(temp_path, "wb") as f:
                            f.write(decoded_data)
                        logger.debug(f"[{remotejid}] Audio file saved: {temp_path}")
                        with open(temp_path, "rb") as audio_file:
                            transcription = await client.audio.transcriptions.create(
                                model="whisper-1",
                                file=audio_file,
                                language="pt"
                            )
                        logger.info(f"[{remotejid}] Audio transcribed successfully: {transcription.text}")
                        os.remove(temp_path)
                        logger.debug(f"[{remotejid}] Temporary file removed: {temp_path}")
                        return {"type": "audio", "transcription": transcription.text}
                    else:
                        logger.error(f"[{remotejid}] Unsupported media type: {media_type}")
                        return {"error": f"Unsupported media type: {media_type}"}
                except Exception as e:
                    logger.error(f"[{remotejid}] Error verifying or processing media: {str(e)}")
                    return {"error": f"Error verifying or processing media: {str(e)}"}
    except Exception as e:
        logger.error(f"[{remotejid}] Error fetching base64 from Evolution API: {str(e)}")
        return {"error": f"Error fetching base64: {str(e)}"}