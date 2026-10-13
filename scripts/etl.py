#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETL — Dashboard Produto Alimentação SESI · Área de Mercado
==========================================================
Lê a planilha exportada do CRM (pasta entrada/), aplica as regras de negócio
consolidadas do dashboard e grava alimentacao/dados.json.

Rodar local:   python scripts/etl.py
Na automação:  chamado pelo GitHub Actions a cada upload em entrada/
"""

import json
import re
import sys
import unicodedata
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

import pandas as pd

# ==========================================================================
# CONFIGURAÇÃO — os únicos valores que você mexe no dia a dia
# ==========================================================================

# Privacidade: mude para True quando quiser tirar o dado do arquivo público.
MASCARAR_CNPJ     = False   # True  -> o CNPJ sai do dados.json
ANONIMIZAR_CLIENTE = False  # True  -> razão social vira "Cliente 001"
REMOVER_MOTIVO_RECUSA = False  # True -> o motivo da recusa sai do arquivo

# Escopo do produto ALIMENTAÇÃO — lista fechada (comparação sem acento, maiúsculas).
# Para incluir um produto novo do CRM, acrescente a linha aqui.
# Vigência estimada: contratos sem Data de Início/Término usam a Data de Modificação
# como início e somam este número de meses para o término.
MESES_VIGENCIA_PADRAO = 12

# Lista "Contratos a Corrigir no CRM": contratos sem Data de Início E sem Data de
# Término, criados a partir desta data. Cobre TODO o CRM (todos os produtos e
# unidades), não só o recorte de Alimentação — é a lista de cobrança aos
# proprietários. Mude aqui para ampliar ou encurtar a janela.
PENDENCIAS_DESDE = '2025-01-01'

PRODUTOS_ESCOPO = {
    'ALMOCO PADRAO - CONSTRUCAO CIVIL',
    'ALMOCO PADRAO - EMPRESAS',
    'CAFE DA MANHA - CONSTRUCAO CIVIL',
    'FORNECIMENTO DE LANCHE',
    'FORNECIMENTO DE REFEICAO - REST SESI TAGUATINGA',
    'FORNECIMENTO DE REFEICOES',
}

# Trava de segurança: se a planilha nova trouxer menos registros que isto,
# o processo falha e o dashboard continua com o dado anterior.
MINIMO_REGISTROS = 500

# Pasta do dashboard dentro do repositório meus-dashboards.
# A Vercel publica a raiz do repositório, então esta pasta é a rota /alimentacao.
PASTA_SITE = 'alimentacao'

# ==========================================================================
# OUTROS DASHBOARDS ALIMENTADOS PELA MESMA PLANILHA
# Cada um recebe <pasta>/base.json com as LINHAS BRUTAS do seu recorte.
# O próprio dashboard aplica as regras dele — nada muda no jeito de calcular.
# Para ativar um novo, basta acrescentar uma entrada aqui.
# ==========================================================================
COLUNAS_BRUTAS = [
    'Proprietário', 'ID da Proposta', 'ID da Revisão', 'CNPJ (Cliente)', 'Cliente',
    'CNAE (Cliente)', 'Porte', 'Razão do Status', 'Entidade/Unidade', 'Produto Existente',
    'Data do Aceite', 'Data de Modificação', 'Valor Total',
    'Quantidade', 'Quantidade de Pessoas Atendidas', 'Email (Contato)',
    'Endereço Principal: Bairro (Cliente)', 'Cidade (Cliente)', 'Endereço 1: Estado (Cliente)',
]

# E-mail do contato: o repositório é público, então o endereço real NÃO sai do CRM.
# Os dashboards (Vacinas e IEL) usam essa coluna apenas para medir qualidade de
# cadastro — quantos contatos têm e-mail válido, vazio ou inválido. Enviamos um
# marcador que preserva as três categorias sem revelar ninguém.
# Mude para False se algum dia o repositório virar privado e você quiser o e-mail real.
EMAIL_SOMENTE_INDICADOR = True
EMAIL_MARCADOR_VALIDO   = 'contato@omitido.invalid'   # vira "OK" no painel de qualidade
EMAIL_MARCADOR_INVALIDO = 'invalido'                  # vira "INVÁLIDO"

_EMAIL_RE = re.compile(r'^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$')


def indicador_email(v):
    """Converte o e-mail real no marcador equivalente, preservando a categoria."""
    s = texto(v, '')
    if not s:
        return None
    return EMAIL_MARCADOR_VALIDO if _EMAIL_RE.match(s.strip().lower().replace(' ', '')) \
        else EMAIL_MARCADOR_INVALIDO

OUTROS_DASHBOARDS = {
    'bp': {
        'descricao': 'Brasil Mais Produtivo',
        'tipo': 'produto',
        'valores': {'NOVO B P MANUFATURA ENXUTA', 'NOVO B P EFICIENCIA ENERGETICA'},
    },
    'vacinas': {
        'descricao': 'Campanha de Vacinação',
        'tipo': 'produto_contem',
        'valores': {'VACINA'},
    },
    'iel': {
        'descricao': 'IEL',
        'tipo': 'entidade_contem',
        'valores': {'IEL'},
    },
    'producao-area-mercado': {
        'descricao': 'Produção da Área de Mercado',
        'tipo': 'tudo',              # sem recorte: o dashboard aplica a própria carteira
        'valores': set(),
        # Formato colunar: em vez de repetir o nome da coluna em cada linha, o arquivo
        # traz o cabeçalho UMA vez e as linhas como listas. Esta é a maior base do
        # repositório (~18 mil linhas) e o ganho é de mais da metade do tamanho.
        'formato': 'colunar',
        # Só as colunas que este dashboard realmente lê (as 11 obrigatórias do
        # importador + Bairro/Cidade/UF, usadas para posicionar o cliente no mapa).
        'colunas': [
            'ID da Proposta', 'Proprietário', 'Cliente', 'CNPJ (Cliente)',
            'Entidade/Unidade', 'Razão do Status', 'Produto Existente', 'Porte',
            'Valor Total', 'Data do Aceite', 'Data de Modificação',
            'Endereço Principal: Bairro (Cliente)', 'Cidade (Cliente)',
            'Endereço 1: Estado (Cliente)',
        ],
    },
}

CONTRATOS = {}   # preenchido em main() a partir da planilha de contratos
PENDENCIAS = []  # contratos sem vigência cadastrada, para a lista de correção

RAIZ    = Path(__file__).resolve().parent.parent
ENTRADA = RAIZ / 'entrada'
SAIDA   = RAIZ / PASTA_SITE / 'dados.json'
FUSO_BR = timezone(timedelta(hours=-3))

COLUNAS = {
    'pro':   ['proprietario'],
    'id':    ['id da proposta'],
    'cnpj':  ['cnpj (cliente)', 'cnpj'],
    'cli':   ['cliente'],
    'cnae':  ['cnae (cliente)', 'cnae'],
    'porte': ['porte'],
    'st':    ['razao do status', 'status'],
    'ent':   ['entidade/unidade', 'entidade'],
    'prod':  ['produto existente', 'produto'],
    'dc':    ['data de criacao'],
    'da':    ['data do aceite'],
    'dm':    ['data de modificacao'],
    'vt':    ['valor total'],          # coluna T — a PRIMEIRA ocorrência
    'qtd':   ['quantidade'],
    'pes':   ['quantidade de pessoas atendidas'],
    'cid':   ['cidade (cliente)'],
    'ind':   ['e industria (cliente)'],
    'mot':   ['motivo da recusa'],
}


def log(msg):
    print(f'[etl] {msg}', flush=True)


def erro(msg):
    print(f'::error::{msg}', flush=True)
    sys.exit(1)


def sem_acento(v):
    if v is None:
        return ''
    txt = unicodedata.normalize('NFD', str(v))
    return ''.join(c for c in txt if unicodedata.category(c) != 'Mn').upper().strip()


def listar_planilhas():
    if not ENTRADA.exists():
        erro(f'Pasta "entrada/" não encontrada em {RAIZ}.')
    arqs = [p for p in ENTRADA.iterdir()
            if p.suffix.lower() in ('.xlsx', '.xlsm', '.xls') and not p.name.startswith('~$')]
    if not arqs:
        erro('Nenhuma planilha (.xlsx) encontrada na pasta "entrada/". '
             'Faça o upload da exportação do CRM e tente de novo.')
    return sorted(arqs, key=lambda p: p.stat().st_mtime, reverse=True)


def tipo_da_planilha(caminho):
    """Descobre pelo conteúdo se o arquivo é a Base de Mercado ou Todos os Contratos."""
    try:
        topo = pd.read_excel(caminho, header=None, nrows=12, dtype=object)
    except Exception:
        return None, None
    for i in range(len(topo)):
        linha = [sem_acento(c) for c in topo.iloc[i].tolist()]
        tem_contrato = any('ID DO CONTRATO' in c for c in linha)
        tem_proposta = any(c == 'ID DA PROPOSTA' for c in linha)
        tem_produto  = any('PRODUTO EXISTENTE' in c for c in linha)
        if tem_contrato:
            return 'contratos', i
        if tem_proposta and tem_produto:
            return 'mercado', i
    return None, None


def escolher_planilhas():
    """Separa os arquivos da pasta entrada/ por tipo, usando o mais recente de cada."""
    mercado = contratos = None
    pulo_m = pulo_c = 0
    for arq in listar_planilhas():
        tipo, pulo = tipo_da_planilha(arq)
        if tipo == 'mercado' and mercado is None:
            mercado, pulo_m = arq, pulo
        elif tipo == 'contratos' and contratos is None:
            contratos, pulo_c = arq, pulo
    if mercado is None:
        erro('Nenhuma planilha reconhecida como Base de Produção Mercado na pasta "entrada/". '
             'O arquivo precisa ter as colunas "ID da Proposta" e "Produto Existente".')
    return mercado, pulo_m, contratos, pulo_c


def carregar_contratos(caminho, pulo):
    """Monta o mapa ID da Proposta -> dados do contrato (usa o contrato mais recente)."""
    if caminho is None:
        return {}
    df = pd.read_excel(caminho, skiprows=pulo, dtype=object)
    df.columns = [str(c).strip() for c in df.columns]

    def achar(*chaves):
        for col in df.columns:
            c = sem_acento(col)
            if all(k in c for k in chaves):
                return col
        return None

    col_pid = achar('ID DA PROPOSTA')
    col_ctr = achar('ID DO CONTRATO')
    if not col_pid or not col_ctr:
        log('AVISO: planilha de contratos sem "ID da Proposta" ou "ID do Contrato" — ignorada.')
        return {}
    col_st  = achar('STATUS')
    col_ini = achar('DATA', 'INICIO')
    col_fim = achar('DATA', 'TERMINO')
    col_val = achar('VALOR TOTAL')
    col_cnaed = achar('DESCRICAO DO CNAE')
    col_prod = achar('RESUMO DE PRODUTOS')
    col_cri = achar('DATA DE CRIACAO')
    col_dono = achar('PROPRIETARIO')
    col_cnpj = achar('CNPJ')
    col_ent = achar('ENTIDADE')
    # "Cliente" precisa de correspondência EXATA: a planilha também tem
    # "CNPJ (Cliente) (Cliente)" e "CNAE (Cliente) (Cliente)", que casariam
    # com uma busca por conteúdo e trariam o CNPJ no lugar da razão social.
    col_cli = None
    for col in df.columns:
        if sem_acento(col) == 'CLIENTE':
            col_cli = col
            break
    # "Data de Modificação" aparece duas vezes (uma com o prefixo "(Não Modificar)").
    # A boa é a que NÃO tem esse prefixo.
    col_mod = None
    for col in df.columns:
        c = sem_acento(col)
        if 'DATA DE MODIFICACAO' in c and 'NAO MODIFICAR' not in c:
            col_mod = col
            break

    mapa = {}
    pendentes = []          # contratos sem início/término, para a lista de correção no CRM
    vistos_ctr = set()      # a planilha traz uma linha por contrato, mas garantimos unicidade
    for _, r in df.iterrows():
        ini = to_iso(r[col_ini]) if col_ini else None
        fim = to_iso(r[col_fim]) if col_fim else None

        # ---- Lista de pendências: sem NENHUMA das duas datas, criado a partir de
        # PENDENCIAS_DESDE. Vale para TODO o CRM, não só o recorte de Alimentação.
        cri = to_iso(r[col_cri]) if col_cri else None
        id_ctr = texto(r[col_ctr], '')
        if (not ini and not fim) and id_ctr and id_ctr not in vistos_ctr:
            if cri and cri >= PENDENCIAS_DESDE:
                vistos_ctr.add(id_ctr)
                pendentes.append({
                    'ctr':   id_ctr,
                    'dono':  texto(r[col_dono], '') if col_dono else '',
                    'cli':   texto(r[col_cli], '') if col_cli else '',
                    'cnpj':  '' if MASCARAR_CNPJ else limpar_cnpj(r[col_cnpj]) if col_cnpj else '',
                    'ent':   texto(r[col_ent], '') if col_ent else '',
                    'prod':  texto(r[col_prod], '') if col_prod else '',
                    'val':   round(to_num(r[col_val]), 2) if col_val else 0,
                    'cri':   cri,
                    'mod':   to_iso(r[col_mod]) if col_mod else None,
                    'st':    texto(r[col_st], '') if col_st else '',
                })

        pid = texto(r[col_pid], '')
        if not pid:
            continue
        estimado = False
        if not ini or not fim:
            # Regra: sem vigência informada, a Data de Modificação vira o início
            # e o término é 12 meses depois.
            mod = to_iso(r[col_mod]) if col_mod else None
            if mod:
                ini = ini or mod
                if not fim:
                    d = datetime.strptime(ini, '%Y-%m-%d')
                    ano, mes = d.year + (d.month - 1 + MESES_VIGENCIA_PADRAO) // 12, \
                               (d.month - 1 + MESES_VIGENCIA_PADRAO) % 12 + 1
                    dia = min(d.day, [31,29 if ano%4==0 and (ano%100!=0 or ano%400==0) else 28,
                                      31,30,31,30,31,31,30,31,30,31][mes-1])
                    fim = f'{ano:04d}-{mes:02d}-{dia:02d}'
                estimado = True
        prods = texto(r[col_prod], '') if col_prod else ''
        reg = {
            'ctr':    texto(r[col_ctr], ''),
            'ctrSt':  texto(r[col_st], '') if col_st else '',
            'ctrIni': ini,
            'ctrFim': fim,
            'ctrEst': estimado,
            'ctrVal': round(to_num(r[col_val]), 2) if col_val else 0,
            'cnaeD':  texto(r[col_cnaed], '') if col_cnaed else '',
            'ctrProd': prods,
            'ctrAli': any(a in sem_acento(prods) for a in PRODUTOS_ESCOPO),
        }
        ant = mapa.get(pid)
        if ant is None:
            reg['ctrN'] = 1
            mapa[pid] = reg
        else:
            reg['ctrN'] = ant['ctrN'] + 1
            # fica com o de término mais distante (o contrato mais "vivo")
            if (reg['ctrFim'] or '') >= (ant['ctrFim'] or ''):
                mapa[pid] = reg
            else:
                ant['ctrN'] = reg['ctrN']
    pendentes.sort(key=lambda x: ((x['dono'] or 'ZZZ').upper(), x['cri'] or ''))
    globals()['PENDENCIAS'] = pendentes
    est = sum(1 for v in mapa.values() if v['ctrEst'])
    ali = sum(1 for v in mapa.values() if v['ctrAli'])
    log(f'Contratos: {len(df)} linhas lidas, {len(mapa)} propostas com contrato')
    log(f'   sem vigência no CRM, criados desde {PENDENCIAS_DESDE}: {len(pendentes)} '
        f'({len(set(p["dono"] for p in pendentes))} proprietários)')
    log(f'   vigência estimada pela Data de Modificação + {MESES_VIGENCIA_PADRAO} meses: {est}')
    log(f'   com produto de Alimentação no Resumo de Produtos: {ali}')
    return mapa


def main():
    arq = achar_planilha()


def achar_cabecalho(caminho):
    """A exportação do CRM traz linhas de título antes do cabeçalho real."""
    topo = pd.read_excel(caminho, header=None, nrows=40, dtype=object)
    for i in range(len(topo)):
        linha = [sem_acento(c) for c in topo.iloc[i].tolist()]
        if 'ID DA PROPOSTA' in linha and ('CLIENTE' in linha or any('RAZAO DO STATUS' in c for c in linha)):
            return i
    erro('Cabeçalho não localizado: nenhuma linha da planilha contém "ID da Proposta". '
         'Confira se o arquivo é mesmo a exportação de propostas do CRM.')


def mapear_colunas(df):
    cab = {sem_acento(c): i for i, c in enumerate(df.columns)}
    idx = {}
    for chave, alternativas in COLUNAS.items():
        idx[chave] = None
        for alt in alternativas:
            pos = cab.get(sem_acento(alt))
            if pos is not None:
                idx[chave] = df.columns[pos]
                break
    faltando = [k for k in ('id', 'prod', 'st', 'vt') if idx[k] is None]
    if faltando:
        erro(f'Colunas obrigatórias ausentes na planilha: {", ".join(faltando)}. '
             f'Colunas encontradas: {list(df.columns)[:25]}')
    return idx


def to_iso(v):
    """Data sem deslocamento de fuso — aceita Date, serial Excel, DD/MM/AAAA e AAAA-MM-DD."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
        return None
    if isinstance(v, (int, float)):
        try:
            base = datetime(1899, 12, 30) + timedelta(days=int(v))
            return base.strftime('%Y-%m-%d')
        except Exception:
            return None
    t = pd.to_datetime(v, errors='coerce', dayfirst=True)
    return None if pd.isna(t) else t.strftime('%Y-%m-%d')


