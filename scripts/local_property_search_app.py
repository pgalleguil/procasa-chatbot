"""Desktop search for PROCASA. Local JSON catalog, BGE-M3 and SQLite only.

No server, CRM writes, conversation reads, scraping or message sending.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chatbot.local_property_search import LocalPropertyIndex, validate_profile
from chatbot.local_embedding_cache import EmbeddingCache, model_namespace
from scripts.run_local_property_search import LocalEmbeddings, LocalReranker

LABELS = {"bedrooms":"Dormitorios", "bathrooms":"Baños", "parking":"Estacionamientos",
          "storage":"Bodegas", "usable_surface_m2":"Superficie útil (m²)", "total_surface_m2":"Superficie total (m²)"}
REASONS = {"PRICE_UNKNOWN":"Precio ausente", "PRICE_UNVERIFIED":"Precio no validado",
           "LOCATION_REVIEW_REQUIRED":"Ubicación por verificar", "EXTERNAL_HUMAN_VERIFICATION":"Anuncio externo: autorización y vigencia pendientes",
           "AVAILABILITY_UNKNOWN":"Disponibilidad desconocida", "LAST_PRICE_VERIFIED_AT_STALE":"Precio sin verificación reciente",
           "LAST_AVAILABILITY_VERIFIED_AT_STALE":"Disponibilidad sin verificación reciente"}

def parse_amount(value):
    raw = str(value).strip()
    if not re.fullmatch(r"(?:[0-9]+|[0-9]{1,3}(?:\.[0-9]{3})+)", raw):
        raise ValueError("Ingresa presupuesto CLP entero, por ejemplo 110.000.000.")
    amount = int(raw.replace(".", ""))
    if amount <= 0:
        raise ValueError("El presupuesto debe ser positivo.")
    return amount

def manual_profile(operation, communes, kinds, budget, query, attributes, location=""):
    result = {"operation": operation, "comunas":[v.strip() for v in communes.split(",") if v.strip()],
              "property_types":[v.strip().upper() for v in kinds.split(",") if v.strip()],
              "max_price_clp":parse_amount(budget), "query":query.strip(),
              "minimum_attributes":{}}
    for key, val in attributes.items():
        raw = str(val).strip()
        if raw:
            if not re.fullmatch(r"[0-9]+(?:[.,][0-9]+)?", raw):
                raise ValueError("Valor inválido: " + LABELS[key])
            n = float(raw.replace(",", "."))
            if key in {"bedrooms","bathrooms","parking","storage"} and not n.is_integer():
                raise ValueError("Ingresa un número entero: " + LABELS[key])
            result["minimum_attributes"][key] = n
    if location.strip():
        result["location_targets"] = [location.strip()]
    validate_profile(result)
    return result

def safe_url(value):
    if not isinstance(value, str) or any(ord(c)<32 for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in {"http","https"} and parsed.hostname and not parsed.username and not parsed.password else ""
    except ValueError:
        return ""

def classify_result(item, verified):
    reasons = item.get("review_reasons") or []
    incomplete = any(r == "PRICE_UNKNOWN" or r == "LOCATION_REVIEW_REQUIRED" or r.startswith("UNKNOWN_") for r in reasons)
    return {"ajuste": "Datos obligatorios pendientes" if incomplete else "Cumple filtros registrados",
            "estado": "Verificación registrada" if verified else "Por verificar — no ofrecer aún",
            "motivos":[REASONS.get(r, "Falta " + LABELS.get(r[8:].lower(), r)) if r.startswith("UNKNOWN_") else REASONS.get(r,r) for r in reasons]}

def result_rows(result, catalog):
    rows = []
    for group in ("compatible", "review"):
        for rank, item in enumerate(result.get(group, []), 1):
            original = catalog[item["listing_key"]]
            rows.append({**item, **classify_result(item, group == "compatible"),
                         "rank":rank, "grupo":group, "title":original.get("title") or original.get("property_type") or "",
                         "property_type":original.get("property_type"), "operation":original.get("operation"),
                         "source_portal":original.get("source_portal"), "url":safe_url(item.get("url")),
                         "description":str(original.get("description") or "")[:4000]})
    return rows

class DesktopEngine:
    def __init__(self, args):
        self.args = args
        self.index = None
        self.rows = {}
        self.signature = None
        self.embedder = None
        self.reranker = None
        self.cache = None

    def reload(self):
        args = self.args
        raw = args.catalog.read_bytes()
        import hashlib
        signature = hashlib.sha256(raw).hexdigest()
        if signature == self.signature and self.index is not None:
            return len(self.rows), signature, True
        rows = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(rows, list) or not rows or any(not isinstance(x, dict) for x in rows):
            raise ValueError("El catálogo debe ser una lista JSON no vacía de fichas normalizadas.")
        if self.embedder is None:
            import sentence_transformers
            namespace = model_namespace(args.model, args.revision, version=sentence_transformers.__version__)
            # Validate pinned reranker too. No downloads without explicit flag.
            model_namespace(args.reranker, args.reranker_revision, version=sentence_transformers.__version__)
            self.cache = EmbeddingCache(args.cache, namespace)
            self.embedder = LocalEmbeddings(args.model,args.device,args.allow_model_download,args.revision)
            self.reranker = LocalReranker(args.reranker,args.device,args.allow_model_download,args.reranker_revision)
        if self.reranker is None:
            self.reranker = LocalReranker(args.reranker,args.device,args.allow_model_download,args.reranker_revision)
        index = LocalPropertyIndex(rows,self.embedder,cache=self.cache,reranker=self.reranker,rerank_pool=args.rerank_pool)
        self.index = index
        self.rows = {x["listing_key"]:x for x in rows}
        self.signature = signature
        return len(rows), signature, False

    def search_many(self, profiles, top, progress=None):
        if self.index is None:
            raise ValueError("Carga el catálogo primero.")
        if not isinstance(profiles,list) or not 1 <= len(profiles) <= 1000:
            raise ValueError("Se requiere una lista de 1 a 1000 perfiles manuales.")
        for p in profiles:
            validate_profile(p)
        result = []
        for i, p in enumerate(profiles,1):
            start = time.perf_counter()
            now = datetime.now(timezone.utc)
            r = self.index.search(p,limit=top,now=now)
            result.append({"profile_id":p.get("profile_id",i),"profile":p,"as_of":now.isoformat(),
                           "catalog_sha256":self.signature,"elapsed_ms":round((time.perf_counter()-start)*1000),
                           "compatible_count":r["compatible_count"],"review_count":r["review_count"],
                           "excluded":r["excluded"],"mode":r["mode"],"reranked":r["reranked"],
                           "results":result_rows(r,self.rows)})
            if progress:
                progress(i,len(profiles))
        return result

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--catalog",type=Path,required=True)
    p.add_argument("--cache",type=Path,required=True)
    p.add_argument("--model",default="BAAI/bge-m3")
    p.add_argument("--revision")
    p.add_argument("--reranker",default="BAAI/bge-reranker-v2-m3")
    p.add_argument("--reranker-revision")
    p.add_argument("--device",choices=["cpu","cuda"],default="cpu")
    p.add_argument("--allow-model-download",action="store_true")
    p.add_argument("--rerank-pool",type=int,default=20)
    args = p.parse_args()
    if not 1 <= args.rerank_pool <= 500 or args.catalog.resolve()==args.cache.resolve():
        p.error("Pool inválido o catálogo y caché coinciden.")
    for model, revision in [(args.model,args.revision),(args.reranker,args.reranker_revision)]:
        if not Path(model).is_dir() and not re.fullmatch(r"[0-9a-fA-F]{40}",revision or ""):
            p.error("Indica la revisión SHA fija del benchmark o una carpeta local del modelo.")
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import queue
    engine = DesktopEngine(args)
    executor = ThreadPoolExecutor(max_workers=1)
    progress_queue = queue.SimpleQueue()
    root = tk.Tk()
    root.title("PROCASA | Buscador inmobiliario local")
    root.geometry("1250x820")
    root.minsize(950,680)
    root.columnconfigure(0,weight=1)
    root.rowconfigure(3,weight=1)
    status = tk.StringVar(value="Iniciando buscador local...")
    summary = tk.StringVar(value="Resultados pendientes de búsqueda")
    busy = False
    ready = False
    reports = []
    shown = {}
    controls = []
    bar = ttk.Frame(root,padding=12)
    bar.grid(row=0,column=0,sticky="ew")
    ttk.Label(bar,text="PROCASA · Inteligencia inmobiliaria",font=("Segoe UI",18,"bold")).pack(anchor="w")
    ttk.Label(bar,textvariable=status,wraplength=1100).pack(anchor="w")
    filters=ttk.LabelFrame(root,text="Requisitos obligatorios",padding=10)
    filters.grid(row=1,column=0,sticky="ew",padx=12)
    for col in range(4):
        filters.columnconfigure(col,weight=1)
    vars={}
    def field(parent,key,label,row,col,initial="",choices=None):
        ttk.Label(parent,text=label).grid(row=row,column=col,sticky="w",padx=4)
        v=tk.StringVar(value=initial);vars[key]=v
        widget=ttk.Combobox(parent,textvariable=v,values=choices,state="readonly") if choices else ttk.Entry(parent,textvariable=v)
        widget.grid(row=row+1,column=col,sticky="ew",padx=4,pady=(0,7))
    field(filters,"operation","Operación",0,0,"VENTA",["VENTA","ARRIENDO"])
    field(filters,"communes","Comunas (separadas por coma)",0,1)
    field(filters,"kinds","Tipos (separados por coma)",0,2,"CASA")
    field(filters,"budget","Presupuesto máximo CLP",0,3)
    for i,(key,label) in enumerate(LABELS.items()):
        field(filters,key,label,2+(i//4)*2,i%4)
    field(filters,"location","Referencia geográfica (requiere verificar)",4,2)
    field(filters,"top","Resultados por grupo",4,3,"10",["5","10","20","50"])
    ttk.Label(filters,text="Preferencias semánticas (no son requisitos garantizados)").grid(row=6,column=0,columnspan=4,sticky="w",padx=4)
    query=tk.Text(filters,height=3,wrap="word")
    query.grid(row=7,column=0,columnspan=4,sticky="ew",padx=4)
    actions=ttk.Frame(root,padding=12);actions.grid(row=2,column=0,sticky="ew")
    table=ttk.Frame(root,padding=(12,0));table.grid(row=3,column=0,sticky="nsew")
    table.columnconfigure(0,weight=1);table.rowconfigure(1,weight=1)
    ttk.Label(table,textvariable=summary).grid(row=0,column=0,sticky="w")
    cols=("rank","title","price","commune","source","fit","state")
    tree=ttk.Treeview(table,columns=cols,show="headings",selectmode="browse")
    for key,label,width in zip(cols,("Orden","Propiedad","Precio CLP","Comuna","Fuente","Ajuste","Estado comercial"),(65,260,110,120,95,200,240)):
        tree.heading(key,text=label);tree.column(key,width=width,stretch=key in {"title","fit","state"})
    tree.grid(row=1,column=0,sticky="nsew")
    scroll=ttk.Scrollbar(table,orient="vertical",command=tree.yview);scroll.grid(row=1,column=1,sticky="ns");tree.configure(yscrollcommand=scroll.set)
    details=tk.Text(root,height=8,wrap="word",state="disabled")
    details.grid(row=4,column=0,sticky="ew",padx=12,pady=10)

    def set_detail(text):
        details.configure(state="normal");details.delete("1.0","end");details.insert("1.0",text);details.configure(state="disabled")
    def detail(_=None):
        selected=tree.selection()
        if not selected:return
        r=shown[selected[0]]
        attributes=" · ".join(f"{LABELS.get(k,k)}: {v}" for k,v in r["attributes"].items())
        set_detail(f"{r['title']}\n{r['listing_key']} | {r['ajuste']} | {r['estado']}\n{attributes}\nMotivos: {', '.join(r['motivos']) or 'Sin pendientes registrados'}\n{r['description']}\n{r['url']}")
    tree.bind("<<TreeviewSelect>>",detail)
    def open_link():
        selected=tree.selection()
        if selected and shown[selected[0]]["url"]:
            webbrowser.open(shown[selected[0]]["url"])
    def display(results):
        nonlocal reports
        reports=results;shown.clear();tree.delete(*tree.get_children());set_detail("")
        for report in results:
            for i,r in enumerate(report["results"]):
                key=str(len(shown));shown[key]=r
                price=r.get("price_clp")
                formatted=f"{price:,.0f}".replace(",",".") if isinstance(price,(int,float)) and price>0 else "Sin precio"
                tree.insert("","end",iid=key,values=(f"{report['profile_id']} / {r['rank']}",r["title"],formatted,r["commune"],r["source_portal"] or r["source_type"],r["ajuste"],r["estado"]))
        summary.set(f"{len(results)} búsquedas · {sum(x['compatible_count'] for x in results)} verificadas · {sum(x['review_count'] for x in results)} por revisar · {len(shown)} resultados mostrados (puede repetirse un aviso)")
    def export():
        if not reports:return
        path=filedialog.asksaveasfilename(defaultextension=".json",filetypes=[("JSON","*.json")])
        if not path:return
        target=Path(path)
        if target.resolve() in {args.catalog.resolve(),args.cache.resolve()}:
            messagebox.showerror("Archivo protegido","No sobrescribas catálogo ni caché.");return
        try:
            with target.open("x",encoding="utf-8") as f:
                json.dump(reports,f,ensure_ascii=False,indent=2,allow_nan=False)
            status.set("Resultados exportados a "+str(target))
        except FileExistsError:
            messagebox.showerror("Archivo existente","Elige un nombre nuevo.")
        except (OSError,ValueError) as e:
            messagebox.showerror("Exportación",str(e))
    def poll_progress():
        try:
            while True:
                i,n=progress_queue.get_nowait();status.set(f"Buscando perfil {i} de {n}")
        except queue.Empty:pass
        root.after(200,poll_progress)
    def update_controls():
        for b,requires_ready in controls:
            b.configure(state="disabled" if busy or (requires_ready and not ready) else "normal")
    def submit(fn,done,message):
        nonlocal busy
        if busy:return
        busy=True;status.set(message);update_controls()
        future=executor.submit(fn)
        def poll():
            nonlocal busy
            if not future.done():
                root.after(200,poll);return
            busy=False
            try:done(future.result())
            except Exception as e:
                status.set("Operación no completada. Revisa el catálogo y configuración local.")
                messagebox.showerror("Error de búsqueda",str(e))
            update_controls()
        root.after(200,poll)
    def load():
        nonlocal ready
        ready=False
        def done(value):
            nonlocal ready,reports
            count,signature,unchanged=value
            ready=True
            if not unchanged:
                reports=[];shown.clear();tree.delete(*tree.get_children());set_detail("");summary.set("Catálogo actualizado: realiza una nueva búsqueda.")
            status.set(f"{count:,} avisos | {args.model} + reranker | {args.device.upper()} | Snapshot {signature[:10]}")
        submit(engine.reload,done,"Cargando catálogo, modelos y caché incremental...")
    def perform(profiles):
        top=int(vars["top"].get())
        def done(results):
            display(results);status.set("Búsqueda completada. Confirma disponibilidad antes de entregar alternativas.")
        submit(lambda:engine.search_many(profiles,top,lambda i,n:progress_queue.put((i,n))),done,"Buscando propiedades...")
    def search():
        try:
            profile=manual_profile(vars["operation"].get(),vars["communes"].get(),vars["kinds"].get(),vars["budget"].get(),query.get("1.0","end"),{k:vars[k].get() for k in LABELS},vars["location"].get())
            perform([profile])
        except ValueError as e:messagebox.showerror("Revisa los filtros",str(e))
    def batch():
        path=filedialog.askopenfilename(filetypes=[("Perfiles JSON","*.json")])
        if not path:return
        try:
            profiles=json.loads(Path(path).read_text(encoding="utf-8-sig"))
            perform(profiles)
        except (OSError,ValueError) as e:messagebox.showerror("Perfiles inválidos",str(e))
    for label,action,req in [("Buscar",search,True),("Búsqueda masiva JSON",batch,True),("Actualizar catálogo local",load,False),("Abrir anuncio",open_link,True),("Exportar resultados",export,True)]:
        b=ttk.Button(actions,text=label,command=action);b.pack(side="left",padx=4);controls.append((b,req))
    def close():
        if busy:
            messagebox.showinfo("Proceso activo","Espera a que termine o interrumpe desde la consola. La caché conserva lotes completados.");return
        executor.shutdown(wait=False,cancel_futures=True);root.destroy()
    root.protocol("WM_DELETE_WINDOW",close)
    root.after(200,poll_progress)
    root.after(100,load)
    update_controls()
    root.mainloop()

if __name__=="__main__":
    main()
