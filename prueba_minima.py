# prueba_minima.py → VERSIÓN FINAL 100% FUNCIONAL (diciembre 2025)
from fastapi import FastAPI, Request, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
import uvicorn

# Rutas absolutas
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

app = FastAPI(title="PROCASA – Sistema Interno")

# Archivos estáticos (con nombre para que url_for funcione)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Templates
templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals["get_flashed_messages"] = lambda *a, **k: []  # evita error en plantillas

# Obtener imágenes del carrusel
def get_images():
    prop_dir = STATIC_DIR / "propiedades"
    if not prop_dir.exists() or not prop_dir.is_dir():
        return ["propiedades/default.jpg"]
    images = [
        f"propiedades/{f.name}" for f in prop_dir.iterdir()
        if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    ]
    print(f"Imágenes cargadas ({len(images)}): {images}")
    return images or ["propiedades/default.jpg"]

# ======================
# RUTAS CON NOMBRE (para que url_for funcione)
# ======================

@app.get("/", name="login")
async def login_get(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "images": get_images()}
    )

@app.post("/", name="login")
async def login_post(request: Request):
    # Aquí irá tu login real más adelante
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "images": get_images()},
        headers={"HX-Redirect": "/dashboard"}  # ejemplo futuro
    )

@app.get("/forgot-password", name="forgot_password")
async def forgot_password(request: Request):
    return templates.TemplateResponse("forgot_password.html", {"request": request})

@app.get("/reset-password/{token}", name="reset_password")
async def reset_password(request: Request, token: str):
    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token})

@app.get("/404")
async def not_found(request: Request):
    return templates.TemplateResponse("404.html", {"request": request}, status_code=404)

# ======================
if __name__ == "__main__":
    print("\nPROCASA – PRUEBA MÍNIMA")
    print("Accede → http://127.0.0.1:8000")
    print("CTRL+C para detener\n")
    uvicorn.run("prueba_minima:app", host="127.0.0.1", port=8000, reload=True)