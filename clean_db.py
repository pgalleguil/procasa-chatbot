from chatbot.storage import get_db
import re

def clean_quotes():
    db = get_db()
    col = db['universo_obelix']
    # Debug: Check a few documents
    for doc in col.find({"codigo": {"$regex": "64342"}}):
        print(f"DEBUG SPECIFIC: {repr(doc.get('codigo'))}")
        print(f"DEBUG REPR OF REPR: {repr(repr(doc.get('codigo')))}")

    # Find all documents and check for quotes in Python
    docs = list(col.find())
    print(f"Checking {len(docs)} documents...")
    
    count = 0
    for doc in docs:
        old_code = str(doc.get('codigo', ''))
        if "'" in old_code:
            new_code = old_code.replace("'", "").strip()
            col.update_one({'_id': doc['_id']}, {'$set': {'codigo': new_code}})
            count += 1
        
    print(f"Successfully cleaned {count} documents.")

if __name__ == "__main__":
    clean_quotes()
