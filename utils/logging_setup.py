# utils/logging_setup.py
import logging

def setup_logging():
    # Configurar o logger principal
    logging.basicConfig(
        level=logging.INFO,  # Alterado para INFO para reduzir verbosidade
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Reduzir verbosidade de bibliotecas externas
    logging.getLogger('hpack').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('openai').setLevel(logging.WARNING)
    
    return logging.getLogger(__name__)