"""Channel boundary for office automation and campaign decision readers."""


def office_messages(messages):
    """Executive audit evidence is passive, never input to office automation.

    Legacy office messages without a channel remain supported. Explicit
    executive evidence stays in canonical storage for authorized audit views.
    """
    return [m for m in (messages or []) if isinstance(m, dict)
            and m.get('channel') != 'executive_whatsapp']
