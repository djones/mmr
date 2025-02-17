from bs4 import BeautifulSoup
from collections import deque
from dateutil.tz import gettz, tzlocal
from dateutil.tz.tz import tzfile
from exchange_calendars import ExchangeCalendar
from ib_insync.contract import Contract
from pandas import Timestamp
from pypager.pager import Pager
from pypager.source import GeneratorSource
from rich.console import Console
from rich.table import Table
from typing import Any, Callable, cast, Dict, Generic, List, Optional, Tuple, TypeVar, Union

import collections
import datetime as dt
import exchange_calendars as ec
import io
import json
import locale
import logging
import numpy as np
import os
import pandas as pd
import plotille as plt
import pytz
import scipy.stats as st
import socket
import tempfile
import warnings


def flatten_dict(d, parent_key='', sep='_'):
    items = []
    for k, v in d.items():
        new_key = parent_key + sep + k if parent_key else k
        if isinstance(v, collections.MutableMapping):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def flatten_json(y):
    out = {}

    def flatten(x, name=''):
        if type(x) is dict:
            for a in x:
                flatten(x[a], name + a + '_')
        elif type(x) is list:
            i = 0
            for a in x:
                flatten(a, name + str(i) + '_')
                i += 1
        else:
            out[name[:-1]] = x

    flatten(y)
    return out


def contract_from_dict(d: Dict[str, Any]) -> Contract:
    contract = Contract(
        conId=d['conId'],
        symbol=d['symbol'],
        exchange=d['exchange'],
        primaryExchange=d['primaryExchange'],
        currency=d['currency']
    )
    return contract


def symbol_to_contract(symbol: str) -> Contract:
    if type(symbol) is int or type(symbol) is np.int_ or type(symbol) is np.int64:
        return Contract(conId=int(symbol))
    if type(symbol) is str and symbol.isnumeric():
        return Contract(conId=int(symbol))
    raise ValueError('todo implement this')


def get_contract_from_csv(contract_csv_file: str = '/home/trader/mmr/data/symbols_historical.csv') -> pd.DataFrame:
    if not os.path.exists(contract_csv_file):
        raise ValueError('csv_file {} not found'.format(contract_csv_file))
    return pd.read_csv(contract_csv_file)


T = TypeVar('T')
K = TypeVar('K')
V = TypeVar('V')
class DictHelper(Generic[K, V]):
    @classmethod
    def to_object(cls, item: Dict[K, V]) -> V:
        def convert(item):
            if isinstance(item, dict):
                return type('faked_' + str(type(V)), (), {k: convert(v) for k, v in item.items()})
            if isinstance(item, list):
                def yield_convert(item):
                    for index, value in enumerate(item):
                        yield convert(value)
                return list(yield_convert(item))
            else:
                return item
        return cast(V, convert(item))

    @classmethod
    def to_series(cls, item: Dict) -> pd.Series:
        return pd.Series(item)

    @classmethod
    def dict_to_tuple_list(cls, d: Dict[K, V]) -> List[Tuple[K, V]]:
        result: List[Tuple[K, V]] = []
        for k, v in d.items():
            result.append((k, v))
        return result

    @classmethod
    def dict_from_object(cls, obj, columns: Optional[List[str]]):
        if type(obj) is not dict and not hasattr(obj, '__dict__'):
            return {str(obj): str(obj)}

        result: Dict = {}
        if type(obj) is dict:
            result = obj
        else:
            result = obj.__dict__

        for k, v in result.items():
            if isinstance(obj, (int, float, complex)):
                continue
            elif type(v) is list:
                result[k] = str(v)
            elif type(v) is dict:
                result = result | DictHelper[K, V].dict_from_object(v, columns)
            elif hasattr(v, '__class__'):
                result = result | DictHelper[K, V].dict_from_object(v, columns)

        if columns and len(columns) > 0:
            new_result = {}
            for c in columns:
                if c in result:
                    new_result[c] = result[c]
            return new_result
        return result


