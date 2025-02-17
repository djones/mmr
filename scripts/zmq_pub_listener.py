import os
import sys


# in order to get __main__ to work, we follow: https://stackoverflow.com/questions/16981921/relative-imports-in-python-3
PACKAGE_PARENT = '../'
SCRIPT_DIR = os.path.dirname(os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(__file__))))
sys.path.append(os.path.normpath(os.path.join(SCRIPT_DIR, PACKAGE_PARENT)))

from asyncio.events import AbstractEventLoop
from ib_insync.contract import Contract
from ib_insync.ticker import Ticker
from reactivex.abc import DisposableBase, ObserverBase
from reactivex.observer import AutoDetachObserver
from rich.live import Live
from rich.table import Table
from trader.common.logging_helper import setup_logging
from trader.messaging.clientserver import TopicPubSub
from typing import Dict, Optional

import asyncio
import click
import numpy as np
import pandas as pd
import rich
import signal


logging = setup_logging(module_name='trading_runtime')

class RichLiveDataFrame():
    def __init__(self, console: rich.console.Console):
        self.table = Table()
        self.live = Live()
        self.console = console
        self.first: bool = True

    def print_console(self, df: pd.DataFrame, title: Optional[str] = None):
        def move(y, x):
            print("\033[%d;%dH" % (y, x))

        if self.first:
            self.console.clear()
            self.first = False

        self.table = Table()
        for column in df.columns:
            self.table.add_column(column)

        for index, value_list in enumerate(df.values.tolist()):
            row = [str(x) for x in value_list]
            self.table.add_row(*row)

        # self.console.clear()
        move(0, 0)
        if title:
            self.console.print(title)
        self.console.print(self.table)


class ZmqPrettyPrinter():
    def __init__(
        self,
        zmq_pubsub_server_address: str,
        zmq_pubsub_server_port: int,
        csv: bool = False,
    ):
        self.zmq_pubsub_server_address = zmq_pubsub_server_address
        self.zmq_pubsub_server_port = zmq_pubsub_server_port
        self.contract_ticks: Dict[Contract, Ticker] = {}
        self.console = rich.console.Console()
        self.rich_live = RichLiveDataFrame(self.console)
        self.subscription: DisposableBase
        self.observer: ObserverBase
        self.csv = csv
        self.counter = 0
        self.zmq_subscriber: TopicPubSub
        self.wait_handle: asyncio.Event = asyncio.Event()
        self.being_shutdown = False

    def print_console(self, ticker: Optional[Ticker] = None):
        def get_snap(ticker: Ticker):
            date_time_str = ticker.time.strftime('%H:%M.%S') if ticker.time else ''
            return {
                'symbol': ticker.contract.symbol if ticker.contract else '',
                'primaryExchange': ticker.contract.primaryExchange if ticker.contract else '',
                'currency': ticker.contract.currency if ticker.contract else '',
                'time': date_time_str,
                'bid': '%.2f' % ticker.bid,
                'ask': '%.2f' % ticker.ask,
                'last': '%.2f' % ticker.last,
                'lastSize': int(ticker.lastSize) if not np.isnan(ticker.lastSize) else -1,
                'open': '%.2f' % ticker.open,
                'high': '%.2f' % ticker.high,
                'low': '%.2f' % ticker.low,
                'close': '%.2f' % ticker.close,
                'halted': int(ticker.halted) if not np.isnan(ticker.halted) else -1
            }

        # self.console.clear(True)
        # rich_table(data_frame, False, True, ['currency', 'bid', 'ask', 'last', 'open', 'high', 'low', 'close'])
        if not self.csv:
            self.counter += 1
            data = [get_snap(ticker) for contract, ticker in self.contract_ticks.items()]
            data_frame = pd.DataFrame(data)
            self.rich_live.print_console(data_frame, 'Ctrl-c to stop...')
            if self.counter % 1000 == 0:
                self.console.clear()
                self.contract_ticks.clear()
        else:
            t = get_snap(ticker)  # type: ignore
            str_values = [str(v) for v in t.values()]
            print(','.join(str_values))

    def on_next(self, ticker: Ticker):
        if ticker.contract:
            self.contract_ticks[ticker.contract] = ticker
            try:
                self.print_console(ticker)
            except Exception as ex:
                logging.exception(ex)
                self.wait_handle.set()
                raise ex

    def on_error(self, ex: Exception):
        logging.exception(ex)
        self.wait_handle.set()
        raise ex

    def on_completed(self):
        logging.debug('zmq_pub_listener.on_completed')

    async def listen(self, topic: str):
        try:
            logging.debug('zmq_pub_listener listen({}, {}, {})'.format(
                self.zmq_pubsub_server_address,
                self.zmq_pubsub_server_port,
                topic
            ))

            self.zmq_subscriber = TopicPubSub[Ticker](
                self.zmq_pubsub_server_address,
                self.zmq_pubsub_server_port,
            )

            observable = await self.zmq_subscriber.subscriber(topic=topic)
            self.observer = AutoDetachObserver(on_next=self.on_next, on_error=self.on_error, on_completed=self.on_completed)
            self.subscription = observable.subscribe(self.observer)

            await self.wait_handle.wait()
        except KeyboardInterrupt:
            logging.debug('KeyboardInterrupt')
            self.wait_handle.set()
        finally:
            await self.shutdown()

    # https://www.joeltok.com/blog/2020-10/python-asyncio-create-task-fails-silently
    async def shutdown(self):
        logging.debug('shutdown()')
        self.wait_handle.set()
        if not self.being_shutdown:
            self.being_shutdown = True
            self.zmq_subscriber.subscriber_close()
            self.observer.on_completed()
            self.subscription.dispose()


@click.command()
@click.option('--csv', required=True, is_flag=True, default=False)
@click.option('--topic', required=True, default='ticker')
@click.option('--zmq_pubsub_server_address', required=True, default='tcp://127.0.0.1')
@click.option('--zmq_pubsub_server_port', required=True, default=42002)
def main(
    csv: bool,
    topic: str,
    zmq_pubsub_server_address: str,
    zmq_pubsub_server_port: int
):
    printer = ZmqPrettyPrinter(zmq_pubsub_server_address, zmq_pubsub_server_port, csv=csv)

    def stop_loop(loop: AbstractEventLoop):
        loop.run_until_complete(printer.shutdown())

    loop = asyncio.get_event_loop()
    loop.set_debug(enabled=True)
    loop.add_signal_handler(signal.SIGINT, stop_loop, loop)
    loop.run_until_complete(printer.listen(topic=topic))

if __name__ == '__main__':
    main()
