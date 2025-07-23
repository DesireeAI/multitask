# tools/image_tools.py
import base64
from openai import AsyncOpenAI
from config.config import OPENAI_API_KEY
from utils.logging_setup import setup_logging
from tenacity import retry, stop_after_attempt, wait_exponential
import json
import re

logger = setup_logging()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
async def analyze_image(content: str, mimetype: str = "image/jpeg") -> str:
    """Analyze an image and return a description, focusing on medical documents like prescriptions."""
    logger.debug(f"Analisando imagem... (tamanho base64: {len(content)})")
    try:
        # Ensure content is base64-encoded
        match = re.match(r"^data:image/(?P<fmt>\w+);base64,(?P<data>.+)", content)
        if match:
            mimetype = f"image/{match.group('fmt')}"
            base64_data = match.group('data')
            logger.info(f"Data URI detectado. Formato: {mimetype}")
        else:
            base64_data = content
            logger.warning(f"Prefixo data:image não encontrado. Usando mimetype padrão: {mimetype}")

        try:
            base64.b64decode(base64_data, validate=True)
            logger.info(f"Tamanho da imagem decodificada: {len(base64.b64decode(base64_data))} bytes")
        except Exception as e:
            logger.error(f"Erro ao decodificar imagem: {e}, Base64 inicial: {base64_data[:50]}")
            return json.dumps({"is_medical_document": False, "details": f"Erro ao decodificar imagem: {str(e)}"})

        image_data_url = f"data:{mimetype};base64,{base64_data}"

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": """
                            Analise a imagem fornecida e determine se é um documento médico, como uma prescrição, laudo ou exame. 
                            Considere que o documento pode ser digitado ou manuscrito, com ou sem cabeçalho/logotipo, e pode incluir texto em português do Brasil.
                            Se for um documento médico, extraia informações relevantes, como:
                            - Nome do paciente (se visível, caso contrário, retorne 'Não identificado').
                            - Nome do médico (se visível, com ou sem título como 'Dr.' ou 'Dra.', caso contrário, retorne 'Não identificado').
                            - Medicamentos ou instruções (ex.: nome do medicamento, dosagem, frequência, como 'Amoxicilina 500mg, 3x/dia'; se não visível, retorne ['Não identificado']).
                            - Data ou número do documento (se visível, formato DD/MM/AAAA ou outro; caso contrário, retorne 'Não identificado').
                            Retorne um JSON no formato:
                            {
                                "is_medical_document": true,
                                "patient_name": "Nome do paciente ou 'Não identificado'",
                                "doctor_name": "Nome do médico ou 'Não identificado'",
                                "medications": ["medicamento: dosagem, frequência" ou "Não identificado"],
                                "document_date": "Data ou 'Não identificado'",
                                "details": "Outros detalhes relevantes, como tipo de documento (ex.: prescrição, laudo)"
                            }
                            Se não for um documento médico, retorne:
                            {
                                "is_medical_document": false,
                                "details": "Imagem não é um documento médico. Por favor, envie uma prescrição, laudo ou exame válido."
                            }
                            """
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": image_data_url}
                        }
                    ]
                }
            ],
            temperature=0.4  # Increased for better flexibility
        )
        description = response.choices[0].message.content.strip()
        logger.debug(f"Raw image analysis response: {description}")
        try:
            return json.dumps(json.loads(description), ensure_ascii=False)
        except json.JSONDecodeError:
            logger.error(f"Resposta da análise de imagem não é um JSON válido: {description}")
            return json.dumps({"is_medical_document": False, "details": f"Erro ao processar imagem: resposta inválida"})
    except Exception as e:
        logger.error(f"Erro ao processar imagem: {str(e)}")
        return json.dumps({"is_medical_document": False, "details": f"Erro ao processar imagem: {str(e)}"})