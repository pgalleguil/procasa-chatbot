import sys
import pandas as pd
from pymongo import MongoClient
sys.path.append(r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok')
from config import Config

def main():
    excel_path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\prop_sRfxpyar9W.xls'
    df = pd.read_excel(excel_path, engine='xlrd', header=1)
    
    for idx, row in df.iterrows():
        row_str = " ".join([str(x).lower() for x in row.values])
        if 'regi' in row_str and ('odigo' in row_str or 'cód' in row_str):
            df.columns = df.iloc[idx]
            df = df[idx+1:]
            break
            
    cod_col = next((c for c in df.columns if 'odigo' in str(c).lower() or 'cód' in str(c).lower()), None)
    
    print("Muestra codigos en Excel:")
    samples = df[cod_col].dropna().head(10).tolist()
    print(samples)

if __name__ == '__main__':
    main()
