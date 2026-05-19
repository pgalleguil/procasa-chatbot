
import os

def find_string(search_path, target):
    found = []
    for root, dirs, files in os.walk(search_path):
        for file in files:
            if file.endswith(('.py', '.html', '.js', '.json', '.env', '.sh', '.bat')):
                full_path = os.path.join(root, file)
                try:
                    with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                        if target in f.read():
                            found.append(full_path)
                except:
                    pass
    return found

target_str = "tasaciones_final"
results = find_string(".", target_str)
print(f"Found '{target_str}' in:")
for r in results:
    print(f" - {r}")
