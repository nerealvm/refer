"""Сборка данных для дашборда «Угольный цикл».

Шаги:
  1. Скачать открытые источники в commodities/data/raw (World Bank Pink Sheet, FRED CPI, EIA).
  2. Перевести цены в реальные доллары (US CPI-U, база — последний месяц CPI).
  3. Найти фазы больших циклов (zigzag по реальной цене, порог 0.4 в логарифме).
  4. Посчитать «дорого/дёшево» и «где мы в цикле» для угля и 20 коммодити.
  5. Записать commodities/data/*.csv и вшить JSON в дашборд.

Запуск:  python -I commodities/pipeline/build.py [--offline]
EIA API: переменная EIA_API_KEY (по умолчанию DEMO_KEY, у него жёсткий лимит запросов).
"""
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / 'data' / 'raw'
OUT = ROOT / 'data'
DASH = ROOT / 'dashboard'

WB_PAGE = 'https://www.worldbank.org/en/research/commodity-markets'
FRED_CPI = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCNS'
EIA_AER = 'https://www.eia.gov/totalenergy/data/annual/xls/stb0709.xls'
EIA_API = 'https://api.eia.gov/v2/coal'
SHORT_TON = 0.90718474  # метрических тонн в короткой тонне

THRESHOLD = 0.4  # разворот на 0.4 в логарифме: +49% от дна или −33% от пика
MIN_PHASE_MONTHS = 6  # внутренняя фаза короче полугода считается шумом
PCT_FROM = '1970-01-01'
PCT_RECENT_YEARS = 20

COMMODITIES = [
    # ключ, колонка Pink Sheet, название, группа, единица
    ('coal_au', 'Coal, Australian', 'Уголь энергетический, Австралия', 'Уголь', '$/т'),
    ('coal_za', 'Coal, South African **', 'Уголь энергетический, ЮАР', 'Уголь', '$/т'),
    ('brent', 'Crude oil, Brent', 'Нефть Brent', 'Энергия', '$/барр.'),
    ('gas_eu', 'Natural gas, Europe', 'Газ, Европа', 'Энергия', '$/млн БТЕ'),
    ('lng_jp', 'Liquefied natural gas, Japan', 'СПГ, Япония', 'Энергия', '$/млн БТЕ'),
    ('gas_us', 'Natural gas, US', 'Газ, США', 'Энергия', '$/млн БТЕ'),
    ('iron_ore', 'Iron ore, cfr spot', 'Железная руда', 'Металлы', '$/сух. т'),
    ('copper', 'Copper', 'Медь', 'Металлы', '$/т'),
    ('aluminum', 'Aluminum', 'Алюминий', 'Металлы', '$/т'),
    ('nickel', 'Nickel', 'Никель', 'Металлы', '$/т'),
    ('zinc', 'Zinc', 'Цинк', 'Металлы', '$/т'),
    ('lead', 'Lead', 'Свинец', 'Металлы', '$/т'),
    ('tin', 'Tin', 'Олово', 'Металлы', '$/т'),
    ('gold', 'Gold', 'Золото', 'Драгметаллы', '$/унц.'),
    ('silver', 'Silver', 'Серебро', 'Драгметаллы', '$/унц.'),
    ('platinum', 'Platinum', 'Платина', 'Драгметаллы', '$/унц.'),
    ('potash', 'Potassium chloride **', 'Калий хлористый', 'Удобрения', '$/т'),
    ('phosrock', 'Phosphate rock', 'Фосфорит', 'Удобрения', '$/т'),
    ('urea', 'Urea ', 'Карбамид', 'Удобрения', '$/т'),
]


# ---------- загрузка ----------

def fetch(url, dest, timeout=150, tries=3):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (commodity-cycles pipeline)'})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            dest.write_bytes(data)
            return dest
        except Exception as e:  # сеть или 5xx у EIA: повторяем с паузой
            if i == tries - 1:
                raise
            print(f'  retry {url[:80]}… ({e})')
            time.sleep(4 * (i + 1))


