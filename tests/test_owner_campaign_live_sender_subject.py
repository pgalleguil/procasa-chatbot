import pytest

from campanas import owner_campaign_live_sender as sender


@pytest.mark.parametrize("property_code", ["16469", "16527"])
def test_live_message_subject_keeps_base_and_appends_matching_property_code(property_code):
    row = {
        "property_code": property_code,
        "owner_email": "owner@example.com",
        "final_cc_list": ["boss@example.com", "advisor@example.com"],
        "html": "<html><body>Campaign message</body></html>",
    }

    message, _ = sender._build_message(row)

    assert message["Subject"] == (
        f"{sender.EMAIL_SUBJECT_BASE} · PROCASA SUCRE · {property_code}"
    )
    assert sender._subject_matches_property(message["Subject"], property_code)
    other_code = "16527" if property_code == "16469" else "16469"
    assert not sender._subject_matches_property(message["Subject"], other_code)


def test_subject_composition_rejects_missing_or_multiline_property_code():
    with pytest.raises(sender.SenderError, match="subject_identity_invalid"):
        sender._compose_subject(sender.EMAIL_SUBJECT_BASE, "PROCASA SUCRE", "")

    with pytest.raises(sender.SenderError, match="subject_identity_invalid"):
        sender._compose_subject(sender.EMAIL_SUBJECT_BASE, "PROCASA SUCRE", "16469\n16527")
