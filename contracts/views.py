import json
import logging
import base64
import os
import requests
from datetime import datetime
from base64 import b64encode
from django.utils.timezone import now
from django.conf import settings
from django.template import engines
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework import status, viewsets
from docusign_esign import (ApiClient, EnvelopesApi, EnvelopeDefinition, 
                            Document, Signer, SignHere, Tabs, Recipients, 
                            EventNotification)
from xhtml2pdf import pisa
from io import BytesIO

from .gemini_helper import GeminiHelper
from .models import Contract
from .serialiazars import ContractSerializer
from .utils import generate_keys, sign_message

# ----------- Logging Setup -----------
logger = logging.getLogger("contract_service")
logger.setLevel(logging.DEBUG)
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('[%(asctime)s] %(levelname)s - %(message)s'))
logger.addHandler(console_handler)


# ----------- Helper Functions -----------
def parse_date(date_str):
    """Parse date string from various formats to YYYY-MM-DD format."""
    if not date_str:
        return None
    try:
        if 'T' in date_str:
            return date_str.split('T')[0]
        return date_str
    except Exception:
        return None


def get_contract_or_404(owner_name: str, client_name: str) -> Contract:
    """Retrieve contract or raise error if not found."""
    try:
        return Contract.objects.get(owner_name=owner_name, client_name=client_name)
    except Contract.DoesNotExist:
        raise ValueError("Contract not found")


def save_signature_image(owner_name: str, signature_image_data: str) -> str:
    """Save base64 encoded signature image to file system."""
    try:
        header, encoded = signature_image_data.split(",", 1)
        image_data = base64.b64decode(encoded)
    except (ValueError, IndexError):
        raise ValueError("Invalid base64 image data")

    image_name = f"{owner_name.replace(' ', '_')}_{now().strftime('%Y%m%d%H%M%S')}.png"
    image_dir = os.path.join(settings.MEDIA_ROOT, "signatures")
    os.makedirs(image_dir, exist_ok=True)

    image_path = os.path.join(image_dir, image_name)
    with open(image_path, "wb") as f:
        f.write(image_data)

    return os.path.join('signatures', image_name)


def html_to_pdf_from_text(text: str) -> bytes:
    """Convert HTML text to PDF bytes."""
    html_engine = engines['django']
    template = html_engine.from_string(text)
    rendered_html = template.render({})  # Optionally pass context

    pdf_file = BytesIO()
    pisa_status = pisa.CreatePDF(rendered_html, dest=pdf_file)
    if pisa_status.err:
        raise Exception("PDF generation failed")
    return pdf_file.getvalue()


# ----------- External API Fetch Helpers -----------
def fetch_profile(user):
    """Fetch user profile from external service."""
    try:
        url = f"http://host.docker.internal:8008/profile/profil/?user={user}"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch profile for user {user}: {e}")
        return None


def fetch_request(request):
    """Fetch rental request details from external service."""
    try:
        url = f"http://host.docker.internal:8015/rental/rental_requests/{request}/"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch request {request}: {e}")
        return None


def fetch_equipment(equipment_id):
    """Fetch equipment details from external service."""
    try:
        url = f"http://host.docker.internal:8006/api/stuffs/{equipment_id}/"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch equipment ID {equipment_id}: {e}")
        return None