def to_num(v):
    if v is None or pd.isna(v):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace('R$', '').strip()
    s = s.replace('.', '').replace(',', '.') if ',' in s else s
    try:
        return float(re.sub(r'[^\d.\-]', '', s) or 0)
    except ValueError:
        return 0.0


def limpar_cnpj(v):
    if v is None or pd.isna(v):
        return ''
    d = re.sub(r'\D', '', str(v).split('.')[0])
    return d.zfill(14) if d else ''


def limpar_cnae(v):
    if v is None or pd.isna(v):
        return ''
    d = re.sub(r'\D', '', str(v).split('.')[0])
    return d.zfill(7)[:7] if d else ''


def texto(v, padrao=''):
    if v is None or pd.isna(v):
        return padrao
    s = str(v).strip()
    return padrao if s.lower() in ('nan', 'none', 'nat') else s


def valor_bruto(col, v):
    """Converte UMA célula para o tipo que os dashboards esperam."""
    if col.startswith('Data'):
        return to_iso(v)
    if col == 'Valor Total':
        return round(to_num(v), 2)
    if col in ('Quantidade', 'Quantidade de Pessoas Atendidas'):
        return to_num(v)
    if col == 'Email (Contato)':
        return indicador_email(v) if EMAIL_SOMENTE_INDICADOR else texto(v, None)
    return texto(v, None)


