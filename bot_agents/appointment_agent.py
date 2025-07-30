# bot_agents/appointment_agent.py
import asyncio
from datetime import datetime, timedelta
import pytz
from supabase import create_client, Client
from config.config import SUPABASE_URL, SUPABASE_KEY
from tools.whatsapp_tools import send_whatsapp_message
from utils.logging_setup import setup_logging

logger = setup_logging()

async def check_and_send_reminders():
    """
    Check for upcoming appointments and send reminders via WhatsApp at 8 AM.
    """
    client = create_client(SUPABASE_URL, SUPABASE_KEY)
    try:
        # Get current time in Brazil (UTC-3)
        br_tz = pytz.timezone('America/Sao_Paulo')
        now = datetime.now(br_tz)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_end = tomorrow + timedelta(days=1)

        # Query clients with appointments tomorrow and payment_status = 'pago'
        response = await client.table("clients").select("*").eq("payment_status", "pago").gte("appointment_datetime", tomorrow.isoformat()).lt("appointment_datetime", tomorrow_end.isoformat()).execute()

        if not response.data:
            logger.info("No appointments found for tomorrow with payment_status='pago'.")
            return

        for lead in response.data:
            remotejid = lead.get("remotejid")
            appointment_datetime = datetime.fromisoformat(lead.get("appointment_datetime"))
            medico = lead.get("medico", "Médico")
            consulta_type = lead.get("consulta_type", "consulta")
            clinic_id = lead.get("clinic_id")
            phone_number = lead.get("phone_number") or remotejid.replace("@s.whatsapp.net", "")

            if not remotejid or not clinic_id:
                logger.warning(f"Skipping reminder for lead {remotejid}: missing remotejid or clinic_id")
                continue

            # Set clinic_id for RLS
            await client.rpc("set_current_clinic_id", {"clinic_id": clinic_id}).execute()

            # Format reminder message
            appointment_time = appointment_datetime.strftime("%H:%M")
            message = (
                f"Olá! Lembrete da sua {consulta_type} com {medico} amanhã, {appointment_datetime.strftime('%d/%m/%Y')} às {appointment_time}. "
                "Chegue com 10 minutos de antecedência. Caso precise cancelar ou remarcar, entre em contato: wa.me/5537987654321."
            )

            # Send WhatsApp message
            success = await send_whatsapp_message(phone_number, message, remotejid=remotejid)
            if success:
                logger.info(f"Reminder sent to {remotejid} for appointment with {medico} at {appointment_datetime}")
            else:
                logger.error(f"Failed to send reminder to {remotejid} for appointment with {medico}")

    except Exception as e:
        logger.error(f"Error in check_and_send_reminders: {str(e)}")
    finally:
        await client.auth.sign_out()

async def start_appointment_reminder():
    """
    Start the appointment reminder task, running daily at 8 AM.
    """
    logger.info("Starting appointment reminder task")
    br_tz = pytz.timezone('America/Sao_Paulo')

    while True:
        try:
            now = datetime.now(br_tz)
            # Calculate time until next 8 AM
            next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if now.hour >= 8:
                next_run += timedelta(days=1)
            seconds_until_next_run = (next_run - now).total_seconds()

            logger.debug(f"Next reminder check scheduled for {next_run}")
            await asyncio.sleep(seconds_until_next_run)
            await check_and_send_reminders()
        except Exception as e:
            logger.error(f"Error in appointment reminder loop: {str(e)}")
            await asyncio.sleep(60)  # Wait 1 minute before retrying on error