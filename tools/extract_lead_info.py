# tools/extract_lead_info.py
import re
import json
from typing import Dict, Optional
from openai import AsyncOpenAI
from config.config import OPENAI_API_KEY
from models.lead_data import LeadData
from utils.logging_setup import setup_logging
from datetime import datetime

logger = setup_logging()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

async def extract_lead_info(message: str, remotejid: Optional[str] = None) -> str:
    """Extract patient information from a message and return as JSON."""
    logger.debug(f"Executing extract_lead_info for message: {message}, remotejid: {remotejid}")
    try:
        lead_data = LeadData(remotejid=remotejid)
        extracted_data = {}

        # Regex patterns for structured fields
        patterns = {
            "email": r'[\w\.-]+@[\w\.-]+\.\w+',
            "cep": r'\d{5}-?\d{3}',  # Accepts both xxxxx-xxx and xxxxxxxx
            "data_nascimento": r'\b(\d{2}/\d{2}/\d{4})\b',
            "cpf_cnpj": r'\b(\d{3}\.\d{3}\.\d{3}-\d{2}|\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})\b'
        }

        # Extract structured fields
        for field, pattern in patterns.items():
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                extracted_data[field] = match.group(0)
                setattr(lead_data, field, match.group(0))

        # Detect language using OpenAI
        try:
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "user",
                        "content": f"Detecte o idioma principal da mensagem: '{message}'. Retorne apenas o nome do idioma (ex.: 'português')."
                    }
                ],
                temperature=0.2
            )
            idioma = response.choices[0].message.content.strip()
            if idioma:
                extracted_data["idioma"] = idioma
                lead_data.idioma = idioma
        except Exception as e:
            logger.error(f"[{remotejid}] Erro ao detectar idioma: {str(e)}")

        # Detect symptoms using OpenAI
        try:
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "user",
                        "content": f"""
                        Analise a mensagem: '{message}'.
                        Identifique quaisquer sintomas mencionados (ex.: dor de ouvido, zumbido, dificuldade para respirar).
                        Retorne apenas os sintomas identificados (ex.: 'dor de ouvido') ou 'nenhum' se não houver menção.
                        """
                    }
                ],
                temperature=0.2
            )
            sintomas = response.choices[0].message.content.strip()
            if sintomas != "nenhum":
                extracted_data["sintomas"] = sintomas
                lead_data.sintomas = sintomas
        except Exception as e:
            logger.error(f"[{remotejid}] Erro ao detectar sintomas: {str(e)}")

        # Detect consultation type using OpenAI
        try:
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "user",
                        "content": f"""
                        Analise a mensagem: '{message}'.
                        Determine o tipo de consulta desejada (ex.: otorrino, fonoaudiologia, exame).
                        Exemplos:
                        - "Quero marcar uma consulta" → otorrino
                        - "Preciso de um exame auditivo" → exame
                        - "Quero uma consulta com fonoaudiólogo" → fonoaudiologia
                        Retorne apenas o tipo de consulta (ex.: 'otorrino') ou 'nenhum' se não for mencionado.
                        """
                    }
                ],
                temperature=0.2
            )
            consulta_type = response.choices[0].message.content.strip()
            if consulta_type != "nenhum":
                extracted_data["consulta_type"] = consulta_type
                lead_data.consulta_type = consulta_type
        except Exception as e:
            logger.error(f"[{remotejid}] Erro ao detectar tipo de consulta: {str(e)}")

        # Detect médico using OpenAI
        try:
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "user",
                        "content": f"""
                        Analise a mensagem: '{message}'.
                        Identifique o nome do médico mencionado, se houver (ex.: 'Dr. João', 'Dra. Maria').
                        Retorne apenas o nome do médico (ex.: 'Dr. João') ou 'nenhum' se não for mencionado.
                        """
                    }
                ],
                temperature=0.2
            )
            medico = response.choices[0].message.content.strip()
            if medico != "nenhum":
                extracted_data["medico"] = medico
                lead_data.medico = medico
        except Exception as e:
            logger.error(f"[{remotejid}] Erro ao detectar médico: {str(e)}")

        # Extract cidade, estado, and nome_cliente
        if "cidade:" in message.lower():
            extracted_data["cidade"] = message.lower().split("cidade:")[1].strip().split()[0]
            lead_data.cidade = extracted_data["cidade"]
        if "estado:" in message.lower():
            extracted_data["estado"] = message.lower().split("estado:")[1].strip().split()[0]
            lead_data.estado = extracted_data["estado"]
        if "nome:" in message.lower():
            extracted_data["nome_cliente"] = message.lower().split("nome:")[1].strip().capitalize()
            lead_data.nome_cliente = extracted_data["nome_cliente"]

        # Update ult_contato
        extracted_data["ult_contato"] = datetime.now().isoformat()
        lead_data.ult_contato = extracted_data["ult_contato"]

        logger.info(f"[{remotejid}] Extracted lead info: {extracted_data}")
        return json.dumps(extracted_data)
    except Exception as e:
        logger.error(f"[{remotejid}] Erro ao extrair informações do lead: {str(e)}")
        return json.dumps({"error": f"Erro ao extrair informações: {str(e)}"})