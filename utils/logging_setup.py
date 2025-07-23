# utils/logging_setup.py
import logging

def setup_logging():
    logging.basicConfig(
        level=logging.DEBUG,  # Changed from INFO to DEBUG
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    return logging.getLogger(__name__)