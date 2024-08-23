
import imaplib
import email
import re
import requests
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

# Load configuration from file
with open('config_amazon.json', 'r') as config_file:
    config = json.load(config_file)

IMAP_SERVER = config['imap_server']
IMAP_USER = config['imap_user']
IMAP_PASSWORD = config['imap_password']
WEBHOOK_URL = config['webhook_url']
EMAIL_SENDERS = config['email_senders']
ORDER_THRESHOLD = config['order_threshold']
CHECK_INTERVAL_SECONDS = config['check_interval_seconds']
RECIPIENT_EXCLUDE_LIST = config.get('recipient_exclude_list', [])

# Initialize start_time with the current time when the script starts
last_check_start_time = datetime.now(timezone.utc)

# Set up logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler("email_check_amazon.log"),
                        logging.StreamHandler()
                    ])

# Separate handler for verbose logging to the file only
verbose_logger = logging.getLogger("verbose")
verbose_logger.propagate = False
if not verbose_logger.hasHandlers():
    verbose_handler = logging.FileHandler("email_verbose_amazon.log")
    verbose_handler.setLevel(logging.DEBUG)
    verbose_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    verbose_logger.addHandler(verbose_handler)
    verbose_logger.setLevel(logging.DEBUG)

def is_amazon_order_email(email_message):
    """Checks if the email is from an Amazon order."""
    sender = email.utils.parseaddr(email_message['From'])[1]
    subject = email_message['Subject']
    verbose_logger.debug(f"Checking sender: {sender} and subject: {subject}")

    # Check if the sender matches any of the configured senders
    sender_match = any(config_sender.lower() in sender.lower() for config_sender in EMAIL_SENDERS)
    subject_match = "order" in subject.lower()

    if sender_match and subject_match:
        verbose_logger.debug("Email matched Amazon order criteria.")
        return True
    
    verbose_logger.debug("Email did not match Amazon order criteria.")
    return False

def extract_order_total(email_message):
    """Extracts the order total from the email message."""
    order_total_pattern = re.compile(r'Order Total:\s*\$([\d,]+\.\d{2})')

    if email_message.is_multipart():
        for part in email_message.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))

            if "attachment" not in content_disposition and content_type in ["text/plain", "text/html"]:
                body = part.get_payload(decode=True).decode(part.get_content_charset(), errors='ignore')
                verbose_logger.debug(f"Email Body: {body}")

                match = order_total_pattern.search(body)
                if match:
                    try:
                        total_amount = float(match.group(1).replace(',', ''))
                        verbose_logger.debug(f"Extracted order total: \${total_amount}")
                        return total_amount
                    except (IndexError, ValueError) as e:
                        verbose_logger.error(f"Error parsing order total: {e}")
                        return None
    else:
        body = email_message.get_payload(decode=True).decode(email_message.get_content_charset(), errors='ignore')
        verbose_logger.debug(f"Email Body: {body}")

        match = order_total_pattern.search(body)
        if match:
            try:
                total_amount = float(match.group(1).replace(',', ''))
                verbose_logger.debug(f"Extracted order total: \${total_amount}")
                return total_amount
            except (IndexError, ValueError) as e:
                verbose_logger.error(f"Error parsing order total: {e}")
                return None

    verbose_logger.debug("Order total could not be extracted from the email.")
    return None

def extract_order_number(email_message):
    """Extracts the order number from the email message."""
    order_number_pattern = re.compile(r'Order #\s*(\d{3}-\d{7}-\d{7})')
    
    if email_message.is_multipart():
        for part in email_message.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))

            if "attachment" not in content_disposition and content_type in ["text/plain", "text/html"]:
                body = part.get_payload(decode=True).decode(part.get_content_charset(), errors='ignore')
                verbose_logger.debug(f"Email Body: {body}")

                match = order_number_pattern.search(body)
                if match:
                    verbose_logger.debug(f"Extracted order number: {match.group(1)}")
                    return match.group(1)
    else:
        body = email_message.get_payload(decode=True).decode(email_message.get_content_charset(), errors='ignore')
        verbose_logger.debug(f"Email Body: {body}")

        match = order_number_pattern.search(body)
        if match:
            verbose_logger.debug(f"Extracted order number: {match.group(1)}")
            return match.group(1)

    verbose_logger.debug("Order number could not be extracted from the email.")
    return None

def get_recipient_email(email_message):
    """Extracts the recipient's email from the 'Delivered-To' header or defaults to the IMAP user email."""
    recipient = email_message.get('Delivered-To', IMAP_USER)
    return recipient