def linha_bruta(r, df):
    """Converte uma linha da planilha nas colunas originais que os outros dashboards leem."""
    out = {}
    for col in COLUNAS_BRUTAS:
        if col not in df.columns:
            continue
        v = r[col]
        if col.startswith('Data'):
            out[col] = to_iso(v)
        elif col == 'Valor Total':
            out[col] = round(to_num(v), 2)
        elif col in ('Quantidade', 'Quantidade de Pessoas Atendidas'):
            out[col] = to_num(v)
        elif col == 'Email (Contato)':
            out[col] = indicador_email(v) if EMAIL_SOMENTE_INDICADOR else texto(v, None)
        else:
            out[col] = texto(v, None)
    return out


def gerar_outros_dashboards(df, idx, nome_arquivo):
    """Gera <pasta>/base.json para cada dashboard alimentado pela mesma planilha."""
    if not OUTROS_DASHBOARDS:
        return
    prod = df[idx['prod']].map(sem_acento)
    ent  = df['Entidade/Unidade'].map(sem_acento) if 'Entidade/Unidade' in df.columns else None
    carimbo = datetime.now(FUSO_BR).strftime('%Y-%m-%dT%H:%M:%S')

    for pasta, cfg in OUTROS_DASHBOARDS.items():
        tipo, alvo = cfg['tipo'], cfg['valores']
        if tipo == 'produto':
            m = prod.isin(alvo)
        elif tipo == 'produto_contem':
            m = prod.apply(lambda v: any(a in v for a in alvo))
        elif tipo == 'entidade_contem':
            m = ent.apply(lambda v: any(a in v for a in alvo)) if ent is not None else prod.apply(lambda v: False)
        else:
            m = pd.Series(True, index=df.index)

        sub = df[m]
        destino = RAIZ / pasta / 'base.json'
        if not destino.parent.exists():
            log(f'AVISO: pasta "{pasta}" não existe no repositório — pulando.')
            continue

        if cfg.get('formato') == 'colunar':
            # Cabeçalho uma vez + linhas como listas. Vale a pena em bases grandes.
            cols = [c for c in cfg.get('colunas', COLUNAS_BRUTAS) if c in df.columns]
            faltando = [c for c in cfg.get('colunas', []) if c not in df.columns]
            if faltando:
                log(f'   AVISO ({pasta}): colunas ausentes na planilha: {", ".join(faltando)}')
            linhas = [[valor_bruto(c, r[c]) for c in cols] for _, r in sub.iterrows()]
            pacote = {'gerado_em': carimbo, 'origem': nome_arquivo, 'formato': 'colunar',
                      'total': len(linhas), 'cols': cols, 'rows': linhas}
        else:
            linhas = [linha_bruta(r, df) for _, r in sub.iterrows()]
            pacote = {'gerado_em': carimbo, 'origem': nome_arquivo,
                      'total': len(linhas), 'rows': linhas}

        destino.write_text(json.dumps(pacote, ensure_ascii=False, separators=(',', ':')),
                           encoding='utf-8')
        log(f'   + {pasta}/base.json  ({cfg["descricao"]}): {len(linhas)} linhas, '
            f'{destino.stat().st_size / 1024:.0f} KB')


