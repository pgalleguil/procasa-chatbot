"""Explicit contracts for independent messaging domains."""

CHATBOT = "chatbot"
DOCUMENT_SIGNATURE = "document_signature"
COMMERCIAL_NOTIFICATION = "commercial_notification"
SLA_ALERT = "sla_alert"


def require_domain(document, expected_domain):
    actual = str((document or {}).get("message_domain") or "")
    if actual != expected_domain:
        raise ValueError(f"wrong_message_domain:{actual or 'missing'}")
    return document


def chatbot_key(inbound_provider_message_id, batch_id):
    return f"chatbot:{inbound_provider_message_id}:{batch_id}"


def document_key(document_id, version, recipient):
    return f"document_signature:{document_id}:{version}:{recipient}"


def commercial_key(assignment_cycle_id, notification_type, recipient_user_id):
    return f"commercial_notification:{assignment_cycle_id}:{notification_type}:{recipient_user_id}"


def sla_key(assignment_cycle_id, alert_type, threshold, recipient_user_id):
    return f"sla_alert:{assignment_cycle_id}:{alert_type}:{threshold}:{recipient_user_id}"
