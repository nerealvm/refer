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
# Д. Джекс, реальные цены коммодити 1850–2020 (1900 = 100). Страница SFU недоступна, берём копию из Web Archive.
JACKS_URL = ('https://web.archive.org/web/2022id_/http://www.sfu.ca/~djacks/data/boombust/'  # id_ = сырой файл без обёртки Wayback
             'Real%20commodity%20prices,%201850-2020.xlsx')
SHORT_TON = 0.90718474  # метрических тонн в короткой тонне

THRESHOLD = 0.4  # разворот на 0.4 в логарифме: +49% от дна или −33% от пика
MIN_PHASE_MONTHS = 6  # внутренняя фаза короче полугода считается шумом
PCT_FROM = '1970-01-01'
PCT_RECENT_YEARS = 20

COMMODITIES = [
    # ключ, колонка Pink Sheet, название, группа, единица
    ('coal_au', 'Coal, Australian', 'Уголь Newcastle (Австралия)', 'Уголь', '$/т'),
    ('coal_za', 'Coal, South African **', 'Уголь Richards Bay (ЮАР)', 'Уголь', '$/т'),
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


# Фундаментальная связь с углём: знак и механизм. Задано вручную, обоснование повторено в методологии.
# +1 — есть механизм, по которому ряд и уголь должны двигаться вместе; 0 — прямого механизма нет.
FUND_LINKS = {
    'coal_za': (1, 'Тот же морской рынок энергетического угля: арбитраж между Атлантикой и Азией.'),
    'brent': (1, 'Общий энергетический цикл: нефть задаёт цену контрактов на СПГ, издержки добычи и фрахта.'),
    'gas_eu': (1, 'Замещение в генерации: дорогой газ переключает электростанции Европы на уголь, дешёвый — обратно.'),
    'lng_jp': (1, 'Замещение в генерации Азии: СПГ и уголь конкурируют за место в энергобалансе.'),
    'gas_us': (0, 'Неоднозначно: дешёвый сланцевый газ вытесняет уголь внутри США, но выталкивает американский уголь на экспорт.'),
    'iron_ore': (1, 'Сталь: спрос на коксующийся уголь идёт вместе со спросом на руду.'),
    'copper': (1, 'Промышленный цикл Китая: один и тот же спрос на металлы и электроэнергию.'),
    'aluminum': (1, 'Энергоёмкий металл: выплавка в Китае и Индии идёт на угольной генерации.'),
    'nickel': (1, 'Промышленный цикл; никелевая выплавка Индонезии работает на угле.'),
    'zinc': (1, 'Промышленный цикл Китая.'),
    'lead': (1, 'Промышленный цикл Китая.'),
    'tin': (1, 'Промышленный цикл и электроника.'),
    'gold': (0, 'Защитный актив: связь с углём только через доллар и инфляцию.'),
    'silver': (0, 'Защитный актив и промышленный металл: прямого механизма нет.'),
    'platinum': (0, 'Автокатализаторы и добыча в ЮАР: прямого механизма нет.'),
    'potash': (0, 'Удобрения: общий сырьевой цикл, прямого механизма нет.'),
    'phosrock': (0, 'Удобрения: общий сырьевой цикл, прямого механизма нет.'),
    'urea': (1, 'Карбамид в Китае делают из угля: уголь — сырьё и издержки производителя.'),
}


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
    if not (RAW / 'jacks.xlsx').exists():  # исторический файл не меняется
        fetch(JACKS_URL, RAW / 'jacks.xlsx')
    key = os.environ.get('EIA_API_KEY')
    if not key:
        print('EIA_API_KEY не задан: ряды EIA берутся из data/*.csv прошлой сборки')
        return
    try:
        fetch(f'{EIA_API}/price-by-rank/data/?api_key={key}&frequency=annual&data[]=price'
              f'&facets[stateRegionId][]=US&facets[coalRankId][]=BIT&length=500', RAW / 'eia_price_bit.json')
        for rank in ('MET', 'STM'):
            fetch(f'{EIA_API}/exports-imports-quantity-price/data/?api_key={key}&frequency=quarterly'
                  f'&data[]=price&data[]=quantity&facets[exportImportType][]=Exports&facets[countryId][]=TOT'
                  f'&facets[customsDistrictId][]=TOT&facets[coalRankId][]={rank}&length=1000',
                  RAW / f'eia_exports_{rank}.json', tries=5)
    except Exception as e:  # API EIA недоступен: ниже сработает запасной путь через data/*.csv
        print(f'EIA API недоступен ({e}): ряды EIA берутся из data/*.csv прошлой сборки')


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
    cpi = pd.to_numeric(c.set_index('date')['cpi'], errors='coerce')
    # внутренние пропуски (октябрь 2025 года в FRED пуст) — лог-линейная интерполяция между соседями
    return np.exp(np.log(cpi).interpolate(limit_area='inside')).dropna()


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
    api_file = RAW / 'eia_price_bit.json'
    if api_file.exists():
        api = json.loads(api_file.read_text())['response']['data']
        for x in api:  # данные API (2001+) свежее и с ревизиями — они главнее
            if x.get('coalRankId') == 'BIT' and x.get('price') not in (None, ''):
                rows[int(x['period'])] = float(x['price'])
        return pd.Series(rows).sort_index() / SHORT_TON
    # запасной путь: номинальный ряд прошлой сборки (уже в метрических тоннах) поверх таблицы AER
    prev = pd.read_csv(OUT / 'coal_us_bituminous_annual.csv', index_col='year')['nominal_usd_t']
    s = pd.Series(rows).sort_index() / SHORT_TON
    return prev.combine_first(s).sort_index()


def read_jacks_coal():
    """Реальный индекс цены угля Д. Джекса, 1850–2020, 1900 = 100."""
    d = pd.read_excel(RAW / 'jacks.xlsx', sheet_name='Commodities', header=None)
    col = [i for i, v in enumerate(d.iloc[1]) if v == 'Coal'][0]
    rows = d.iloc[2:, [0, col]].dropna()
    rows = rows[rows.iloc[:, 0].astype(str).str.fullmatch(r'\d{4}')]
    return pd.Series(rows.iloc[:, 1].astype(float).values, index=rows.iloc[:, 0].astype(int).values)


def read_oce_annual():
    """OCE Resources and Energy Quarterly, historical data, таблица 6: годовые индикаторные цены, номинал $/т.
    Сайт OCE рвёт соединение скриптам, файл кладётся в data/raw/oce_historical.xlsx вручную; без него ряд пропускается."""
    f = RAW / 'oce_historical.xlsx'
    if not f.exists():
        return None
    d = pd.read_excel(f, sheet_name='6', header=None)
    is_year = lambda v: isinstance(v, (int, float)) and not pd.isna(v) and 1900 < v < 2100 and float(v).is_integer()
    hdr = next(i for i in range(len(d)) if sum(is_year(v) for v in d.iloc[i].tolist()) >= 10)  # строка с годами
    years = [(i, int(v)) for i, v in enumerate(d.iloc[hdr].tolist()) if is_year(v)]
    lab_col = next(c for c in range(d.shape[1]) if d.iloc[:, c].astype(str).str.contains('Metallurgical coal').any())
    labels = d.iloc[:, lab_col].astype(str).str.strip().str.lower()

    def row(prefix, nth=0):
        r = d[labels.str.startswith(prefix)].iloc[nth]
        return pd.Series({y: float(r.iloc[i]) for i, y in years if isinstance(r.iloc[i], (int, float)) and not pd.isna(r.iloc[i])})
    return dict(met_spot=row('metallurgical coal'), thermal_contract=row('thermal coal', 0), thermal_spot=row('thermal coal', 1))


def read_us_exports(rank):
    """Средняя цена экспорта угля из США (FAS), $/т (метрич.), поквартально с 2000."""
    api_file = RAW / f'eia_exports_{rank}.json'
    if not api_file.exists():  # запасной путь: номинальный ряд прошлой сборки
        prev = pd.read_csv(OUT / 'coal_us_exports_quarterly.csv', index_col='quarter')
        col = {'MET': 'met_nominal_usd_t', 'STM': 'steam_nominal_usd_t'}[rank]
        idx = [pd.Timestamp(int(q[:4]), (int(q[-1]) - 1) * 3 + 1, 1) for q in prev.index]
        return pd.Series(prev[col].values, index=idx).dropna().sort_index()
    data = json.loads(api_file.read_text())['response']['data']
    s = {}
    for x in data:
        if x.get('price') in (None, ''):
            continue
        y, q = x['period'].split('-Q')
        s[pd.Timestamp(int(y), (int(q) - 1) * 3 + 1, 1)] = float(x['price']) / SHORT_TON
    return pd.Series(s).sort_index()


# ---------- аналитика ----------

def despike(series, thr=THRESHOLD):
    """Убирает одиночные выбросы: точка отличается от обоих соседей больше чем на thr в логарифме
    в одну сторону (скачок и возврат). Значение заменяется средним геометрическим соседей.
    Только для датировки циклов; на графиках остаются исходные цены."""
    s = series.dropna().copy()
    lv = np.log(s.values)
    fixed = []
    for i in range(1, len(lv) - 1):
        a, b = lv[i] - lv[i - 1], lv[i] - lv[i + 1]
        if a * b > 0 and min(abs(a), abs(b)) > thr:
            new = (lv[i - 1] + lv[i + 1]) / 2
            fixed.append((s.index[i], float(s.iloc[i]), float(np.exp(new))))
            lv[i] = new
    return pd.Series(np.exp(lv), index=s.index), fixed


def zigzag(series, thr=THRESHOLD):
    """Подтверждённые точки разворота: цена ушла от экстремума на thr в логарифме.

    Возвращает список (дата, 'P'|'T', цена) и незавершённый экстремум текущей фазы.
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
                pts.append([lo, 'T']); trend, ext = 1, i
            elif v[hi] - v[i] >= thr:
                pts.append([hi, 'P']); trend, ext = -1, i
        elif trend == 1:
            if v[i] > v[ext]:
                ext = i
            elif v[ext] - v[i] >= thr:
                pts.append([ext, 'P']); trend, ext = -1, i
        else:
            if v[i] < v[ext]:
                ext = i
            elif v[i] - v[ext] >= thr:
                pts.append([ext, 'T']); trend, ext = 1, i
    end = ext if trend else len(v) - 1
    pts = drop_short(pts, v, idx, end)
    out = [(idx[i], k, float(s.iloc[i])) for i, k in pts]
    pending = (idx[ext], 'P' if trend == 1 else 'T', float(s.iloc[ext])) if trend else None
    return out, pending


def snap(pts, v, end):
    """Каждая точка — настоящий экстремум между соседними точками (пик — максимум, дно — минимум)."""
    for j, (i, k) in enumerate(pts):
        lo = pts[j - 1][0] + 1 if j else 0
        hi = pts[j + 1][0] - 1 if j + 1 < len(pts) else end
        if hi < lo:
            continue
        seg = v[lo:hi + 1]
        pts[j][0] = lo + int(np.argmax(seg) if k == 'P' else np.argmin(seg))
    return pts


def drop_short(pts, v, idx, end, min_months=MIN_PHASE_MONTHS):
    """Склеивает завершённые фазы короче min_months (всплески вроде ЮАР 10–11.2021).

    Из двух ближайших однотипных точек остаётся более экстремальная, затем все точки
    переносятся на настоящие экстремумы между соседями. Повторяем, пока коротких фаз нет.
    Последняя подтверждённая фаза (перед текущей) не склеивается: за ней нет точки того же типа.
    """
    pts = snap([list(p) for p in pts], v, end)
    for _ in range(len(pts) + 1):
        short = [j for j in range(0, len(pts) - 2) if months_between(idx[pts[j][0]], idx[pts[j + 1][0]]) < min_months]
        if not short:
            break
        j = min(short, key=lambda q: months_between(idx[pts[q][0]], idx[pts[q + 1][0]]))
        a, c = pts[j], pts[j + 2]
        a_wins = v[a[0]] > v[c[0]] if a[1] == 'P' else v[a[0]] < v[c[0]]
        if a_wins:
            del pts[j + 1:j + 3]
        else:
            del pts[j:j + 2]
        pts = snap(pts, v, end)
    return pts


def months_between(a, b):
    return (b.year - a.year) * 12 + (b.month - a.month)


def phases_from(series, outliers=None):
    """Фазы цикла по реальной цене. Цены фаз берутся из исходного ряда, датировка — по ряду без выбросов."""
    s = series.dropna()
    monthly = len(s) > 2 and (s.index[1:] - s.index[:-1]).median().days <= 31
    # выбросы ищем только в помесячных рядах: в годовых и квартальных одна точка — это уже целый период
    clean, fixed = despike(s) if monthly else (s, [])
    if outliers is not None:
        outliers.extend(fixed)
    pts, pending = zigzag(clean)
    pts = [(d, k, float(s.loc[d])) for d, k, _ in pts]
    if pending:
        pending = (pending[0], pending[1], float(s.loc[pending[0]]))
    # фаза, начатая в первые 12 месяцев ряда, обрезана началом данных — в статистику не идёт
    cut = lambda d: months_between(s.index[0], d) < 12
    phases = []
    for (d0, k0, p0), (d1, k1, p1) in zip(pts, pts[1:]):
        phases.append(dict(start=d0, end=d1, kind='up' if k0 == 'T' else 'down',
                           p0=p0, p1=p1, months=months_between(d0, d1),
                           change=p1 / p0 - 1, complete=True, data_start=cut(d0)))
    if pts:
        d0, k0, p0 = pts[-1]
        last_d, last_p = s.index[-1], float(s.iloc[-1])
        kind = 'up' if k0 == 'T' else 'down'
        ph = dict(start=d0, end=last_d, kind=kind, p0=p0, p1=last_p,
                  months=months_between(d0, last_d), change=last_p / p0 - 1,
                  complete=False, data_start=cut(d0))
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


def coal_links(real):
    """Связь Newcastle с другими рядами: корреляция 12-месячных логарифмических изменений реальных цен.
    Лаг: x.shift(k) при k>0 ставит прошлое значение x против текущего угля, то есть x опережает уголь на k месяцев."""
    lr = np.log(real.where(real > 0)).diff(12)
    base = lr['Coal, Australian']
    out = []
    for key, col, name, group, unit in COMMODITIES:
        if col == 'Coal, Australian':
            continue
        x = lr[col]
        df = pd.concat([base.rename('c'), x.rename('x')], axis=1).dropna()
        df = df[df.index >= PCT_FROM]
        if len(df) < 60:
            continue
        c_all = float(df.c.corr(df.x))
        d10 = df[df.index > df.index[-1] - pd.DateOffset(years=10)]
        c_10 = float(d10.c.corr(d10.x))
        best_k, best_c = 0, c_all
        for k in range(-12, 13):
            if k == 0:
                continue
            cc = float(df.c.corr(x.shift(k).reindex(df.index)))
            if abs(cc) > abs(best_c) + 0.03 and abs(cc) >= 0.3:  # лаг засчитываем, если он заметно лучше нуля и сама связь не шум
                best_k, best_c = k, cc
        roll = df.c.rolling(60).corr(df.x)
        roll_y = roll.groupby(roll.index.year).mean().dropna()
        sign, why = FUND_LINKS.get(key, (0, ''))
        out.append(dict(key=key, name=name, group=group, unit=unit, from_year=int(df.index[0].year), n=len(df),
                        corr_all=rnd(c_all, 2), corr_10y=rnd(c_10, 2), lead=best_k, corr_lead=rnd(best_c, 2),
                        fund=sign, why=why, roll=[[int(y), rnd(v, 2)] for y, v in roll_y.items()]))
    return out


def qid(d):
    """'ГГГГ-ММ' → 'ГГГГ-Qn'."""
    y, m = int(d[:4]), int(d[5:7])
    return f'{y}-Q{(m - 1) // 3 + 1}'


def months_ym(a, b):
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


def turns_from(key, c):
    """Развороты ряда: концы завершённых фаз, неподтверждённый экстремум текущей фазы и начало ряда.
    id разворота — 'ГГГГ-ММ' для месячных рядов и 'ГГГГ-Qn' для квартальных; к нему привязываются события."""
    q = c['freq'] == 'Q'
    tid = (lambda d: qid(d)) if q else (lambda d: d)
    phs = c['phases']
    out = []
    for i, p in enumerate(phs):
        if not p['complete']:
            continue
        nxt = phs[i + 1] if i + 1 < len(phs) else None
        out.append(dict(id=tid(p['end']), series=key, date=p['end'], kind='peak' if p['kind'] == 'up' else 'trough',
                        price=p['p1'], unconfirmed=False,
                        phase_in=dict(kind=p['kind'], change=p['change'], months=p['months']),
                        phase_out=dict(kind=nxt['kind'], change=nxt['change'], months=nxt['months'], complete=nxt['complete']) if nxt else None))
    cur = phs[-1]
    if not cur['complete'] and cur.get('extreme_date'):
        out.append(dict(id=tid(cur['extreme_date']), series=key, date=cur['extreme_date'],
                        kind='trough' if cur['kind'] == 'down' else 'peak', price=cur['extreme_price'], unconfirmed=True,
                        phase_in=dict(kind=cur['kind'], change=cur['extreme_change'], months=months_ym(cur['start'], cur['extreme_date'])),
                        phase_out=dict(kind='up' if cur['kind'] == 'down' else 'down', change=cur['since_extreme'],
                                       months=months_ym(cur['extreme_date'], cur['end']), complete=False)))
    first = phs[0]
    out.append(dict(id=tid(first['start']), series=key, date=first['start'], kind='start', price=first['p0'], unconfirmed=False,
                    phase_in=None, phase_out=dict(kind=first['kind'], change=first['change'], months=first['months'], complete=first['complete'])))
    return out


def check_links(events, turns, coal):
    """Правило привязки события к развороту, проверяется кодом:
    - «драйвер движения»: событие лежит внутри фазы, ведущей к развороту, и его знак совпадает с направлением этой фазы;
    - «драйвер разворота»: событие в окне от 6 месяцев до разворота до 12 месяцев после него, знак совпадает с направлением следующей фазы.
    Иначе привязка снимается, событие становится фоновым (linked_turn_rejected хранит исходную привязку)."""
    tmap = {(t['series'], t['id']): t for t in turns}
    for ev in events:
        lt = ev.get('linked_turn')
        if not lt:
            ev['link_role'] = {}
            continue
        roles = {}
        for k in ev.get('turn_series', []):
            t = tmap[(k, lt)]
            m = months_ym(ev['date'], t['date'])  # > 0: событие раньше разворота
            up = ev['expected_direction'] > 0
            pin, pout = t.get('phase_in'), t.get('phase_out')
            role = None
            if pin and 0 <= m <= pin['months'] and (pin['kind'] == 'up') == up:
                role = 'to_turn'
            elif pout and -12 <= m <= 6 and (pout['kind'] == 'up') == up:
                role = 'turn'
            roles[k] = role
        ev['link_role'] = roles
        if not any(roles.values()):
            print(f"  событие {ev['date']} «{ev['label']}» не согласуется с разворотом {lt} по правилу привязки: переведено в фоновые")
            ev['linked_turn_rejected'] = lt
            ev['linked_turn'] = None
            ev['turn_series'] = []


def event_impact(ev, series_real):
    """Фактический сдвиг реальной цены после события: лог-изменение через 3, 6 и 12 месяцев
    (для квартального ряда — 1, 2 и 4 квартала). Считается по данным, не задаётся руками."""
    out = {}
    t = pd.Timestamp(ev['date'] + '-01')
    for key in ev.get('series', []):
        s, q = series_real.get(key, (None, False))
        if s is None:
            continue
        s = s.dropna()
        start = s[s.index >= t]
        if start.empty:
            continue
        i0 = s.index.get_loc(start.index[0])
        p0 = float(s.iloc[i0])
        res = {}
        for lab, steps in (('m3', 1 if q else 3), ('m6', 2 if q else 6), ('m12', 4 if q else 12)):
            res[lab] = rnd(float(np.log(s.iloc[i0 + steps] / p0)), 3) if i0 + steps < len(s) else None
        res['at'] = ym(start.index[0])
        out[key] = res
    return out


def event_type_stats(events, real_series, key='thermal_au'):
    """Рейтинг типов событий по ряду key. Для каждого события берётся лог-изменение реальной цены через 3/6/12 месяцев,
    из него вычитается медианный дрейф ряда за тот же горизонт (фон без привязки к событиям), результат умножается на
    ожидаемое направление («в ожидаемую сторону»). По типу считаются медиана этого знакового сдвига, медиана модуля сдвига
    и доля совпадений знака через 12 месяцев. События одного типа в одном месяце считаются один раз."""
    s = real_series.dropna()
    lg = np.log(s)
    drift = {h: float(lg.diff(n).dropna().median()) for h, n in (('m3', 3), ('m6', 6), ('m12', 12))}
    base = {('med_abs' + h[1:]): rnd(float(lg.diff(n).abs().dropna().median()), 3) for h, n in (('m3', 3), ('m6', 6), ('m12', 12))}
    base.update({('drift' + h[1:]): rnd(v, 3) for h, v in drift.items()})
    rows = {}
    seen = set()
    all_hit, all_n = 0, 0
    for ev in events:
        im = (ev.get('impact') or {}).get(key)
        if not im:
            continue
        dk = (ev['date'], ev['type'])
        if dk in seen:
            continue
        seen.add(dk)
        d = ev['expected_direction']
        r = rows.setdefault(ev['type'], dict(type=ev['type'], n=0, ids=[], m3=[], m6=[], m12=[], a3=[], a6=[], a12=[], hit12=0, n12=0))
        r['n'] += 1
        r['ids'].append(ev['n'])
        for h in ('m3', 'm6', 'm12'):
            if im.get(h) is not None:
                adj = im[h] - drift[h]
                r[h].append(adj * d)
                r['a' + h[1:]].append(abs(adj))
        if im.get('m12') is not None:
            r['n12'] += 1
            ok = int(np.sign(im['m12'] - drift['m12']) == d)
            r['hit12'] += ok
            all_hit += ok
            all_n += 1
    out = []
    for r in rows.values():
        med = lambda v: rnd(float(np.median(v)), 3) if v else None
        out.append(dict(type=r['type'], n=r['n'], ids=r['ids'], n12=r['n12'], med_m3=med(r['m3']), med_m6=med(r['m6']), med_m12=med(r['m12']),
                        med_abs3=med(r['a3']), med_abs6=med(r['a6']), med_abs12=med(r['a12']),
                        hit12=rnd(r['hit12'] / r['n12'], 2) if r['n12'] else None))
    out.sort(key=lambda r: -(r['med_m12'] if r['med_m12'] is not None else -9))
    return dict(types=out, baseline=base, all=dict(n=sum(r['n'] for r in out), hit12=rnd(all_hit / all_n, 2) if all_n else None),
                from_=None)


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

    outliers = []
    coal = {}
    for key, rs, ns, freq in (('thermal_au', au_r, au_n, 'M'), ('thermal_za', za_r, za_n, 'M'),
                              ('met_us', met_r, met_n, 'Q'), ('steam_us', stm_r, stm_n, 'Q')):
        ph = phases_from(rs)  # выбросы угля попадают в список через карту коммодити
        coal[key] = dict(
            freq=freq,
            series=[[ym(d), rnd(ns.loc[d], 1), rnd(rs.loc[d], 1)] for d in rs.index],
            phases=[ser_phase(p) for p in ph],
            position=ser_position(position(rs, ph)),
        )

    # годовой спот PHCC из OCE — ориентир для сглаженного прокси США; без файла пропускается
    oce = read_oce_annual()
    if oce is not None:
        phcc_n = oce['met_spot']
        phcc_r = phcc_n * (base / cpi_a.reindex(phcc_n.index))
        coal['met_phcc'] = dict(freq='A', series=[[f'{int(y)}-07', rnd(phcc_n.loc[y], 1), rnd(phcc_r.loc[y], 1)] for y in phcc_n.index],
                                phases=[], position=None,
                                source='OCE Resources and Energy Quarterly, September 2026, historical data, table 6: premium hard coking coal spot')
    else:
        print('data/raw/oce_historical.xlsx нет: ряд PHCC пропущен')

    au_annual = au_r.groupby(au_r.index.year).mean()
    au_months = au_r.groupby(au_r.index.year).size()
    # 170 лет: индекс Джекса, после 2020 продлён по годовой реальной цене Newcastle.
    # Коэффициент связки — среднее отношение индекса к Newcastle за 2010–2020, а не один 2020 год.
    jk = read_jacks_coal()
    last_y = int(jk.index[-1])
    link_years = [y for y in range(2010, last_y + 1) if y in au_annual.index]
    link = float(np.mean([jk.loc[y] / au_annual.loc[y] for y in link_years]))
    ext = {y: au_annual.loc[y] * link for y in au_annual.index if y > last_y}
    jk_all = pd.concat([jk, pd.Series(ext)])
    jk_ts = pd.Series(jk_all.values, index=pd.to_datetime([f'{y}-07-01' for y in jk_all.index]))
    jk_phases = phases_from(jk_ts)
    long_run = dict(
        jacks=[[int(y), rnd(v, 1), int(y > last_y)] for y, v in jk_all.items()],
        jacks_phases=[dict(start=int(p['start'].year), end=int(p['end'].year), kind=p['kind'],
                           change=rnd(p['change'], 4), complete=p['complete']) for p in jk_phases],
        jacks_last_source_year=last_y,
        jacks_link_years=[link_years[0], link_years[-1]],
        au_partial_months=int(au_months.iloc[-1]),
        us_bit=[[int(y), rnd(bit_n.loc[y], 1), rnd(bit_r.loc[y], 1)] for y in bit_r.index],
        au=[[int(y), rnd(v, 1), int(au_months.loc[y])] for y, v in au_annual.items()],
        us_bit_ma10=[[int(y), rnd(v, 1)] for y, v in bit_r.rolling(10).mean().dropna().items()],
        au_ma10=[[int(y), rnd(v, 1)] for y, v in au_annual.rolling(10).mean().dropna().items()],
    )

    # ---- карта коммодити ----
    cmap = []
    for key, col, name, group, unit in COMMODITIES:
        rs = real[col].dropna()
        found = []
        ph = phases_from(rs, found)
        outliers.extend((name, d, o, r) for d, o, r in found)
        pos = position(rs, ph)
        annual = rs[rs.index >= PCT_FROM].groupby(rs[rs.index >= PCT_FROM].index.year).mean()
        since = rs[rs.index >= PCT_FROM]
        cmap.append(dict(key=key, name=name, group=group, unit=unit, freq='M', nominal=rnd(pink[col].dropna().iloc[-1], 2),
                         spark=[[int(y), rnd(v, 3)] for y, v in annual.items()],
                         monthly=[[ym(d), rnd(v, 1) if v >= 10 else rnd(v, 3)] for d, v in since.items()],  # реальные, для наложения на график угля
                         cycles=sum(1 for p in ph if p['complete'] and not p['data_start']),
                         **ser_position(pos)))
    # коксующийся уголь (прокси) — отдельной строкой, история с 2000 года
    ph = phases_from(met_r)
    pos = position(met_r, ph)
    cmap.insert(2, dict(key='met_us', name='Уголь коксующийся (экспорт США)', group='Уголь', unit='$/т', freq='Q',
                        nominal=rnd(met_n.iloc[-1], 1), short_history=True,
                        spark=[[int(y), rnd(v, 3)] for y, v in met_r.groupby(met_r.index.year).mean().items()],
                        cycles=sum(1 for p in ph if p['complete'] and not p['data_start']),
                        **ser_position(pos)))

    # ---- слой предложения и спроса (собирается вручную по источникам, data/supply_demand.json) ----
    sd_file = ROOT / 'data' / 'supply_demand.json'
    sd = {x['key']: x for x in json.loads(sd_file.read_text(encoding='utf-8'))} if sd_file.exists() else {}
    for c in cmap:
        x = sd.get(c['key'])
        if not x:
            c['sd'] = None
            continue
        sup, dem = int(x['supply']['score']), int(x['demand']['score'])
        c['sd'] = dict(supply=sup, demand=dem, net=dem - sup, horizon=x.get('horizon'), as_of=x.get('as_of'), confidence=x.get('confidence'),
                       supply_summary=x['supply'].get('summary', ''), demand_summary=x['demand'].get('summary', ''),
                       supply_facts=x['supply'].get('facts', []), demand_facts=x['demand'].get('facts', []), balance_note=x.get('balance_note', ''))
    print('supply/demand:', sum(1 for c in cmap if c['sd']), 'из', len(cmap), 'рядов')

    events = json.loads((ROOT / 'data' / 'events.json').read_text(encoding='utf-8'))
    for ev in events:
        ev.pop('review', None)  # след проверки остаётся в events.json, в дашборд не идёт
    # события еженедельного мониторинга trend-watch: без привязки к развороту, помечены live
    live_file = ROOT / 'data' / 'events_live.json'
    if live_file.exists():
        live = json.loads(live_file.read_text(encoding='utf-8'))
        for ev in live:
            ev.setdefault('linked_turn', None)
            ev['live'] = True
        events.extend(live)
        events.sort(key=lambda e: e['date'])
        print('события мониторинга:', len(live))
    deals = json.loads((ROOT / 'data' / 'deals.json').read_text(encoding='utf-8'))
    # ---- развороты и события ----
    turns = []
    for key in ('thermal_au', 'met_us', 'thermal_za', 'steam_us'):
        turns.extend(turns_from(key, coal[key]))
    turn_ids = {(t['series'], t['id']) for t in turns}
    series_real = {'thermal_au': (au_r, False), 'thermal_za': (za_r, False), 'met_us': (met_r, True), 'steam_us': (stm_r, True)}
    for i, ev in enumerate(events):
        ev['n'] = i + 1
        lt = ev.get('linked_turn')
        ev['turn_series'] = [k for k in ev.get('series', []) if (k, lt) in turn_ids] if lt else []
        if lt and not ev['turn_series']:
            print(f"  событие {ev['date']} «{ev['label']}»: разворот {lt} не найден у рядов {ev.get('series')}")
        ev['impact'] = event_impact(ev, series_real)
    check_links(events, turns, coal)
    event_types = event_type_stats(events, au_r)
    event_types['from'] = events[0]['date'][:4] if events else None
    event_types.pop('from_', None)

    sig_file = ROOT / 'data' / 'signals.json'
    signals = json.loads(sig_file.read_text(encoding='utf-8')) if sig_file.exists() else []
    capex_file = ROOT / 'data' / 'capex.json'
    capex = json.loads(capex_file.read_text(encoding='utf-8')) if capex_file.exists() else []
    for c in capex:
        c.pop('note', None)  # служебные оговорки сборщиков остаются в data/capex.json
    print('сигналы:', len(signals), '· компании с капзатратами:', len(capex))
    # хроника недель: записи пишет рутина trend-watch, цену на дату прогона добавляет сборка
    story_file = ROOT / 'data' / 'story.json'
    story = []
    if story_file.exists():
        try:
            story = json.loads(story_file.read_text(encoding='utf-8'))
            assert isinstance(story, list)
        except Exception as exc:
            print('хроника: файл не читается, пропущена:', exc)
            story = []
    clean = []
    for st in story:
        if not isinstance(st, dict) or not st.get('run'):
            print('хроника: запись без run пропущена:', str(st)[:80])
            continue
        st.pop('note', None)  # служебная пометка остаётся в data/story.json
        st['price'] = None
        # цена на дату прогона: последний ЗАВЕРШЁННЫЙ месяц до месяца прогона (индекс ряда стоит на 1-м числе),
        # чтобы выход нового Pink Sheet не менял цену в старых записях
        done = au_n[au_n.index < pd.Timestamp(st['run']).replace(day=1)]
        if len(done):
            d0, v = done.index[-1], float(done.iloc[-1])
            def _at(months):
                x = au_n.get(d0 - pd.DateOffset(months=months))
                return None if x is None or pd.isna(x) else float(x)
            v1, v12 = _at(1), _at(12)
            st['price'] = dict(date=d0.strftime('%Y-%m'), value=round(v, 1),
                               chg_1m=round(v / v1 - 1, 3) if v1 else None,
                               chg_12m=round(v / v12 - 1, 3) if v12 else None)
        clean.append(st)
    story = clean
    story.sort(key=lambda x: x['run'], reverse=True)
    print('хроника:', len(story), 'недель')

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
            outliers=[dict(series=n, date=ym(d), original=rnd(o, 1), replaced=rnd(r, 1))
                      for n, d, o, r in sorted({(n, d, o, r) for n, d, o, r in outliers}, key=lambda x: (x[0], x[1]))],
        ),
        coal=coal,
        long_run=long_run,
        commodities=cmap,
        links=coal_links(real),
        turns=turns,
        event_types=event_types,
        signals=signals,
        capex=capex,
        events=events,
        deals=deals,
        story=story,
    )

    # ---- выгрузки ----
    OUT.mkdir(exist_ok=True)
    pd.DataFrame({'nominal_usd_t': au_n, 'real_usd_t': au_r}).to_csv(OUT / 'coal_newcastle_monthly.csv', index_label='month')
    qdf = pd.DataFrame({'met_nominal_usd_t': met_n, 'met_real_usd_t': met_r, 'steam_nominal_usd_t': stm_n,
                        'steam_real_usd_t': stm_r})
    qdf.index = [f'{d.year}-Q{(d.month - 1) // 3 + 1}' for d in qdf.index]
    qdf.to_csv(OUT / 'coal_us_exports_quarterly.csv', index_label='quarter')
    pd.DataFrame({'nominal_usd_t': bit_n, 'real_usd_t': bit_r}).to_csv(OUT / 'coal_us_bituminous_annual.csv', index_label='year')
    pd.DataFrame({'real_index_1900_100': jk_all, 'extended_by_newcastle': [int(y > last_y) for y in jk_all.index]}).to_csv(
        OUT / 'coal_jacks_1850_annual.csv', index_label='year')
    rows = []
    for key, c in coal.items():
        for p in c['phases']:
            rows.append(dict(series=key, **{k: v for k, v in p.items()}))
    pd.DataFrame(rows).to_csv(OUT / 'coal_cycles.csv', index=False)
    qlab = lambda d: f'{d[:4]}-Q{(int(d[5:7]) - 1) // 3 + 1}'
    pd.DataFrame([{**{k: v for k, v in c.items() if k not in ('spark', 'phase', 'stats')},
                   **({'last_date': qlab(c['last_date']), 'hist_from': qlab(c['hist_from'])} if c['freq'] == 'Q' else {})}
                  for c in cmap]).to_csv(OUT / 'commodity_map.csv', index=False)
    (OUT / 'dashboard.json').write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')

    tpl = (DASH / 'template.html').read_text(encoding='utf-8')
    blob = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    html = tpl.replace('/*__DATA__*/null', blob)
    for name in ('research', 'method', 'story'):
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
