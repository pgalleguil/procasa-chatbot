import sys
import pandas as pd
import numpy as np

def main():
    excel_path = r'c:\Users\pgall\Desktop\Python\ChatBot_v4_Grok\prop_sRfxpyar9W.xls'
    try:
        # Load without headers
        dfs = pd.read_html(excel_path, header=None)
        df = dfs[0]
        
        # Find the row that contains 'Región' or 'codigo'
        header_idx = -1
        for idx, row in df.iterrows():
            row_str = " ".join([str(x).lower() for x in row.values])
            if 'regi' in row_str and 'ódigo' in row_str:
                header_idx = idx
                break
                
        if header_idx != -1:
            # set the new header
            df.columns = df.iloc[header_idx]
            df = df[header_idx+1:]
            df = df.reset_index(drop=True)
            
            print("Successfully found headers:", df.columns.tolist()[:10])
            
            # Let's inspect Arica properties
            reg_col = next((c for c in df.columns if 'regi' in str(c).lower()), None)
            cod_col = next((c for c in df.columns if 'códig' in str(c).lower() or 'codigo' in str(c).lower()), None)
            
            if reg_col and cod_col:
                # Find the ones that have "Región Bío-Bío"
                bio_df = df[df[reg_col].astype(str).str.contains("bio", case=False, na=False)]
                nuble_df = df[df[reg_col].astype(str).str.contains("uble", case=False, na=False)]
                
                print(f"Bio Bio props in Excel: {len(bio_df)}")
                print(f"Nuble props in Excel: {len(nuble_df)}")
                
                arica_df = df[df[reg_col].astype(str).str.contains("Arica", case=False, na=False)]
                print(f"Arica props in Excel: {len(arica_df)}")
                
                print("Total properties:", len(df))
                
                # The user says there are 26 confirmed properties.
                # Let's see BioBio properties
                if len(bio_df) > 0:
                    print("Biobio properties codes:", bio_df[cod_col].tolist())
    except Exception as e:
        print("Error:", e)

if __name__ == '__main__':
    main()