class ListHelper(Generic[T]):
    @classmethod
    def find_or_none(cls, lst: List[T], filter: Callable[[T], bool]) -> Optional[T]:
        for item in lst:
            if filter(item):
                return item
        return None

    @classmethod
    def isin(cls, lst: List[T], expr: Callable[[T], bool]) -> bool:
        for item in lst:
            if expr(item):
                return True
        return False


def parse_fundamentals(xml: str) -> Dict:
    def type_value(type: str):
        for elem in reversed(soup.find_all(type)):
            result[elem['type']] = elem.text.strip()

    def value(key: str):
        elem = soup.find(key)
        result[key] = elem.text  # type: ignore
        for k, value in elem.attrs.items():  # type: ignore
            result[key + '_' + k] = value

    soup = BeautifulSoup(xml, features='lxml')

    result = {}

    # start with the ratios
    for ratio in reversed(soup.find_all('ratio')):
        if ratio['type'] == 'N':
            result[ratio['fieldname']] = float(str(ratio.text).strip())
        elif ratio['type'] == 'D':
            result[ratio['fieldname']] = dt.datetime.fromisoformat(str(ratio.text).strip())
        else:
            print('not found')

    type_value('issueid')
    type_value('coid')
    value('mostrecentsplit')
    value('exchange')
    value('lastmodified')
    value('latestavailableannual')
    value('latestavailableinterim')
    value('employees')
    value('sharesout')
    value('reportingcurrency')
    value('mostrecentexchange')
    value('cotype')
    value('costatus')
    return result


def parse_fundamentals_pandas(xml: str) -> pd.DataFrame:
    return pd.DataFrame(parse_fundamentals(xml))


def which(program):
    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program):
            return program
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file
    return None


def rich_json(json_str: str):
    try:
        df = pd.read_json(json.dumps(json_str))
        rich_table(df)
    except ValueError as ex:
        rich_dict(json_str)  # type: ignore


def rich_tablify(df, financial: bool = False, financial_columns: List[str] = [], include_index=False):
    if type(df) is list:
        df = pd.DataFrame(df)

    if financial:
        locale.setlocale(locale.LC_ALL, 'en_US.UTF-8')

    cols: List[str] = list(df.columns)
    table = Table()
    for column in df.columns:
        table.add_column(str(column))
    for row in df.itertuples():
        r = []
        for i in range(1, len(row)):
            if type(row[i]) is float and not financial:
                r.append('%.3f' % row[i])
            elif type(row[i]) is float and financial:
                if len(financial_columns) > 0 and cols[i - 1] in financial_columns:
                    r.append(locale.currency(row[i], grouping=True))
                elif len(financial_columns) == 0 and financial:
                    r.append(locale.currency(row[i], grouping=True))
                else:
                    r.append('%.3f' % row[i])

            else:
                r.append(str(row[i]))
        table.add_row(*r)
    return table


def rich_table(df, csv: bool = False, financial: bool = False, financial_columns: List[str] = [], include_index=False):
    if type(df) is list:
        df = pd.DataFrame(df)

    if csv:
        if which('vd'):
            temp_file = tempfile.NamedTemporaryFile(suffix='.csv')
            df.to_csv(temp_file.name, index=include_index, float_format='%.2f')
            os.system('vd {}'.format(temp_file.name))
            return None
        else:
            print(df.to_csv(index=False))
        return

    table = rich_tablify(df, financial, financial_columns, include_index)

    console = Console()
    console.print(table)


def rich_dict(d: Dict):
    table = Table()
    table.add_column('key')
    table.add_column('value')
    for key, value in d.items():
        table.add_row(str(key), str(value))
    console = Console()
    console.print(table)


def rich_list(list_source: List):
    d = {}
    for counter in range(0, len(list_source)):
        d[counter] = list_source[counter]
    rich_dict(d)


