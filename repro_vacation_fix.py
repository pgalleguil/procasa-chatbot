from chatbot.lead_router import find_responsible_executive

def test_fix():
    # Property 65476 is owned by Erika Garrido (who is on vacation)
    print("Testing routing for property 65476 (Owned by Erika Garrido)...")
    exec_name, phone = find_responsible_executive("65476")
    print(f"Result: {exec_name} ({phone})")
    
    # Expected: Raquel Cheneaux (replacement for Erika)
    assert exec_name == "Raquel Cheneaux", f"Expected Raquel Cheneaux, got {exec_name}"
    print("Verification SUCCESS: Lead correctly routed to replacement!")

if __name__ == "__main__":
    try:
        test_fix()
    except Exception as e:
        print(f"Verification FAILED: {e}")
        exit(1)