def download_all():
    RAW.mkdir(parents=True, exist_ok=True)
    page = urllib.request.urlopen(urllib.request.Request(WB_PAGE, headers={'User-Agent': 'Mozilla/5.0'}), timeout=60).read().decode('utf-8', 'ignore')
    links = sorted(set(re.findall(r'https://thedocs\.worldbank\.org/[^"\']+CMO-Historical-Data-Monthly\.xlsx', page)))
    if not links:
        raise SystemExit('Не нашёл ссылку на CMO-Historical-Data-Monthly.xlsx на странице World Bank')
    print('World Bank:', links[0])
    fetch(links[0], RAW / 'wb_monthly.xlsx')
    fetch(FRED_CPI, RAW / 'cpi_nsa.csv')
    fetch(EIA_AER, RAW / 'eia_aer_0709.html')
    key = os.environ.get('EIA_API_KEY', 'DEMO_KEY')
    fetch(f'{EIA_API}/price-by-rank/data/?api_key={key}&frequency=annual&data[]=price'
          f'&facets[stateRegionId][]=US&facets[coalRankId][]=BIT&length=500', RAW / 'eia_price_bit.json')
    for rank in ('MET', 'STM'):
        fetch(f'{EIA_API}/exports-imports-quantity-price/data/?api_key={key}&frequency=quarterly'
              f'&data[]=price&data[]=quantity&facets[exportImportType][]=Exports&facets[countryId][]=TOT'
              f'&facets[customsDistrictId][]=TOT&facets[coalRankId][]={rank}&length=1000',
              RAW / f'eia_exports_{rank}.json', tries=5)


# ---------- чтение ----------

def read_pink_sheet():
    m = pd.read_excel(RAW / 'wb_monthly.xlsx', sheet_name='Monthly Prices', header=None)
    hdr = m.iloc[4].tolist()
    d = m.iloc[6:].copy()
    d.columns = ['date'] + hdr[1:]
    d = d[d['date'].astype(str).str.match(r'^\d{4}M\d{2}$')]
    d = d.replace({'…': np.nan, '..': np.nan})
    d['date'] = pd.to_datetime(d['date'].str.replace('M', '-') + '-01')
    d = d.set_index('date').apply(pd.to_numeric, errors='coerce')
    updated = str(m.iloc[3, 0]).replace('Updated on', '').strip()
    return d, updated


def read_cpi():
    c = pd.read_csv(RAW / 'cpi_nsa.csv')
    c.columns = ['date', 'cpi']
    c['date'] = pd.to_datetime(c['date'])
    return c.set_index('date')['cpi'].astype(float)


def read_us_bituminous():
    """Средняя цена битуминозного угля на шахте в США, $/т (метрич.), 1949+."""
    t = pd.read_html(RAW / 'eia_aer_0709.html')[0]
    rows = {}
    for _, r in t.iterrows():
        y = str(r.iloc[0])[:4]
        if y.isdigit():
            try:
                rows[int(y)] = float(r.iloc[1])
            except ValueError:
                pass
    api = json.loads((RAW / 'eia_price_bit.json').read_text())['response']['data']
    for x in api:  # данные API (2001+) свежее и с ревизиями — они главнее
        if x.get('coalRankId') == 'BIT' and x.get('price') not in (None, ''):
            rows[int(x['period'])] = float(x['price'])
    s = pd.Series(rows).sort_index()
    return s / SHORT_TON


def read_us_exports(rank):
    """Средняя цена экспорта угля из США (FAS), $/т (метрич.), поквартально с 2000."""
    data = json.loads((RAW / f'eia_exports_{rank}.json').read_text())['response']['data']
    s = {}
    for x in data:
        if x.get('price') in (None, ''):
            continue
        y, q = x['period'].split('-Q')
        s[pd.Timestamp(int(y), (int(q) - 1) * 3 + 1, 1)] = float(x['price']) / SHORT_TON
    return pd.Series(s).sort_index()


# ---------- аналитика ----------

def zigzag(series, thr=THRESHOLD):
    """Подтверждённые точки разворота: цена ушла от экстремума на thr в логарифме.

    Возвращает список (дата, 'P'|'T', цена) и незавершённый экстремум текущей фазы.
    Первая точка — начало данных, это не настоящий разворот.
    """
    s = series.dropna()
    v = np.log(s.values)
    idx = s.index
    pts = []
    trend, ext = 0, 0
    lo, hi = 0, 0
    for i in range(1, len(v)):
        if trend == 0:
            if v[i] < v[lo]:
                lo = i
            if v[i] > v[hi]:
                hi = i
            if v[i] - v[lo] >= thr:
                pts.append((lo, 'T')); trend, ext = 1, i
            elif v[hi] - v[i] >= thr:
                pts.append((hi, 'P')); trend, ext = -1, i
        elif trend == 1:
            if v[i] > v[ext]:
                ext = i
            elif v[ext] - v[i] >= thr:
                pts.append((ext, 'P')); trend, ext = -1, i
        else:
            if v[i] < v[ext]:
                ext = i
            elif v[i] - v[ext] >= thr:
                pts.append((ext, 'T')); trend, ext = 1, i
    out = [(idx[i], k, float(s.iloc[i])) for i, k in pts]
    out = drop_short(out)
    pending = (idx[ext], 'P' if trend == 1 else 'T', float(s.iloc[ext])) if trend else None
    return out, pending