def paginate(content: str):
    def generate_content(content: str):
        for line in io.StringIO(content).readlines():
            yield [('', line)]

    p = Pager()
    p.add_source(GeneratorSource(generate_content(content)))  # type: ignore
    p.run()


def timezoneify(date_time: Union[dt.datetime, Timestamp, str], timezone: Union[str, tzfile]) -> dt.datetime:
    zone = None

    if isinstance(date_time, Timestamp):
        date_time = cast(Timestamp, date_time).to_pydatetime()

    if isinstance(date_time, str):
        date_time = dt.datetime.strptime(date_time, '%Y%m%d-%H:%M:%S')

    if isinstance(timezone, str):
        zone = gettz(timezone)  # type: ignore
    elif isinstance(timezone, tzfile):
        zone = timezone  # type: ignore
    else:
        raise ValueError('timezone should be either string or tzfile')

    date_time = cast(dt.datetime, date_time.astimezone(zone))  # not sure if I should be casting here
    return date_time

def utcify(date_time: dt.datetime) -> dt.datetime:
    if not date_time.tzinfo:
        raise ValueError('tzinfo on datetime required')

    return date_time.astimezone(pytz.UTC)

def utcify_str(date_time: dt.datetime) -> str:
    return utcify(date_time).strftime('%Y%m%d-%H:%M:%S')

def dateify(
    date_time: Optional[Union[dt.datetime, dt.date, Timestamp]] = None,
    timezone: Optional[Union[str, tzfile]] = None,
    make_eod: bool = False,
    make_sod: bool = False,
) -> dt.datetime:

    zone = None

    if not timezone:
        zone = tzlocal()
    elif timezone and isinstance(timezone, str):
        zone = gettz(timezone)  # type: ignore
    elif timezone and isinstance(timezone, tzfile):
        zone = timezone  # type: ignore

    if isinstance(date_time, dt.date) and not isinstance(date_time, dt.datetime):
        # dt.date's don't have timezone's
        result = dt.datetime(year=date_time.year, month=date_time.month, day=date_time.day, tzinfo=zone)
        if make_eod:
            result = result.replace(hour=23, minute=59, second=59)
        if make_sod:
            result = result.replace(hour=0, minute=0, second=0)
        return result

    if date_time:
        if isinstance(date_time, Timestamp):
            date_time = cast(Timestamp, date_time).to_pydatetime()

        date_time = cast(dt.datetime, date_time)

        # check to see if there is already a tzinfo
        if date_time.tzinfo and not timezone:
            if make_eod:
                date_time = date_time.replace(hour=23, minute=59, second=59)
            if make_sod:
                date_time = date_time.replace(hour=0, minute=0, second=0)

            return date_time.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            # if we have an already existing timezone in the datetime
            # and the user has passed a timezone, let's convert!
            date_time = date_time.astimezone(zone)
            date_time = date_time.replace(hour=0, minute=0, second=0, microsecond=0)
            if make_eod:
                date_time = date_time.replace(hour=23, minute=59, second=59)
            if make_sod:
                date_time = date_time.replace(hour=0, minute=0, second=0)
            return date_time
    else:
        return dt.datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)


def get_network_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    ip = s.getsockname()[0]
    s.close()
    return ip

# todo change this to use exchange calendar
def daily_open(data_frame: pd.DataFrame) -> pd.DataFrame:
    return data_frame.at_time('09:30')


# todo change this to use exchange calendar
def daily_close(data_frame: pd.DataFrame) -> pd.DataFrame:
    return data_frame.at_time('16:00')


# todo change this to use exchange calendar
def market_hours(data_frame: pd.DataFrame) -> pd.DataFrame:
    return data_frame.between_time('09:30', '16:00')


def get_exchange_calendar(contract: Contract) -> ExchangeCalendar:
    exchange = 'NASDAQ'
    if contract.exchange and 'NYSE' in contract.exchange:
        exchange = 'NYSE'
    if contract.exchange and 'SMART' in contract.exchange:
        exchange = 'NASDAQ'
    return ec.get_calendar(exchange)