# ============================================================================
# PAINEL DE CONTRATOS  (contratos/base.json)  — 13/10/2026
# ----------------------------------------------------------------------------
# Sai da MESMA planilha "Todos os Contratos" que ja alimenta o bloco de
# contratos do painel de Alimentacao. Nenhum upload novo e necessario.
#
# Regras combinadas com a area:
#   · 1 linha da planilha = 1 contrato (os IDs sao unicos).
#   · Contratos com Valor Total <= 0 CONTAM no volume, mas ficam fora dos
#     indicadores financeiros. Sao gratuidades e termos institucionais.
#   · A coluna "E Industria" nao e usada.
#   · Todos os proprietarios entram; so as grafias duplicadas sao unificadas.
#   · Entidade/Unidade e normalizada e agrupada por entidade, na mesma regra
#     do painel Producao de Mercado.
#
# Formato de saida (compacto, para o arquivo ficar pequeno):
#   contratos = [criacaoDias, inicioDias, fimDias, statusIdx, valor,
#                propIdx, cliIdx, cnpjIdx, unidIdx, porteIdx, cnaeIdx, [prodIdxs]]
#   inicioDias / fimDias = -1 quando a data nao esta cadastrada.
# ============================================================================
CTR_PASTA       = 'contratos'
CTR_EPOCH       = date(2022, 1, 1)
CTR_STATUS      = ['Ativo', 'Cumprido', 'Faturado', 'Cancelado']
CTR_PORTE       = ['MICRO EMPRESA', 'EMPRESA DE PEQUENO PORTE', 'DEMAIS', 'Não informado']
CTR_ENTIDADES   = ['SESI', 'SENAI', 'ALIMENTAÇÃO', 'IST', 'IEL', 'FIBRA', 'OUTRAS']