def drop_short(pts, min_months=MIN_PHASE_MONTHS):
    """Убирает внутренние фазы короче min_months (всплески вроде ЮАР 10–11.2021).

    Из двух соседних точек одного типа остаётся более экстремальная, чередование P/T сохраняется.
    """
    pts = list(pts)
    while True:
        short = [i for i in range(1, len(pts) - 2) if months_between(pts[i][0], pts[i + 1][0]) < min_months]
        if not short:
            return pts
        i = min(short, key=lambda j: months_between(pts[j][0], pts[j + 1][0]))
        a, c = pts[i], pts[i + 2]
        a_wins = a[2] > c[2] if a[1] == 'P' else a[2] < c[2]
        if a_wins:
            del pts[i + 1:i + 3]
        else:
            del pts[i:i + 2]


def months_between(a, b):
    return (b.year - a.year) * 12 + (b.month - a.month)


def phases_from(series, freq_months=1):
    pts, pending = zigzag(series)
    s = series.dropna()
    phases = []
    for (d0, k0, p0), (d1, k1, p1) in zip(pts, pts[1:]):
        phases.append(dict(start=d0, end=d1, kind='up' if k0 == 'T' else 'down',
                           p0=p0, p1=p1, months=months_between(d0, d1),
                           change=p1 / p0 - 1, complete=True, data_start=(d0 == s.index[0])))
    if pts:
        d0, k0, p0 = pts[-1]
        last_d, last_p = s.index[-1], float(s.iloc[-1])
        kind = 'up' if k0 == 'T' else 'down'
        ph = dict(start=d0, end=last_d, kind=kind, p0=p0, p1=last_p,
                  months=months_between(d0, last_d), change=last_p / p0 - 1,
                  complete=False, data_start=(d0 == s.index[0]))
        if pending:
            pd_, pk, pp = pending
            ph['extreme_date'] = pd_
            ph['extreme_price'] = pp
            ph['extreme_change'] = pp / p0 - 1
            ph['since_extreme'] = last_p / pp - 1
            # уровень, при котором подтвердится разворот
            ph['confirm_level'] = pp * (np.exp(THRESHOLD) if kind == 'down' else np.exp(-THRESHOLD))
        phases.append(ph)
    return phases


def phase_stats(phases):
    done = [p for p in phases if p['complete'] and not p['data_start']]
    res = {}
    for kind in ('up', 'down'):
        xs = [p for p in done if p['kind'] == kind]
        if xs:
            res[kind] = dict(n=len(xs),
                             median_months=float(np.median([p['months'] for p in xs])),
                             median_change=float(np.median([p['change'] for p in xs])))
    return res


def position(real, phases):
    """Где цена сейчас: перцентиль к истории и фаза цикла."""
    s = real.dropna()
    hist = s[s.index >= PCT_FROM]
    recent = s[s.index >= s.index[-1] - pd.DateOffset(years=PCT_RECENT_YEARS)]
    last = float(s.iloc[-1])
    cur = phases[-1] if phases else None
    stats = phase_stats(phases)
    yago = s[s.index <= s.index[-1] - pd.DateOffset(months=12)]
    pct_all = float((hist < last).mean() * 100)
    st, st_label = phase_label(cur, stats)
    sd, sd_label = stance(st, pct_all)
    return dict(
        stage=st, stage_label=st_label, stance=sd, stance_label=sd_label, level=level_label(pct_all),
        last=last,
        last_date=s.index[-1],
        pct_all=pct_all,
        pct_recent=float((recent < last).mean() * 100),
        median_all=float(hist.median()),
        hist_from=hist.index[0],
        chg12=float(last / yago.iloc[-1] - 1) if len(yago) else None,
        phase=cur,
        stats=stats,
    )


def phase_label(ph, stats):
    """Стадия текущей фазы относительно типичной (медианной) фазы этого же товара."""
    if not ph:
        return 'none', 'Нет данных'
    med = stats.get(ph['kind'], {})
    since = ph.get('since_extreme', 0.0)
    if ph['kind'] == 'down':
        if since >= 0.2:
            return 'rebound', 'Отскок со дна'
        mature = med and (ph['months'] >= med['median_months'] or ph['change'] <= med['median_change'])
        return ('late_down', 'Зрелый спад') if mature else ('down', 'Спад')
    if since <= -0.2:
        return 'pullback', 'Откат от пика'
    mature = med and (ph['months'] >= med['median_months'] or ph['change'] >= med['median_change'])
    return ('late_up', 'Зрелый рост') if mature else ('up', 'Ранний рост')