def contracts(contract_file_name: str = '/home/trader/mmr/data/ib_symbols_nyse_nasdaq.csv',
              n: Optional[int] = None) -> pd.DataFrame:
    results = pd.read_csv(contract_file_name)
    if n:
        return results.sort_values(by='market cap', ascending=False).head(n)
    return results.sort_values(by='market cap', ascending=False)


def hist(X):
    print(plt.hist(X))


def scatter(Y):
    X = list(range(1, len(Y) + 1))
    fig = plt.Figure()
    fig.width = 100
    fig.height = 40
    fig.color_mode = 'byte'
    fig.scatter(X, Y)
    print(fig.show())


def line(Y):
    X = list(range(1, len(Y) + 1))
    fig = plt.Figure()
    fig.width = 100
    fig.height = 40
    fig.color_mode = 'byte'
    fig.plot(X, Y, lc=None, interp='linear', label=None)
    print(fig.show())


def pdt(date_time: dt.datetime) -> str:
    return date_time.strftime('%Y-%m-%d')


# def from_aiter(iter, loop) -> Observable:
#     def on_subscribe(observer, scheduler):
#         async def _aio_sub():
#             try:
#                 async for i in iter:
#                     observer.on_next(i)
#                 loop.call_soon(
#                     observer.on_completed)
#             except Exception as e:
#                 loop.call_soon(
#                     functools.partial(observer.on_error, e))

#         task = asyncio.ensure_future(_aio_sub(), loop=loop)
#         return Disposable(lambda: task.cancel())  # type: ignore
#     return rx.create(on_subscribe)


def date_range(start_date: dt.datetime,
               end_date: dt.datetime = dt.datetime.now(),
               exchange_calendar: Optional[ec.ExchangeCalendar] = None):
    current = dateify(end_date)
    while current >= dateify(start_date):
        if exchange_calendar and exchange_calendar.is_session(current):
            yield current
            current = dateify(current - dt.timedelta(days=1))
        elif exchange_calendar and not exchange_calendar.is_session(current):
            current = dateify(current - dt.timedelta(days=1))
            continue
        else:
            yield current
            current = dateify(current - dt.timedelta(days=1))


def day_iter(start_date: dt.datetime,
             end_date: dt.datetime):
    td = dt.timedelta(days=1)
    current_date = dateify(start_date)
    end_date = dateify(end_date)
    while current_date <= end_date:
        yield current_date
        current_date += td


def rolling_window(seq, n=2):
    it = iter(seq)
    win = deque((next(it, None) for _ in range(n)), maxlen=n)
    yield win
    append = win.append
    for e in it:
        append(e)
        yield win


def window(seq, n=2):
    it = iter(seq)
    acc = []
    counter = 0
    for e in it:
        acc.append(e)
        counter = counter + 1
        if counter != 0 and counter % n == 0:
            counter = 0
            yield acc
            acc = []
    yield acc


def reformat_large_tick_values(tick_val, pos):
    if tick_val >= 1000000000:
        val = round(tick_val / 1000000000, 1)
        new_tick_format = '{:}B'.format(val)
    elif tick_val >= 1000000:
        val = round(tick_val / 1000000, 1)
        new_tick_format = '{:}M'.format(val)
    elif tick_val >= 1000:
        val = round(tick_val / 1000, 1)
        new_tick_format = '{:}K'.format(val)
    elif tick_val < 1000:
        new_tick_format = round(tick_val, 1)
    else:
        new_tick_format = tick_val

    # make new_tick_format into a string value
    new_tick_format = str(new_tick_format)

    # code below will keep 4.5M as is but change values such as 4.0M to 4M since that zero after the decimal isn't needed
    index_of_decimal = new_tick_format.find(".")

    if index_of_decimal != -1:
        value_after_decimal = new_tick_format[index_of_decimal + 1]
        if value_after_decimal == "0":
            # remove the 0 after the decimal point since it's not needed
            new_tick_format = new_tick_format[0:index_of_decimal] + new_tick_format[index_of_decimal + 2:]

    return new_tick_format


