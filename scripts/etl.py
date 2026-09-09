#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETL — Dashboard Produto Alimentação SESI · Área de Mercado
==========================================================
Lê a planilha exportada do CRM (pasta entrada/), aplica as regras de negócio
consolidadas do dashboard e grava public/dados.json.

Rodar local:   python scripts/etl.py
Na automação:  chamado pelo GitHub Actions a cada upload em entrada/
"""

import json
import re
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
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

RAIZ    = Path(__file__).resolve().parent.parent
ENTRADA = RAIZ / 'entrada'
SAIDA   = RAIZ / 'public' / 'dados.json'
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


def achar_planilha():
    """Pega o arquivo mais recente da pasta entrada/."""
    if not ENTRADA.exists():
        erro(f'Pasta "entrada/" não encontrada em {RAIZ}.')
    arquivos = [p for p in ENTRADA.iterdir()
                if p.suffix.lower() in ('.xlsx', '.xlsm', '.xls') and not p.name.startswith('~$')]
    if not arquivos:
        erro('Nenhuma planilha (.xlsx) encontrada na pasta "entrada/". '
             'Faça o upload da exportação do CRM e tente de novo.')
    arq = max(arquivos, key=lambda p: p.stat().st_mtime)
    if len(arquivos) > 1:
        log(f'{len(arquivos)} planilhas na pasta — usando a mais recente: {arq.name}')
    return arq


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


def main():
    arq = achar_planilha()
    log(f'Lendo {arq.name} ({arq.stat().st_size / 1048576:.1f} MB)')

    pulo = achar_cabecalho(arq)
    log(f'Cabeçalho localizado na linha {pulo + 1} da planilha')

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
    }

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps(saida, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')

    log('-' * 62)
    log(f'OK  {len(registros)} propostas gravadas em public/dados.json '
        f'({SAIDA.stat().st_size / 1024:.0f} KB)')
    log(f'    Período: {datas[0] if datas else "—"} a {datas[-1] if datas else "—"}')
    log(f'    Status: {por_status}')
    log(f'    Aceitas: {len(aceitas)} · valor aceito R$ {valor_aceito:,.2f}'
        .replace(',', 'X').replace('.', ',').replace('X', '.'))
    log('-' * 62)


if __name__ == '__main__':
    main()