def stance(stage, pct):
    """Эвристика для сделок: стадия цикла × уровень цены в истории. Не прогноз."""
    if stage in ('late_down', 'rebound') and pct <= 60:
        return 'buy', 'Окно покупки'
    if stage == 'up' and pct <= 50:
        return 'buy', 'Окно покупки'
    if stage in ('late_up', 'pullback') and pct >= 67:
        return 'sell', 'Окно продажи'
    return 'wait', 'Наблюдать'


def level_label(pct):
    if pct <= 33:
        return 'Дёшево'
    if pct >= 67:
        return 'Дорого'
    return 'Средне'


# ---------- сериализация ----------

def ym(d):
    return d.strftime('%Y-%m')


def rnd(x, n=2):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    return round(float(x), n)


def ser_phase(p):
    out = dict(start=ym(p['start']), end=ym(p['end']), kind=p['kind'], p0=rnd(p['p0'], 1),
               p1=rnd(p['p1'], 1), months=int(p['months']), change=rnd(p['change'], 4),
               complete=p['complete'], data_start=p['data_start'])
    for k in ('extreme_price', 'extreme_change', 'since_extreme', 'confirm_level'):
        if k in p:
            out[k] = rnd(p[k], 4 if 'change' in k or 'since' in k else 1)
    if 'extreme_date' in p:
        out['extreme_date'] = ym(p['extreme_date'])
    return out


def ser_position(pos):
    return dict(stage=pos['stage'], stage_label=pos['stage_label'], stance=pos['stance'],
                stance_label=pos['stance_label'], level=pos['level'],
                last=rnd(pos['last'], 2), last_date=ym(pos['last_date']), pct_all=rnd(pos['pct_all'], 1),
                pct_recent=rnd(pos['pct_recent'], 1), median_all=rnd(pos['median_all'], 2),
                hist_from=ym(pos['hist_from']), chg12=rnd(pos['chg12'], 4),
                phase=ser_phase(pos['phase']) if pos['phase'] else None,
                stats={k: dict(n=v['n'], median_months=rnd(v['median_months'], 1),
                               median_change=rnd(v['median_change'], 4)) for k, v in pos['stats'].items()})