def pct_change_adjust(df: pd.DataFrame, column: str) -> pd.Series:
    df[column] = df[column].pct_change()
    # set the first row to be 0.0 instead of nan
    df[column].iloc[0] = 0.0
    return df[column]


def fit_distribution(data, distribution_function, bins=200):
    # try and fit
    y, x = np.histogram(data, bins=bins, density=True)
    distribution = distribution_function
    params = distribution.fit(data)

    # Separate parts of parameters
    arg = params[:-2]
    loc = params[-2]
    scale = params[-1]

    # Calculate fitted PDF and error with fit in distribution
    pdf = distribution.pdf(x, loc=loc, scale=scale, *arg)

    return (x, pdf, params)


def best_fit_distribution(data, bins=200, ax=None):
    """Model data by finding best fit distribution to data"""
    # Get histogram of original data
    y, x = np.histogram(data, bins=bins, density=True)
    x = (x + np.roll(x, -1))[:-1] / 2.0

    # Distributions to check
    DISTRIBUTIONS = [
        st.alpha, st.anglit, st.arcsine, st.beta, st.betaprime, st.bradford, st.burr, st.cauchy, st.chi, st.chi2, st.cosine,
        st.dgamma, st.dweibull, st.erlang, st.expon, st.exponnorm, st.exponweib, st.exponpow, st.f, st.fatiguelife, st.fisk,
        st.foldcauchy, st.foldnorm, st.genlogistic, st.genpareto, st.gennorm, st.genexpon,
        st.genextreme, st.gausshyper, st.gamma, st.gengamma, st.genhalflogistic, st.gilbrat, st.gompertz, st.gumbel_r,
        st.gumbel_l, st.halfcauchy, st.halflogistic, st.halfnorm, st.halfgennorm, st.hypsecant, st.invgamma, st.invgauss,
        st.invweibull, st.johnsonsb, st.johnsonsu, st.ksone, st.kstwobign, st.laplace, st.levy, st.levy_l,
        st.logistic, st.loggamma, st.loglaplace, st.lognorm, st.lomax, st.maxwell, st.mielke, st.nakagami,
        st.norm, st.pareto, st.pearson3, st.powerlaw, st.powerlognorm, st.powernorm, st.rdist, st.reciprocal,
        st.rayleigh, st.rice, st.recipinvgauss, st.semicircular, st.t, st.triang, st.truncexpon, st.truncnorm, st.tukeylambda,
        st.uniform, st.vonmises, st.vonmises_line, st.wald, st.weibull_min, st.weibull_max, st.wrapcauchy
    ]

    # Best holders
    best_distribution = st.norm
    best_params = (0.0, 1.0)
    best_sse = np.inf

    # Estimate distribution parameters from data
    for distribution in DISTRIBUTIONS:
        logging.info('fitting {}'.format(distribution))
        # Try to fit the distribution
        try:
            # Ignore warnings from data that can't be fit
            with warnings.catch_warnings():
                warnings.filterwarnings('ignore')

                # fit dist to data
                params = distribution.fit(data)

                # Separate parts of parameters
                arg = params[:-2]
                loc = params[-2]
                scale = params[-1]

                # Calculate fitted PDF and error with fit in distribution
                pdf = distribution.pdf(x, loc=loc, scale=scale, *arg)
                sse = np.sum(np.power(y - pdf, 2.0))

                # if axis pass in add to plot
                try:
                    if ax:
                        pd.Series(pdf, x).plot(ax=ax)  # type: ignore
                except Exception:
                    pass

                # identify if this distribution is better
                if best_sse > sse > 0:
                    best_distribution = distribution
                    best_params = params
                    best_sse = sse

        except Exception:
            pass

    return (best_distribution, best_params)

