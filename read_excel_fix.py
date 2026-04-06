import sys
import pandas as pd

def main():
    excel_path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\prop_sRfxpyar9W.xls'
    try:
        # Sometimes these "xls" from procasa are HTML, sometimes true excel
        df = pd.read_excel(excel_path)
    except Exception as e:
        print("Could not read as excel:", e)
        # fallback for html
        try:
            dfs = pd.read_html(excel_path)
            df = dfs[0]
            print("Read as HTML table")
        except Exception as e2:
            print("Could not read as HTML either:", e2)
            return

    print("Columns:", df.columns.tolist())
    print("Shape:", df.shape)
    
    # Check what regions we have in this excel
    # There's probably a "Región" column and "Código" column
    region_cols = [c for c in df.columns if 'regi' in c.lower()]
    cod_cols = [c for c in df.columns if 'odigo' in c.lower() or 'cód' in c.lower() or 'id' in c.lower()]
    print("Possible region cols:", region_cols)
    print("Possible code cols:", cod_cols)
    
    # print some counts of regions
    if region_cols:
        print("Region counts:\n", df[region_cols[0]].value_counts())

if __name__ == '__main__':
    main()