# grafias diferentes da mesma pessoa / da mesma unidade no CRM
CTR_MERGE_PROP  = {'ALESSANDRA CAMPOS': 'Alessandra Campos Castanheira'}
CTR_MERGE_UNID  = {'IST': 'Instituto Senai de Tecnologia (IST)',
                   'SISTEMAFIBRA': 'FIBRA',
                   'SESI SIG': 'SESI-SIG',
                   'SESI CENTRAL PARK': 'SESI-CENTRAL PARK'}

_CTR_PEQUENAS = {'de', 'da', 'do', 'das', 'dos', 'e', 'em', 'para', 'a', 'o',
                 'as', 'os', 'com', 'sem', 'por', 'ou', 'na', 'no', 'nas', 'nos'}


def ctr_chave(v):
    s = sem_acento(v)
    return re.sub(r'\s+', ' ', re.sub(r'[^A-Z0-9]+', ' ', s)).strip()


def ctr_entidade(unidade):
    """A ORDEM DOS TESTES IMPORTA: Alimentacao vem antes de SESI porque
    SESI-GUARA comeca com SESI; IST vem antes de SENAI pelo mesmo motivo."""
    u = ctr_chave(unidade)
    if not u:
        return 'OUTRAS'
    if 'RESTAURANTE SESI' in u or 'SESI GUARA' in u:
        return 'ALIMENTAÇÃO'
    if 'INSTITUTO SENAI' in u or re.search(r'(^| )IST( |$)', u):
        return 'IST'
    if 'FIBRA' in u or 'FEDERACAO DAS IND' in u:
        return 'FIBRA'
    if 'IEL' in u:
        return 'IEL'
    if 'SENAI' in u:
        return 'SENAI'
    if 'SESI' in u:
        return 'SESI'
    return 'OUTRAS'


