import sys
import pandas as pd

def main():
    excel_path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\prop_sRfxpyar9W.xls'
    try:
        dfs = pd.read_html(excel_path, header=0)
        df = dfs[0]
        # if column Unnamed is still there, maybe header is 1
        if 'Unnamed: 0' in df.columns:
            dfs = pd.read_html(excel_path, header=1)
            df = dfs[0]
    except Exception as e:
        df = pd.read_excel(excel_path, header=1)

    print("Columns:", len(df.columns), df.columns.tolist()[:10], "...")
    print("Shape:", df.shape)
    
    # Try to find codes and regions
    region_col = next((c for c in df.columns if 'regi' in str(c).lower()), None)
    cod_col = next((c for c in df.columns if 'odigo' in str(c).lower() or 'cód' in str(c).lower()), None)
    
    if region_col and cod_col:
        print(f"Using {cod_col} and {region_col}")
        # Find which properties in the Excel are marked as "Región Bío-Bío" or similar
        # or which ones match the 37 we found earlier.
        print("Region counts in Excel:\n", df[region_col].value_counts().head(10))
        
        # also search if "Arica" exists
        arica_df = df[df[region_col].str.contains("Arica", na=False, case=False)]
        print(f"Propiedades en Arica en Excel: {len(arica_df)}")
        nuble_df = df[df[region_col].str.contains("uble", na=False, case=False)]
        bio_df = df[df[region_col].str.contains("bio", na=False, case=False)]
        print(f"Propiedades en Nuble en Excel: {len(nuble_df)}")
        print(f"Propiedades en Biobio en Excel: {len(bio_df)}")
    else:
        print("Could not find columns cleanly.")

if __name__ == '__main__':
    main()