def send_webhook_notification(email_message, order_total, order_number, recipient):
    """Sends a notification to the webhook URL with a nicely formatted message."""
    data = {
        "content": "**AMAZON ORDER ALERT**",
        "embeds": [
            {
                "title": "Order Details:",
                "description": (
                    f"**From:** {email_message['From']}\n"
                    f"**Recipient:** {recipient}\n"
                    f"**Subject:** {email_message['Subject']}\n"
                    f"**Order Number:** {order_number}\n"
                    f"**Order Total:** \${order_total:.2f}"
                ),
                "color": 16724736,  # Red color for the embed
                "footer": {
                    "text": "Amazon Order Notifier v1.0"
                }
            }
        ],
        "username": "Amazon Order Notifier",
        "avatar_url": "https://uxwing.com/wp-content/themes/uxwing/download/signs-and-symbols/exclamation-warning-round-red-icon.png",
        "attachments": []
    }

    response = requests.post(WEBHOOK_URL, json=data)
    if response.status_code != 204:
        logging.error(f"Failed to send webhook: {response.status_code}, {response.text}")
    else:
        logging.info(f"Webhook sent for email from {email_message['From']} with order number {order_number} and total: \${order_total:.2f}")

def check_email():
    """Checks for new emails, processes them, and sends notifications if conditions are met."""
    global last_check_start_time

    try:
        # Capture the start time of this check
        current_check_start_time = datetime.now(timezone.utc)
        logging.info("Connecting to IMAP server...")
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(IMAP_USER, IMAP_PASSWORD)

        status, _ = mail.select('inbox')
        if status != 'OK':
            logging.error(f"Failed to select inbox: {status}")
            return
        
        logging.info("Checking for new emails...")

        # Correcting the date format for the SEARCH command
        since_date = last_check_start_time.strftime("%d-%b-%Y")
        search_criterion = f'(SINCE {since_date})'
        
        result, data = mail.search(None, search_criterion)
        if result != 'OK':
            logging.error(f"Failed to search emails: {result}")
            return

        mail_ids = data[0].split()
        logging.info(f"Found {len(mail_ids)} emails to check.")
        emails_found = 0

        for mail_id in mail_ids:
            verbose_logger.debug(f"Fetching email ID: {mail_id}")
            result, message_data = mail.fetch(mail_id, '(RFC822)')
            if result != 'OK':
                verbose_logger.error(f"Failed to fetch email: {result}")
                continue

            for response_part in message_data:
                if isinstance(response_part, tuple):
                    email_message = email.message_from_bytes(response_part[1])
                    email_date = parsedate_to_datetime(email_message['Date'])

                    # Convert email_date to UTC
                    if email_date.tzinfo is None:
                        email_date = email_date.replace(tzinfo=timezone.utc)
                    else:
                        email_date = email_date.astimezone(timezone.utc)

                    # Add detailed logging
                    verbose_logger.debug(f"Email Date: {email_date}, Check Start Time: {last_check_start_time}")

                    if email_date > last_check_start_time:
                        if is_amazon_order_email(email_message):
                            order_total = extract_order_total(email_message)
                            order_number = extract_order_number(email_message)
                            recipient = get_recipient_email(email_message)
                            if recipient.lower() in [r.lower() for r in RECIPIENT_EXCLUDE_LIST]:
                                logging.info(f"Skipping notification for recipient: {recipient}")
                                continue
                            if order_total is None:
                                verbose_logger.debug("Order total could not be extracted. No notification sent.")
                            elif order_total > ORDER_THRESHOLD:
                                logging.info(f"Relevant email found: {email_message['Subject']}")
                                send_webhook_notification(email_message, order_total, order_number, recipient)
                                emails_found += 1
                            else:
                                verbose_logger.debug(f"Order total is below the threshold (${order_total}). No notification sent.")
                        else:
                            verbose_logger.debug("Email did not match Amazon order criteria.")
                    else:
                        verbose_logger.debug(f"Email arrived before the last check start time. No notification sent.")

        logging.info(f"Finished search. Processed {len(mail_ids)} emails, found {emails_found} relevant emails.")

        # Update last_check_start_time to the current check's start time
        last_check_start_time = current_check_start_time
        
    except imaplib.IMAP4.error as e:
        logging.error(f"IMAP error: {e}")
    
    except KeyboardInterrupt:
        logging.info("Script aborted by user.")
        return
    
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")

    finally:
        try:
            mail.logout()
            logging.info("Logged out from the IMAP server.")
        except imaplib.IMAP4.abort:
            logging.error("IMAP connection aborted.")
        except Exception as e:
            logging.error(f"Error during logout: {e}")

if __name__ == "__main__":
    while True:
        check_email()
        time.sleep(CHECK_INTERVAL_SECONDS)
