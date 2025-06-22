import threading
import logging
from django.apps import AppConfig

class ContractsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'contracts'

    def ready(self):
        # Get logger for this module
        logger = logging.getLogger(__name__)

        # Import inside ready to avoid circular imports
        try:
            from .views import start_contract_consumer_thread
        except ImportError as e:
            logger.error(f"Failed to import RabbitMQ consumer: {e}", exc_info=True)
            return

        def run_consumer():
            try:
                logger.info("Starting RabbitMQ consumer thread...")
                start_contract_consumer_thread()
            except Exception as e:
                logger.error(f"Failed to start RabbitMQ consumer thread: {e}", exc_info=True)

        # Start the consumer in a daemon thread
        consumer_thread = threading.Thread(target=run_consumer, daemon=True)
        consumer_thread.start()