def ctr_cnae(v):
    """O CRM grava o mesmo setor ora em caixa alta, ora em caixa mista. Sem
    baixar tudo para minusculas antes do Title Case, o painel mostrava
    "CONSTRUCAO DE EDIFICIOS" e "Construcao de edificios" como dois setores."""
    s = texto(v, '').strip()
    if not s:
        return ''
    partes = re.sub(r'\s+', ' ', s).lower().split(' ')
    return ' '.join(p if (i and p in _CTR_PEQUENAS) else p.capitalize()
                    for i, p in enumerate(partes))


def ctr_desaspa(s):
    # o export as vezes vem com aspas de CSV, dobradas por dentro do texto.
    # texto() tambem derruba os NaN do pandas — sem isso 18 contratos sem
    # produto viravam um produto chamado "nan".
    s = texto(s, '').strip()
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    return s.replace('""', '"').strip()


def ctr_vocabulario(valores):
    """Existe nome de produto com virgula dentro (ex.: 'Consultoria para
    atendimento a Legislacoes, Normas e Regulamentos Tecnicos'), entao nao da
    para separar por virgula na marra. O vocabulario sai das linhas que tem UM
    produto so — essas sao seguras."""
    vocab = set()
    for v in valores:
        v = ctr_desaspa(v)
        if v and ',' not in v:
            vocab.add(v)
    return vocab


def ctr_produtos(txt, vocab):
    """Casamento mais longo nos limites de virgula. O que nao casar volta
    inteiro, para nunca inventar um produto que nao existe na planilha."""
    t = ctr_desaspa(txt)
    if not t:
        return []
    if ',' not in t:
        return [t]
    partes = [p.strip() for p in t.split(',')]
    saida, i = [], 0
    while i < len(partes):
        casou = False
        for j in range(len(partes), i, -1):
            cand = ', '.join(partes[i:j]).strip()
            if cand in vocab:
                saida.append(cand)
                i = j
                casou = True
                break
        if not casou:
            saida.append(partes[i])
            i += 1
    vistos, unicos = set(), []
    for p in saida:                       # o mesmo produto repete na mesma linha
        if p and p not in vistos:
            vistos.add(p)
            unicos.append(p)
    return unicos


def ctr_dias(v):
    iso = to_iso(v)
    if not iso:
        return -1
    try:
        a, m, d = (int(x) for x in iso[:10].split('-'))
        return (date(a, m, d) - CTR_EPOCH).days
    except Exception:
        return -1


