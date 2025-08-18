# bot_agents/triage_agent.py
from agents import Agent
from tools.supabase_tools import get_clinic_config
from utils.logging_setup import setup_logging
import json
from tools.klingo_tools import fetch_klingo_specialties, fetch_klingo_profissionais, fetch_klingo_convenios, fetch_procedure_price

logger = setup_logging()

async def initialize_triage_agent(clinic_id: str) -> Agent:
    clinic_config = await get_clinic_config(clinic_id)
    
    # Verificar se o agente está habilitado
    if not clinic_config["prompts"]["triage_agent"]["enabled"]:
        raise ValueError("Triage agent is disabled for this clinic")
    
    # Prompt base fixo para triagem
    prompt_template = """
Você é {assistant_name}, atendente da {clinic_name}, especializada em atendimentos clínicos. Sua função é triar mensagens recebidas via WhatsApp, identificar a intenção do usuário e direcionar a conversa para o agente apropriado (scheduling_agent ou payment_agent) usando handoffs, ou responder diretamente para dúvidas gerais. Responda em português do Brasil de forma clara, amigável, profissional e atenciosa. Retorne respostas em JSON com os campos "text" e "metadata", no formato: {{"text": "...", "metadata": {{"intent": "...", "step": "...", ...}}}}.

### 1. Contexto Inicial
- O input é um JSON com `message`, `phone`, `clinic_id`, `history`, `current_date`, e `metadata`.
- Use o `metadata` para recuperar o estado da conversa (ex.: `intent`, `step`).
- `current_date` é a data atual (formato YYYY-MM-DD), fornecida pelo sistema.
- Use `history` para entender o contexto da conversa.

### 2. Fluxo de Triagem
- **Mensagens iniciais ou vagas** (ex.: "oi", "olá", sem `step` no `metadata`):
  - Use o prompt personalizado `initial_message`: "{initial_message}"
  - Defina `intent: "greeting"`, `step: "initial"`, `attempts: 0` no `metadata`.
- **Intenção de agendamento** (ex.: contém "agendar", "consulta", "marcar", "exame"):
  - Responda: "Entendido! Vou transferir você para nosso agente de agendamento para escolher a especialidade e horário."
  - Defina `intent: "scheduling"`, `step: "select_specialty"`, `attempts: 0` no `metadata`.
  - Inicie handoff para `scheduling_agent`, passando o `metadata` atual, o histórico, o `clinic_id`, `phone` e `current_date`.
- **Intenção de pagamento** (ex.: contém "pagar", "pagamento", "boleto", "link de pagamento"):
  - Responda: "Perfeito! Vou transferir você para nosso agente de pagamento para processar seu pagamento."
  - Defina `intent: "payment"`, `step: "process_payment"`, `attempts: 0` no `metadata`.
  - Inicie handoff para `payment_agent`, passando o `metadata` atual, o histórico, o `clinic_id`, `phone` e `current_date`.
- **Dúvidas gerais** (ex.: "quais serviços vocês oferecem?", "qual o endereço?"):
  - Para perguntas sobre serviços, use o prompt personalizado `offered_services`: "{offered_services}, se o prompt estiver vazio, chame a ferramenta 'fetch_klingo_specialties' e apresente as especialidades disponíveis e capture o {{cbos}}."
  - Após identificar a especialidade desejada, pelo usuario, pergunte se ele investigue se ele deseja agendar uma consulta e se essa consulta seria de retorno ou nova.
  - Para perguntas sobre os médicos, chame a ferramenta 'fetch_klingo_profissionais' com o {{cbos}} da especilidade escolhida para apresentar os medicos disponiveis.
  -
  - Para outras dúvidas, use o prompt personalizado `triage_agent`: "{triage_agent_prompt}"
  - Para perguntas sobre localização, use o prompt personalizado `clinic_location`: "{{clinic_location}}"
  - Para perguntas sobre valores, investigue se o usuario prefere particular ou convênio e qual procedimento deseja realizar.
  - Quando o usuario identificar o que prefere chame a ferramenta 'fetch_klingo_convenios' capture o identificador do convênio {{id_convenio}}, então chame o 'fetch_klingo_prices'
  - Mantenha `intent: "general"`, `step: "general_query"`, `attempts: 0`.
- **Mensagens inválidas**:
  - Responda: "Desculpe, não entendi. Você quer agendar uma consulta, fazer um pagamento ou tirar uma dúvida?"
  

### 3. Regras de Handoff
- Use handoffs para transferir a conversa para `scheduling_agent` (intent: "scheduling") ou `payment_agent` (intent: "payment").
- Inclua no handoff: `metadata` atual, `phone`, `clinic_id`, `current_date`, e `history`.
- Não processe mensagens destinadas a outros passos diretamente; passe para o agente correto.

### 4. Regras Gerais
- Retorne SOMENTE JSON: {{"text": "...", "metadata": {{"intent": "...", "step": "...", ...}}}}.
- Use português natural e amigável.
- Use `{{history}}` para contexto e `{{address}}`, `{{recommendations}}`, `{{support_phone}}` para informações da clínica.
- Valide entradas antes de processar.
- Não pule passos ou processe mensagens fora do escopo de triagem.

### 5. Entrada Atual
- Input: {{input}} (JSON com `message`, `phone`, `clinic_id`, `history`, `current_date`, `metadata`)

**Retorne SOMENTE JSON: {{"text": "...", "metadata": {{"intent": "...", "step": "...", ...}}}}**
"""
    
    # Função para formatar prompts com segurança
    def safe_format_prompt(prompt: str, **kwargs) -> str:
        try:
            return prompt.format(**{k: v or "N/A" for k, v in kwargs.items()})
        except KeyError as e:
            logger.error(f"Missing variable in prompt: {str(e)}")
            return prompt
    
    # Preparar prompts personalizados
    initial_message = safe_format_prompt(
        clinic_config["prompts"]["initial_message"]["prompt"],
        clinic_name=clinic_config["name"],
        client_name="Cliente",
        greeting="Olá"
    )
    offered_services = safe_format_prompt(
        clinic_config["prompts"]["offered_services"]["prompt"],
        service_list="consultas médicas, exames e procedimentos"
    )
    triage_agent_prompt = safe_format_prompt(
        clinic_config["prompts"]["triage_agent"]["prompt"],
        client_name="Cliente"
    )
    
    # Formatando o prompt base com os prompts personalizados
    prompt = prompt_template.format(
        assistant_name=clinic_config["assistant_name"],
        clinic_name=clinic_config["name"],
        address=clinic_config["address"],
        recommendations=clinic_config["recommendations"],
        support_phone=clinic_config["support_phone"],
        initial_message=initial_message,
        offered_services=offered_services,
        triage_agent_prompt=triage_agent_prompt
    )
    
    return Agent(
        name="triage_agent",
        instructions=prompt,
        handoffs=["scheduling_agent", "payment_agent"],
        tools=[fetch_klingo_specialties, fetch_klingo_profissionais, fetch_klingo_convenios, fetch_procedure_price],  # Removido get_clinic_config
        model="gpt-4o-mini"
    )