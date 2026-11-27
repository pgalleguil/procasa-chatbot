@echo off
setlocal
set "REPO=%~dp0.."
set "HF_HOME=C:\Users\pgall\Desktop\Python\procasa-search-local-results-20261126\benchmark-20261126-run03\hf-cache"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
set "TOKENIZERS_PARALLELISM=false"
set "PYTHONW=C:\Users\pgall\Desktop\Python\.venvs\procasa-search-validation-20261126\Scripts\pythonw.exe"
set "CATALOG=C:\Users\pgall\Desktop\Python\procasa-search-local-results-20261126\catalog_desktop.json"
set "CACHE=C:\Users\pgall\Desktop\Python\procasa-search-local-results-20261126\benchmark-20261126-variants\bge_m3_reranker\cache\bge_m3_reranker-cuda.sqlite"
pushd "%REPO%"
start "" "%PYTHONW%" "%REPO%\scripts\local_property_search_app.py" --catalog "%CATALOG%" --cache "%CACHE%" --revision 5617a9f61b028005a4858fdac845db406aefb181 --reranker-revision 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41 --device cuda --batch-size 16 --reranker-batch-size 4 --rerank-pool 50
popd
endlocal