def gerar_contratos(caminho, pulo):
    """Le a planilha de contratos e grava contratos/base.json."""
    if caminho is None:
        log('AVISO: sem planilha de contratos — painel de Contratos nao gerado.')
        return
    df = pd.read_excel(caminho, skiprows=pulo, dtype=object)
    df.columns = [str(c).strip() for c in df.columns]

    def achar(*chaves, exato=None):
        if exato:
            for col in df.columns:
                if sem_acento(col) == exato:
                    return col
            return None
        for col in df.columns:
            c = sem_acento(col)
            if all(k in c for k in chaves) and 'NAO MODIFICAR' not in c:
                return col
        return None

    col = {
        'ctr':  achar('ID DO CONTRATO'),
        'prop': achar('PROPRIETARIO'),
        'cli':  achar(exato='CLIENTE'),
        'cnpj': achar('CNPJ'),
        'cnae': achar('DESCRICAO DO CNAE'),
        'porte': achar('PORTE'),
        'prod': achar('RESUMO DE PRODUTOS'),
        'unid': achar('ENTIDADE'),
        'val':  achar('VALOR TOTAL'),
        'cri':  achar('DATA DE CRIACAO'),
        'ini':  achar('DATA', 'INICIO'),
        'fim':  achar('DATA', 'TERMINO'),
        'st':   achar('STATUS'),
    }
    faltando = [k for k, v in col.items() if v is None]
    if faltando:
        log(f'AVISO: planilha de contratos sem as colunas {faltando} — painel de Contratos nao gerado.')
        return

    vocab = ctr_vocabulario(df[col['prod']].tolist())

    props, clis, cnpjs, unids, cnaes, prods = {}, {}, {}, {}, {}, {}

    def idx(dic, valor):
        if valor not in dic:
            dic[valor] = len(dic)
        return dic[valor]

    linhas, vistos, fora = [], set(), 0
    for _, r in df.iterrows():
        idc = texto(r[col['ctr']], '')
        if not idc or idc in vistos:
            continue
        vistos.add(idc)

        st = texto(r[col['st']], '')
        if st not in CTR_STATUS:
            fora += 1
            continue

        nome = texto(r[col['prop']], '') or 'Não informado'
        nome = CTR_MERGE_PROP.get(ctr_chave(nome), nome)

        un = texto(r[col['unid']], '') or 'Não informado'
        un = CTR_MERGE_UNID.get(ctr_chave(un), un)

        porte = texto(r[col['porte']], '')
        if porte not in CTR_PORTE:
            porte = 'Não informado'

        cnae = ctr_cnae(r[col['cnae']])
        cnpj = limpar_cnpj(r[col['cnpj']])
        lista = ctr_produtos(r[col['prod']], vocab)

        linhas.append([
            ctr_dias(r[col['cri']]),
            ctr_dias(r[col['ini']]),
            ctr_dias(r[col['fim']]),
            CTR_STATUS.index(st),
            round(to_num(r[col['val']]), 2),
            idx(props, nome),
            idx(clis, texto(r[col['cli']], '') or 'Não informado'),
            idx(cnpjs, cnpj) if cnpj else -1,
            idx(unids, un),
            CTR_PORTE.index(porte),
            idx(cnaes, cnae) if cnae else -1,
            [idx(prods, p) for p in lista],
        ])

    if len(linhas) < 100:
        log(f'AVISO: apenas {len(linhas)} contratos lidos — abaixo do minimo de seguranca. '
            'O painel de Contratos segue com a base anterior.')
        return

    def chaves(d):
        return [k for k, _ in sorted(d.items(), key=lambda kv: kv[1])]

    criacoes = [c[0] for c in linhas if c[0] >= 0]
    hoje = datetime.now(FUSO_BR).date()
    saida = {
        'gerado_em': datetime.now(FUSO_BR).strftime('%Y-%m-%dT%H:%M:%S'),
        'origem': caminho.name,
        'meta': {
            'epoch': CTR_EPOCH.isoformat(),
            'hoje_dias': (hoje - CTR_EPOCH).days,
            'hoje_iso': hoje.isoformat(),
            'total': len(linhas),
            'data_min_dias': min(criacoes) if criacoes else 0,
            'data_max_dias': max(criacoes) if criacoes else 0,
            'anos': sorted({(CTR_EPOCH + timedelta(days=d)).year for d in criacoes}),
            'avisos': ([f'{fora} linhas com status fora da lista'] if fora else []),
        },
        'lookups': {
            'status': CTR_STATUS, 'porte': CTR_PORTE,
            'proprietarios': chaves(props), 'clientes': chaves(clis),
            'cnpjs': chaves(cnpjs), 'unidades': chaves(unids),
            'cnaes': chaves(cnaes), 'produtos': chaves(prods),
            'entidades': CTR_ENTIDADES,
            'entidadePorUnidade': [ctr_entidade(u) for u in chaves(unids)],
        },
        'contratos': linhas,
    }

    destino = RAIZ / CTR_PASTA / 'base.json'
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(saida, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')

    ativos = sum(1 for c in linhas if c[3] == CTR_STATUS.index('Ativo'))
    valor = sum(c[4] for c in linhas if c[4] > 0)
    semvig = sum(1 for c in linhas if c[3] == CTR_STATUS.index('Ativo') and c[2] < 0)
    log(f'OK  {len(linhas)} contratos gravados em {CTR_PASTA}/base.json '
        f'({destino.stat().st_size / 1024:.0f} KB)')
    log(f'    Ativos: {ativos} · sem vigencia cadastrada: {semvig}')
    log(('    Valor contratado: R$ %s' % format(valor, ',.2f'))
        .replace(',', 'X').replace('.', ',').replace('X', '.'))



def main():
    arq, pulo, arq_ctr, pulo_ctr = escolher_planilhas()
    log(f'Base de Mercado: {arq.name} ({arq.stat().st_size / 1048576:.1f} MB), cabeçalho na linha {pulo + 1}')
    if arq_ctr:
        log(f'Contratos: {arq_ctr.name} ({arq_ctr.stat().st_size / 1048576:.1f} MB)')
    else:
        log('Nenhuma planilha de contratos na pasta — o dashboard fica sem os dados de contrato.')
    CONTRATOS = carregar_contratos(arq_ctr, pulo_ctr)
    globals()['CONTRATOS'] = CONTRATOS

    df = pd.read_excel(arq, skiprows=pulo, dtype=object)
    df.columns = [str(c).strip() for c in df.columns]
    log(f'{len(df)} linhas lidas, {len(df.columns)} colunas')

    idx = mapear_colunas(df)

    escopo = df[df[idx['prod']].map(lambda v: sem_acento(v) in PRODUTOS_ESCOPO)]
    log(f'{len(escopo)} linhas dentro do escopo Alimentação ({len(PRODUTOS_ESCOPO)} produtos)')

    if len(escopo) < MINIMO_REGISTROS:
        erro(f'Apenas {len(escopo)} registros no escopo — abaixo do mínimo de {MINIMO_REGISTROS}. '
             'A planilha pode estar incompleta ou filtrada. Publicação cancelada; '
             'o dashboard segue com a base anterior.')

    registros = []
    codigos_cliente = {}
    for _, r in escopo.iterrows():
        g = lambda k: (r[idx[k]] if idx[k] is not None else None)

        cliente = texto(g('cli'))
        if ANONIMIZAR_CLIENTE:
            if cliente not in codigos_cliente:
                codigos_cliente[cliente] = f'Cliente {len(codigos_cliente) + 1:03d}'
            cliente = codigos_cliente[cliente]

        registros.append({
            'pro':   texto(g('pro')),
            'id':    texto(g('id')),
            'cnpj':  '' if MASCARAR_CNPJ else limpar_cnpj(g('cnpj')),
            'cli':   cliente,
            'cnae':  limpar_cnae(g('cnae')),
            'porte': texto(g('porte'), 'NÃO INFORMADO').upper(),
            'st':    texto(g('st')),
            'ent':   texto(g('ent'), 'NÃO INFORMADO'),
            'prod':  texto(g('prod')),
            'da':    to_iso(g('da')),
            'dm':    to_iso(g('dm')),
            'dc':    to_iso(g('dc')),
            'vt':    round(to_num(g('vt')), 2),
            'qtd':   to_num(g('qtd')),
            'pes':   to_num(g('pes')),
            'cid':   texto(g('cid')),
            'ind':   texto(g('ind')),
            'mot':   '' if REMOVER_MOTIVO_RECUSA else texto(g('mot')),
        })
        ct = CONTRATOS.get(texto(g('id'), ''))
        if ct:
            registros[-1].update({'ctr': ct['ctr'], 'ctrSt': ct['ctrSt'], 'ctrIni': ct['ctrIni'],
                                  'ctrFim': ct['ctrFim'], 'ctrVal': ct['ctrVal'], 'ctrN': ct['ctrN'],
                                  'cnaeD': ct['cnaeD'], 'ctrEst': ct['ctrEst'],
                                  'ctrProd': ct['ctrProd'], 'ctrAli': ct['ctrAli']})

    # ---- resumo para conferência no log da automação ----
    por_status = {}
    for x in registros:
        chave = sem_acento(x['st'])[:20]
        por_status[chave] = por_status.get(chave, 0) + 1
    aceitas = [x for x in registros if sem_acento(x['st']).startswith('ACEIT')]
    valor_aceito = sum(x['vt'] for x in aceitas)
    datas = sorted(d for d in (x['da'] or x['dm'] for x in registros) if d)

    saida = {
        'gerado_em': datetime.now(FUSO_BR).strftime('%Y-%m-%dT%H:%M:%S'),
        'origem': arq.name,
        'total': len(registros),
        'periodo': {'inicio': datas[0] if datas else None, 'fim': datas[-1] if datas else None},
        'privacidade': {
            'cnpj_mascarado': MASCARAR_CNPJ,
            'cliente_anonimizado': ANONIMIZAR_CLIENTE,
            'motivo_recusa_removido': REMOVER_MOTIVO_RECUSA,
        },
        'registros': registros,
        # Contratos sem Data de Início/Término no CRM — lista de correção.
        # Universo: todo o CRM, criados a partir de PENDENCIAS_DESDE.
        'pendencias_contrato': {
            'desde': PENDENCIAS_DESDE,
            'total': len(PENDENCIAS),
            'itens': PENDENCIAS,
        },
    }

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps(saida, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')

    gerar_outros_dashboards(df, idx, arq.name)

    # Painel de Contratos (13/10/2026). Vai dentro de try/except de proposito:
    # se algo mudar na planilha de contratos, os outros cinco paineis continuam
    # sendo publicados normalmente e o log avisa o que aconteceu.
    try:
        gerar_contratos(arq_ctr, pulo_ctr)
    except Exception as e:
        log(f'AVISO: painel de Contratos nao gerado ({e.__class__.__name__}: {e}). '
            'Os demais dashboards nao foram afetados.')

    log('-' * 62)
    log(f'OK  {len(registros)} propostas gravadas em {PASTA_SITE}/dados.json '
        f'({SAIDA.stat().st_size / 1024:.0f} KB)')
    log(f'    Período: {datas[0] if datas else "—"} a {datas[-1] if datas else "—"}')
    log(f'    Status: {por_status}')
    log(f'    Aceitas: {len(aceitas)} · valor aceito R$ {valor_aceito:,.2f}'
        .replace(',', 'X').replace('.', ',').replace('X', '.'))
    com_ctr = sum(1 for x in registros if x.get('ctr'))
    if CONTRATOS:
        hoje_iso = datetime.now(FUSO_BR).strftime('%Y-%m-%d')
        venc = sum(1 for x in registros if x.get('ctrFim') and x['ctrFim'] < hoje_iso)
        vig  = sum(1 for x in registros if x.get('ctrFim') and x['ctrFim'] >= hoje_iso)
        log(f'    Contratos: {com_ctr} propostas com contrato · {vig} vigentes · {venc} vencidos')
    log('-' * 62)


if __name__ == '__main__':
    main()