def build():
    pink, wb_updated = read_pink_sheet()
    cpi = read_cpi()
    base_date = cpi.index[-1]
    base = float(cpi.iloc[-1])
    cpi_m = cpi.reindex(cpi.index.union(pink.index)).ffill()
    defl_m = base / cpi_m.reindex(pink.index)

    real = pink.mul(defl_m, axis=0)

    # квартальные ряды: дефлятор = средний CPI квартала
    cpi_q = cpi.resample('QS').mean()
    cpi_q = cpi_q.reindex(cpi_q.index.union(pd.date_range('2000-01-01', pink.index[-1], freq='QS'))).ffill()
    met_n = read_us_exports('MET')
    stm_n = read_us_exports('STM')
    met_r = met_n * (base / cpi_q.reindex(met_n.index))
    stm_r = stm_n * (base / cpi_q.reindex(stm_n.index))

    cpi_a = cpi.groupby(cpi.index.year).mean()
    bit_n = read_us_bituminous()
    bit_r = bit_n * (base / cpi_a.reindex(bit_n.index))

    # ---- уголь: детально ----
    au_n = pink['Coal, Australian'].dropna()
    au_r = real['Coal, Australian'].dropna()
    za_r = real['Coal, South African **'].dropna()
    za_n = pink['Coal, South African **'].dropna()

    coal = {}
    for key, rs, ns, freq in (('thermal_au', au_r, au_n, 'M'), ('thermal_za', za_r, za_n, 'M'),
                              ('met_us', met_r, met_n, 'Q'), ('steam_us', stm_r, stm_n, 'Q')):
        ph = phases_from(rs)
        coal[key] = dict(
            freq=freq,
            series=[[ym(d), rnd(ns.loc[d], 1), rnd(rs.loc[d], 1)] for d in rs.index],
            phases=[ser_phase(p) for p in ph],
            position=ser_position(position(rs, ph)),
        )

    au_annual = au_r.groupby(au_r.index.year).mean()
    au_months = au_r.groupby(au_r.index.year).size()
    long_run = dict(
        us_bit=[[int(y), rnd(bit_n.loc[y], 1), rnd(bit_r.loc[y], 1)] for y in bit_r.index],
        au=[[int(y), rnd(v, 1), int(au_months.loc[y])] for y, v in au_annual.items()],
        us_bit_ma10=[[int(y), rnd(v, 1)] for y, v in bit_r.rolling(10).mean().dropna().items()],
        au_ma10=[[int(y), rnd(v, 1)] for y, v in au_annual.rolling(10).mean().dropna().items()],
    )

    # ---- карта коммодити ----
    cmap = []
    for key, col, name, group, unit in COMMODITIES:
        rs = real[col].dropna()
        ph = phases_from(rs)
        pos = position(rs, ph)
        annual = rs[rs.index >= PCT_FROM].groupby(rs[rs.index >= PCT_FROM].index.year).mean()
        cmap.append(dict(key=key, name=name, group=group, unit=unit, nominal=rnd(pink[col].dropna().iloc[-1], 2),
                         spark=[[int(y), rnd(v, 3)] for y, v in annual.items()],
                         cycles=sum(1 for p in ph if p['complete'] and not p['data_start']),
                         **ser_position(pos)))
    # коксующийся уголь (прокси) — отдельной строкой, история с 2000 года
    ph = phases_from(met_r)
    pos = position(met_r, ph)
    cmap.insert(2, dict(key='met_us', name='Уголь коксующийся (экспорт США)', group='Уголь', unit='$/т',
                        nominal=rnd(met_n.iloc[-1], 1), short_history=True,
                        spark=[[int(y), rnd(v, 3)] for y, v in met_r.groupby(met_r.index.year).mean().items()],
                        cycles=sum(1 for p in ph if p['complete'] and not p['data_start']),
                        **ser_position(pos)))

    events = json.loads((ROOT / 'data' / 'events.json').read_text(encoding='utf-8'))

    payload = dict(
        meta=dict(
            built=pd.Timestamp.now().strftime('%Y-%m-%d'),
            wb_updated=wb_updated,
            last_month=ym(pink.index[-1]),
            cpi_base=ym(base_date),
            threshold=THRESHOLD,
            up_pct=rnd(np.exp(THRESHOLD) - 1, 3),
            down_pct=rnd(np.exp(-THRESHOLD) - 1, 3),
            pct_from=PCT_FROM[:4],
            recent_years=PCT_RECENT_YEARS,
        ),
        coal=coal,
        long_run=long_run,
        commodities=cmap,
        events=events,
    )

    # ---- выгрузки ----
    OUT.mkdir(exist_ok=True)
    pd.DataFrame({'nominal_usd_t': au_n, 'real_usd_t': au_r}).to_csv(OUT / 'coal_newcastle_monthly.csv', index_label='month')
    pd.DataFrame({'met_nominal_usd_t': met_n, 'met_real_usd_t': met_r, 'steam_nominal_usd_t': stm_n,
                  'steam_real_usd_t': stm_r}).to_csv(OUT / 'coal_us_exports_quarterly.csv', index_label='quarter')
    pd.DataFrame({'nominal_usd_t': bit_n, 'real_usd_t': bit_r}).to_csv(OUT / 'coal_us_bituminous_annual.csv', index_label='year')
    rows = []
    for key, c in coal.items():
        for p in c['phases']:
            rows.append(dict(series=key, **{k: v for k, v in p.items()}))
    pd.DataFrame(rows).to_csv(OUT / 'coal_cycles.csv', index=False)
    pd.DataFrame([{k: v for k, v in c.items() if k not in ('spark', 'phase', 'stats')} for c in cmap]).to_csv(
        OUT / 'commodity_map.csv', index=False)
    (OUT / 'dashboard.json').write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')

    tpl = (DASH / 'template.html').read_text(encoding='utf-8')
    blob = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    html = tpl.replace('/*__DATA__*/null', blob)
    for name in ('research', 'method'):
        frag = DASH / f'{name}.html'
        if frag.exists():
            html = html.replace(f'<!--__{name.upper()}__-->', frag.read_text(encoding='utf-8'))
    (DASH / 'coal-cycles.html').write_text(html, encoding='utf-8')
    print('OK:', DASH / 'coal-cycles.html', f'{len(blob) / 1024:.0f} KB data')
    return payload


if __name__ == '__main__':
    if '--offline' not in sys.argv:
        download_all()
    p = build()
    c = p['coal']['thermal_au']['position']
    print('Newcastle:', c['last'], 'pct', c['pct_all'], 'phase', c['phase']['kind'], c['phase']['start'])