# ----------- API Views -----------
@api_view(['POST'])
@parser_classes([JSONParser])
def generate_contract(request):
    """Generate a new contract using Gemini AI and initiate signing with DocuSign."""
    try:
        data = request.data
        logger.info(f"Received contract generation request: {data}")

        owner_id = data.get("rentalId")
        client_id = data.get("clientId")
        equipment_id = data.get("equipmentId")
        request_id = data.get("requestId")

        logger.info("Step 1: Fetching profiles...")
        profile_info_client = fetch_profile(client_id)
        logger.info(f"Client profile fetched: {profile_info_client}")

        profile_info_owner = fetch_profile(owner_id)
        logger.info(f"Owner profile fetched: {profile_info_owner}")

        logger.info("Step 2: Fetching equipment info...")
        equipment_info = (
            [fetch_equipment(eid) for eid in equipment_id]
            if isinstance(equipment_id, list)
            else fetch_equipment(equipment_id)
        )
        logger.info(f"Equipment info fetched: {equipment_info}")

        logger.info("Step 3: Fetching request info...")
        request_info = fetch_request(request_id)
        logger.info(f"Request info fetched: {request_info}")

        logger.info("Step 4: Creating contract draft with Gemini...")
        contract_data = {
            "owner_name": owner_id,
            "client_name": client_id,
            "equipment": equipment_id,
            "start_date": data.get("startDate"),
            "end_date": data.get("endDate"),
            "total_value": data.get("total_price"),
            "details": data.get("status", ""),
        }

        helper = GeminiHelper()
        contract = helper.create_draft_contract(
            contract_data,
            profile_info_client,
            profile_info_owner,
            equipment_info,
            request_info
        )
        contract_text = contract.contract_text

        signer_email = data.get("signer_email") or "kthirithamer2@gmail.com"
        signer_name = data.get("signer_name") or profile_info_client[0].get("first_name")
        return_url = data.get("return_url") or "http://localhost:5173/client/sign-status/"  # Fallback

        logger.info("Step 5: Sending contract for signing...")
        sign_result = sign_contract(
            contract,
            contract_text,
            signer_email,
            signer_name,
            owner_id,
            client_id
        )

        if "envelope_id" in sign_result:
            logger.info(f"Contract sent to DocuSign with envelope ID: {sign_result['envelope_id']}")

            signing_url_result = fetch_signing_url(
                envelope_id=sign_result["envelope_id"],
                signer_email=signer_email,
                signer_name=signer_name,
                return_url=return_url
            )

            if "signing_url" in signing_url_result:
                return Response({
                    "message": sign_result["message"],
                    "envelope_id": sign_result["envelope_id"],
                    "contract_id": sign_result["contract_id"],
                    "signing_url": signing_url_result["signing_url"]
                })

            else:
                return Response({
                    "error": "Contract created but failed to generate signing URL",
                    "details": signing_url_result.get("details")
                }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        else:
            return Response({
                "error": sign_result.get("error"),
                "details": sign_result.get("details")
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    except Exception as e:
        logger.error(f"Error in contract generation: {str(e)}", exc_info=True)
        return Response(
            {"error": "Internal server error", "details": str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )



def sign_contract(contract, contract_text, signer_email, signer_name, owner_name, client_name):
    """Send contract to DocuSign for signing using JWT authentication."""
    try:
        # Check required fields
        missing = [name for name, val in [
            ('contract', contract),
            ('contract_text', contract_text),
            ('signer_email', signer_email),
            ('signer_name', signer_name),
            ('owner_name', owner_name),
            ('client_name', client_name),
        ] if not val]
        if missing:
            logger.error(f"sign_contract missing required fields: {missing}")
            raise ValueError("Missing required fields")

        # Convert contract text to PDF bytes (you must have this function implemented)
        pdf_content = html_to_pdf_from_text(contract_text)
        base64_pdf = b64encode(pdf_content).decode()

        # Initialize DocuSign API client with JWT authentication
        api_client = ApiClient()
        api_client.host = settings.DOCUSIGN_BASE_PATH

        private_key = settings.DOCUSIGN_PRIVATE_KEY.encode('ascii')

        token_response = api_client.request_jwt_user_token(
            client_id=settings.DOCUSIGN_INTEGRATION_KEY,
            user_id=settings.DOCUSIGN_USER_ID,
            oauth_host_name="account-d.docusign.com",  # Use your appropriate OAuth host
            private_key_bytes=private_key,
            expires_in=3600
        )

        api_client.set_default_header("Authorization", f"Bearer {token_response.access_token}")
        account_id = settings.DOCUSIGN_ACCOUNT_ID

        document = Document(
            document_base64=base64_pdf,
            name="Rental Contract",
            file_extension="pdf",
            document_id="1"
        )

        signer = Signer(
        email=signer_email,
        name=signer_name,
        recipient_id="1",
        routing_order="1",
        client_user_id="1"  # any unique string
        )

        sign_here = SignHere(
            document_id="1",
            page_number="1",
            recipient_id="1",
            tab_label="SignHereTab",
            x_position=100,  # must be int
            y_position=150   # must be int
        )

        signer.tabs = Tabs(sign_here_tabs=[sign_here])
        recipients = Recipients(signers=[signer])

        event_notification = EventNotification(
            url=f"https://58b2-197-25-68-113.ngrok-free.app/contracts/docusign/webhook/",  # Your webhook URL
            logging_enabled=True,
            require_acknowledgment=True,
            envelope_events=[
                {"envelope_event_status_code": "completed"},
                {"envelope_event_status_code": "declined"},
                {"envelope_event_status_code": "sent"},
            ]
        )

        envelope_definition = EnvelopeDefinition(
            email_subject="Please Sign the Rental Contract",
            documents=[document],
            recipients=recipients,
            event_notification=event_notification,
            status="sent"
        )

        envelopes_api = EnvelopesApi(api_client)
        envelope_summary = envelopes_api.create_envelope(
            account_id=account_id,
            envelope_definition=envelope_definition
        )

        # Save envelope ID to your contract model
        contract.envelope_id = envelope_summary.envelope_id
        contract.status = "sent_for_signing"
        contract.save()

        logger.info(f"Contract sent to DocuSign. Envelope ID: {envelope_summary.envelope_id}")

        return {
            "message": "Contract sent to DocuSign for signature",
            "envelope_id": envelope_summary.envelope_id,
            "contract_id": contract.id
        }

    except Exception as e:
        logger.error(f"Error in sign_contract: {str(e)}", exc_info=True)
        return {
            "error": "Failed to send contract",
            "details": str(e)
        }


# ----------- ViewSets -----------
class ContractViewSet(viewsets.ModelViewSet):
    """API endpoint for viewing and editing contracts."""
    queryset = Contract.objects.all()
    serializer_class = ContractSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        return self.filter_queryset_by_params(queryset)

    def filter_queryset_by_params(self, queryset):
        owner_name = self.request.query_params.get('owner_name')
        client_name = self.request.query_params.get('client_name')

        if owner_name:
            queryset = queryset.filter(owner_name=owner_name)
        if client_name:
            queryset = queryset.filter(client_name=client_name)

        return queryset

@api_view(['POST'])
@parser_classes([JSONParser])
def docusign_webhook(request):
    """
    Handle DocuSign webhook notifications for envelope events.
    This endpoint receives notifications when envelope status changes.
    """
    try:
        logger.info("Received DocuSign webhook notification")
        
        # Verify the request is from DocuSign (optional but recommended)
        if not verify_docusign_request(request):
            logger.warning("Received webhook from unauthorized source")
            return Response({"error": "Unauthorized"}, status=status.HTTP_401_UNAUTHORIZED)

        # Parse the webhook notification
        data = request.data
        logger.debug(f"Webhook data: {json.dumps(data, indent=2)}")

        # Extract envelope information
        envelope_id = data.get('envelopeId')
        if not envelope_id:
            logger.error("No envelopeId in webhook data")
            return Response({"error": "Missing envelopeId"}, status=status.HTTP_400_BAD_REQUEST)

        # Get the status and time of the event
        status = data.get('status')
        event_time = data.get('statusChangedDateTime')
        
        if not status:
            logger.error("No status in webhook data")
            return Response({"error": "Missing status"}, status=status.HTTP_400_BAD_REQUEST)

        logger.info(f"Processing envelope {envelope_id} with status {status}")

        try:
            # Find the contract associated with this envelope
            contract = Contract.objects.get(envelope_id=envelope_id)
        except Contract.DoesNotExist:
            logger.error(f"No contract found for envelope {envelope_id}")
            return Response({"error": "Contract not found"}, status=status.HTTP_404_NOT_FOUND)

        # Update contract based on status
        if status.lower() == 'completed':
            # Envelope was signed by all parties
            contract.status = 'completed'
            contract.completed_at = event_time or now()
            contract.save()
            
            logger.info(f"Contract {contract.id} marked as completed")
            
            # You might want to trigger additional actions here:
            # - Notify parties
            # - Process next steps
            # - Update related systems
            
        elif status.lower() == 'declined':
            # Envelope was declined by a signer
            contract.status = 'declined'
            contract.declined_at = event_time or now()
            contract.save()
            
            logger.info(f"Contract {contract.id} was declined")
            
            # You might want to:
            # - Notify the sender
            # - Log the reason if available
            
        elif status.lower() == 'sent':
            # Envelope was sent to recipients
            contract.status = 'sent'
            contract.sent_at = event_time or now()
            contract.save()
            
            logger.info(f"Contract {contract.id} was sent to recipients")

        # Always return 200 to acknowledge receipt
        return Response({"message": "Webhook processed successfully"}, status=status.HTTP_200_OK)

    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}", exc_info=True)
        return Response(
            {"error": "Internal server error"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


def verify_docusign_request(request):
    """
    Verify that the webhook request is from DocuSign.
    This checks the X-DocuSign-Signature header if configured.
    """
    # In production, you should verify the request signature
    # For demo purposes, we'll skip verification
    return True
    
    # For production implementation, you would:
    # 1. Get your webhook secret from settings
    # 2. Compute HMAC signature of the payload
    # 3. Compare with X-DocuSign-Signature header
    # Example:
    """
    secret = settings.DOCUSIGN_WEBHOOK_SECRET
    if not secret:
        return False
        
    received_sig = request.headers.get('X-DocuSign-Signature', '')
    computed_sig = hmac.new(
        secret.encode('utf-8'),
        request.body,
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(received_sig, computed_sig)
    """
def fetch_signing_url(envelope_id, signer_email, signer_name, return_url):
    try:
        if not all([envelope_id, signer_email, signer_name, return_url]):
            raise ValueError("Missing required fields for signing URL")

        api_client = ApiClient()
        api_client.host = settings.DOCUSIGN_BASE_PATH

        # JWT Authentication
        private_key = settings.DOCUSIGN_PRIVATE_KEY.encode('ascii').decode('utf-8')
        token_response = api_client.request_jwt_user_token(
            client_id=settings.DOCUSIGN_INTEGRATION_KEY,
            user_id=settings.DOCUSIGN_USER_ID,
            oauth_host_name="account-d.docusign.com",
            private_key_bytes=private_key,
            expires_in=3600
        )
        api_client.set_default_header(
            "Authorization",
            f"Bearer {token_response.access_token}"
        )

        account_id = settings.DOCUSIGN_ACCOUNT_ID
        envelopes_api = EnvelopesApi(api_client)

        recipient_view_request = {
            "authenticationMethod": "none",
            "clientUserId": "1",
            "recipientId": "1",
            "returnUrl": return_url,
            "userName": signer_name,
            "email": signer_email
        }

        results = envelopes_api.create_recipient_view(
            account_id=account_id,
            envelope_id=envelope_id,
            recipient_view_request=recipient_view_request
        )

        return {"signing_url": results.url}

    except Exception as e:
        logger.error(f"Error getting signing URL: {str(e)}", exc_info=True)
        return {"error": "Failed to retrieve signing URL", "details": str(e)}
