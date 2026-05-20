import csv
from pathlib import Path

csv_path = Path(r"C:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\exports\preview_campana_baja_precio_ola1_20260520.csv")

exclusions = []
with csv_path.open(encoding="utf-8-sig") as f:
    reader = csv.DictReader(f, delimiter=";")
    for row in reader:
        if row.get("ruta") == "Cola Manual":
            exclusions.append({
                "codigo": row.get("codigo"),
                "email": row.get("email"),
                "ejecutivo": row.get("ejecutivo"),
                "categoria_outlier": row.get("categoria_outlier"),
                "precio_publicado": row.get("precio_publicado"),
                "brecha_pct": row.get("brecha_pct")
            })

print(f"Total Excluidos: {len(exclusions)}")
print("CODIGO | EMAIL | EJECUTIVO | MOTIVO EXCLUSION | PRECIO | BRECHA")
print("-" * 90)
for idx, exc in enumerate(exclusions, 1):
    print(f"{idx}. {exc['codigo']} | {exc['email']} | {exc['ejecutivo']} | {exc['categoria_outlier']} | {exc['precio_publicado']} UF | {exc['brecha_pct']}")
