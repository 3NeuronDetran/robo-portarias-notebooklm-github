import os
import json
import requests
from bs4 import BeautifulSoup
import io
from datetime import datetime
from urllib.parse import urljoin
from PyPDF2 import PdfMerger, PdfReader
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import gspread

# ================= CONFIGURAÇÕES GERAIS =================
URL_PLANILHA = 'https://docs.google.com/spreadsheets/d/1XNPM_2ToorZQauPg91mDhBBcwIXpnl-6Ta2ppyeTmQI/edit'
NOME_ABA_CONTROLE = 'controle'
NOME_ABA_LOG = 'Arquivos_baixados'
BASE_URL = "https://mtsp.detran.sc.gov.br"
TAMANHO_DO_GRUPO = 20
SIMULACAO = False

# ================= CARDÁPIO DE ANOS =================
CONFIGS_ANOS = [
    {
        "ano": "2026",
        "url": "https://mtsp.detran.sc.gov.br/portarias_web/portarias.php?ano=2026",
        "pasta_destino": "1QfABwYkKfxn-pvONmKGhOfUTF_VYyxbQ",
        "pasta_agrupados": "14kWNFrSTz6pzC_VG6avrlfARq0b0ll9S",
        "pasta_morto": "1JqL_VEJlRpIar5k4qhu8oMn6jcAwYGCo"
    },
    {
        "ano": "2025",
        "url": "https://mtsp.detran.sc.gov.br/portarias_web/portarias.php?ano=2025",
        "pasta_destino": "1DSbQqQpr_e9dkDkU6w27JGrytqQeisvn",
        "pasta_agrupados": "1nC9kA1calLN6waqw4y7rAPGw0MFDbMxm",
        "pasta_morto": "18RAcsEf59Gp4JlepKgzYbsPx_IeMELkA"
    },
    {
        "ano": "2024",
        "url": "https://mtsp.detran.sc.gov.br/portarias_web/portarias.php?ano=outros",
        "pasta_destino": "1DXu4JJHYNuEmtIjLiTj41eavfHsIg7J1",
        "pasta_agrupados": "1YSz_v2Vb9-1WbaqiAzbnaOxLUpCFfZYQ",
        "pasta_morto": "1ZfbXrRpTTE4DzI4gagwqiqAzOnMFeAYO"
    }
]
# =======================================================

def autenticar_servicos():
    print("🔐 Autenticando com Conta de Serviço via GitHub Secrets...")
    credenciais_json = json.loads(os.environ['GCP_CREDENTIALS'])
    
    escopos = [
        'https://www.googleapis.com/auth/drive',
        'https://www.googleapis.com/auth/spreadsheets'
    ]
    
    creds = service_account.Credentials.from_service_account_info(credenciais_json, scopes=escopos)
    drive_service = build('drive', 'v3', credentials=creds)
    gc = gspread.authorize(creds)
    return drive_service, gc

def mover_para_arquivo_morto(service, origem_id, destino_id):
    print(f"📦 Arquivando lotes antigos...")
    query = f"'{origem_id}' in parents and trashed = false"
    results = service.files().list(q=query, fields="files(id, name, parents)").execute()
    arquivos = results.get('files', [])
    for arq in arquivos:
        try:
            previous_parents = ",".join(arq.get('parents'))
            service.files().update(fileId=arq['id'], addParents=destino_id, removeParents=previous_parents).execute()
            print(f"   [OK] Movido: {arq['name']}")
        except: pass

def upload_arquivo_drive(service, nome_arquivo, conteudo_bytes, folder_id):
    metadata = {'name': nome_arquivo, 'parents': [folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(conteudo_bytes), mimetype='application/pdf')
    arquivo = service.files().create(body=metadata, media_body=media, fields='id').execute()
    return arquivo.get('id')

def executar_robo():
    drive_service, gc = autenticar_servicos()
    
    try:
        sh = gc.open_by_url(URL_PLANILHA)
        ws_controle = sh.worksheet(NOME_ABA_CONTROLE)
        ws_log = sh.worksheet(NOME_ABA_LOG)
    except Exception as e:
        print(f"❌ Erro nas abas da planilha: {e}"); return

    log_baixados = ws_log.col_values(2)[1:]
    set_ja_processados = set(log_baixados)
    
    registros_log_geral = []
    nomes_unificados_geral = []
    headers = {'User-Agent': 'Mozilla/5.0'}

    for config in CONFIGS_ANOS:
        print(f"\n==================================================")
        print(f"🌍 Escaneando site do ano: {config['ano']} por novidades...")
        print(f"==================================================")
        
        novos_para_baixar = []
        
        try:
            res = requests.get(config['url'], headers=headers, timeout=30)
            soup = BeautifulSoup(res.text, 'html.parser')
            for link in soup.find_all("a", href=True):
                if link['href'].lower().endswith('.pdf'):
                    url_pdf = urljoin(BASE_URL, link['href'])
                    nome_pdf = url_pdf.split('/')[-1]
                    if nome_pdf not in set_ja_processados:
                        novos_para_baixar.append({'nome': nome_pdf, 'url': url_pdf})
        except Exception as e:
            print(f"❌ Erro ao acessar o site de {config['ano']}: {e}")
            continue

        print(f"📊 Encontrados {len(novos_para_baixar)} novos arquivos em {config['ano']}.")
        
        if not novos_para_baixar:
            continue

        if not SIMULACAO:
            mover_para_arquivo_morto(drive_service, config['pasta_agrupados'], config['pasta_morto'])

        for i in range(0, len(novos_para_baixar), TAMANHO_DO_GRUPO):
            lote = novos_para_baixar[i : i + TAMANHO_DO_GRUPO]
            num_lote = (i // TAMANHO_DO_GRUPO) + 1
            data_str = datetime.now().strftime('%d%m%Y_%H%M')
            nome_unificado = f"Unificado_{config['ano']}_Lote_{num_lote}_{data_str}.pdf"

            print(f"\n⚙️ Processando Lote {num_lote} de {config['ano']}...")
            merger = PdfMerger()
            sucesso_no_lote = 0

            for item in lote:
                print(f"   📥 Baixando: {item['nome']}")
                try:
                    content = requests.get(item['url'], headers=headers).content
                    upload_arquivo_drive(drive_service, item['nome'], content, config['pasta_destino'])
                    merger.append(PdfReader(io.BytesIO(content)))
                    registros_log_geral.append([datetime.now().strftime("%d/%m/%Y %H:%M"), item['nome'], nome_unificado])
                    set_ja_processados.add(item['nome'])
                    sucesso_no_lote += 1
                except Exception as e: print(f"   ❌ Erro: {e}")

            if sucesso_no_lote > 0 and not SIMULACAO:
                buffer = io.BytesIO()
                merger.write(buffer)
                upload_arquivo_drive(drive_service, nome_unificado, buffer.getvalue(), config['pasta_agrupados'])
                nomes_unificados_geral.append([nome_unificado])
                print(f"   💾 {nome_unificado} salvo com sucesso.")

    if not SIMULACAO and registros_log_geral:
        print("\n📝 Atualizando planilha geral...")
        ws_log.append_rows(registros_log_geral)
        ws_controle.batch_clear(["A2:A500"])
        ws_controle.update(range_name='A2', values=nomes_unificados_geral)
        print("✅ Tudo pronto!")
    else:
        print("\n✅ Verificação finalizada sem novas alterações.")

if __name__ == "__main__":
    executar_robo()
