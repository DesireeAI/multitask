# models/lead_data.py
from pydantic import BaseModel, Field
from typing import Optional

class LeadData(BaseModel):
    remotejid: str = Field(..., description="WhatsApp user ID (e.g., '558496248451@s.whatsapp.net')")
    nome_cliente: Optional[str] = Field(None, description="Nome completo do cliente")
    pushname: Optional[str] = Field(None, description="PushName do WhatsApp")
    telefone: Optional[str] = Field(None, description="Número de telefone ou WhatsApp")
    idioma: Optional[str] = Field(None, description="Idioma do cliente")
    cidade: Optional[str] = Field(None, description="Cidade do cliente")
    estado: Optional[str] = Field(None, description="Estado do cliente")
    email: Optional[str] = Field(None, description="Endereço de e-mail do cliente")
    data_nascimento: Optional[str] = Field(None, description="Data de nascimento do cliente (YYYY-MM-DD)")
    thread_id: Optional[str] = Field(None, description="Thread ID for conversation tracking")
    data_cadastro: Optional[str] = Field(None, description="Data de cadastro do lead")
    data_ultima_alteracao: Optional[str] = Field(None, description="Data da última alteração")
    followup: Optional[bool] = Field(None, description="Indicador de follow-up")
    followup_data: Optional[str] = Field(None, description="Data de follow-up")
    ult_contato: Optional[str] = Field(None, description="Data do último contato")
    cep: Optional[str] = Field(None, description="CEP do cliente")
    endereco: Optional[str] = Field(None, description="Endereço do cliente")
    lead: Optional[int] = Field(None, description="ID do lead")
    verificador: Optional[int] = Field(None, description="Verificador do lead")
    klingo_client_id: Optional[str] = Field(None, description="ID do cliente na Klingo")
    klingo_access_key: Optional[str] = Field(None, description="Chave de acesso do cliente na Klingo")
    asaas_customer_id: Optional[str] = Field(None, description="ID do cliente na Asaas")
    payment_status: Optional[str] = Field(None, description="Status do pagamento (pendente, pago, cancelado)")
    consulta_type: Optional[str] = Field(None, description="Tipo de consulta (ex.: otorrino, fonoaudiologia)")
    medico: Optional[str] = Field(None, description="Nome do médico para a consulta")
    cpf_cnpj: Optional[str] = Field(None, description="CPF ou CNPJ do cliente")
    sintomas: Optional[str] = Field(None, description="Sintomas mencionados pelo paciente (ex.: dor de ouvido, zumbido)")
    clinic_id: Optional[str] = Field(None, description="ID da clínica")
    appointment_id: Optional[str] = None