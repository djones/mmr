from ib_insync import Contract
from ib_insync.ib import IB
from ib_insync.ticker import Ticker
from reactivex.observer import AutoDetachObserver
from trader.common.exceptions import TraderConnectionException, TraderException
from trader.common.listener_helpers import Helpers
from trader.common.logging_helper import setup_logging
from trader.common.singleton import Singleton
from trader.data.data_access import TickStorage
from trader.data.universe import UniverseAccessor
from trader.listeners.ib_history_worker import IBHistoryWorker
from trader.messaging.clientserver import RemotedClient, TopicPubSub
from trader.messaging.trader_service_api import TraderServiceApi
from trader.objects import Action, BarSize, WhatToShow
from trader.trading.strategy import Strategy
from typing import cast, Dict, List, Optional

import asyncio
import datetime as dt
import importlib
import inspect
import os
import pandas as pd
import yaml


logging = setup_logging(module_name='strategy_runtime')


error_table = {
    'trader.common.exceptions.TraderException': TraderException,
    'trader.common.exceptions.TraderConnectionException': TraderConnectionException
}


class StrategyRuntime(metaclass=Singleton):
    def __init__(
        self,
        ib_server_address: str,
        ib_server_port: int,
        strategy_runtime_ib_client_id: int,
        arctic_server_address: str,
        arctic_universe_library: str,
        zmq_pubsub_server_address: str,
        zmq_pubsub_server_port: int,
        zmq_rpc_server_address: str,
        zmq_rpc_server_port: int,
        strategies_directory: str,
        strategy_config_file: str,
        paper_trading: bool = False,
        simulation: bool = False
    ):
        self.ib_server_address = ib_server_address
        self.ib_server_port = ib_server_port
        self.strategy_runtime_ib_client_id: int = strategy_runtime_ib_client_id
        self.arctic_server_address = arctic_server_address
        self.arctic_universe_library = arctic_universe_library
        self.simulation: bool = simulation
        self.paper_trading = paper_trading
        self.zmq_pubsub_server_address = zmq_pubsub_server_address
        self.zmq_pubsub_server_port = zmq_pubsub_server_port
        self.zmq_rpc_server_address = zmq_rpc_server_address
        self.zmq_rpc_server_port = zmq_rpc_server_port
        self.strategies_directory = strategies_directory
        self.strategy_config_file = strategy_config_file

        # todo: this is wrong as we'll have a whole bunch of different tickdata libraries for
        # different bartypes etc.
        self.storage: TickStorage = TickStorage(self.arctic_server_address)

        self.accessor = UniverseAccessor(arctic_server_address, arctic_universe_library)
        self.remoted_client = RemotedClient[TraderServiceApi](error_table=error_table)
        self.strategies: Dict[int, List[Strategy]] = {}
        self.strategy_implementations: List[Strategy] = []
        self.streams: Dict[int, pd.DataFrame] = {}

        self.historical_data_client: IBHistoryWorker

    def load_strategies(self):
        for root, dirs, files in os.walk(self.strategies_directory):
            for file_name in files:
                file = os.path.join(root, file_name)
                try:
                    relative_import = os.path.relpath(file).replace('.py', '').replace('.pyc', '').replace('/', '.')
                    module = importlib.import_module(relative_import)
                    for x in dir(module):
                        obj = getattr(module, x)

                        if inspect.isclass(obj) and issubclass(obj, Strategy) and obj is not Strategy:
                            logging.debug('found implementation of Strategy {}'.format(obj))
                            # todo: fix this
                            # here's where we need bar_size to strategy
                            instance = obj(self.storage, self.accessor, logging)
                            self.strategy_implementations.append(cast(Strategy, instance))

                            # logic to install and download data for the strategy
                            if instance.install(self):
                                instance.enable()

                except Exception as ex:
                    logging.debug(ex)

    def on_next(self, ticker: Ticker):
        logging.debug('StrategyRuntime.on_next()')
        conId = 0

        if not ticker.contract:
            logging.debug('no contract associated with Ticker')
            return
        else:
            conId = ticker.contract.conId

        # populate the dataframe subscription cache
        if conId not in self.streams:
            self.streams[conId] = pd.DataFrame(Helpers.df(ticker))
        else:
            result = pd.concat([self.streams[conId], Helpers.df(ticker)], axis=0, copy=False)
            self.streams[conId] = result

        def __get_strategies(conId: int) -> List[Strategy]:
            if conId in self.strategies:
                return self.strategies[conId]
            else:
                return []

        # execute the strategies attached to the conId's
        for strategy in __get_strategies(conId):
            signal = strategy.on_next(self.streams[conId])
            if signal and signal.action == Action.BUY:
                logging.info('BUY action')
            elif signal and signal.action == Action.SELL:
                logging.info('SELL action')

    def on_error(self, ex: Exception):
        logging.debug('StrategyRuntime.on_error')

    def on_completed(self):
        logging.debug('StrategyRuntime.on_completed')

    def subscribe(self, strategy: Strategy, contract: Contract) -> None:
        logging.debug('strategy_runtime.subscribe() contract: {} strategy: {}'.format(contract, strategy))
        if contract.conId not in self.strategies:
            self.strategies[contract.conId] = []
            self.strategies[contract.conId].append(strategy)
            self.remoted_client.rpc().publish_contract(contract=contract, delayed=False)
        elif contract.conId in self.strategies and strategy not in self.strategies[contract.conId]:
            self.strategies[contract.conId].append(strategy)

    def subscribe_universe(self, strategy: Strategy, universe_name: str) -> None:
        logging.debug('strategy_runtime.subscribe_universe() universe: {} strategy: {}'.format(universe_name, strategy))
        universe = self.accessor.get(universe_name)

        for security in universe.security_definitions:
            self.subscribe(strategy, Contract(conId=security.conId))

    def load_strategy(
        self,
        name: str,
        bar_size_str: str,
        conids: Optional[List[int]],
        universe: Optional[str],
        historical_days_prior: int,
        module: str,
        description: str,
    ) -> None:
        root_path = os.path.abspath(os.getcwd())
        file = os.path.join(root_path, module)
        try:
            relative_import = os.path.relpath(file).replace('.py', '').replace('/', '.')
            loaded_module = importlib.import_module(relative_import)
            for x in dir(loaded_module):
                obj = getattr(loaded_module, x)

                # todo, might have to find StrategyConfig and load that too
                if inspect.isclass(obj) and issubclass(obj, Strategy) and obj is not Strategy:
                    logging.debug('found implementation of Strategy {}'.format(obj))

                    # todo: fix this
                    # here's where we need bar_size to strategy
                    instance = obj(self.storage, self.accessor, logging)
                    instance.name = name
                    instance.bar_size = BarSize.parse_str(bar_size_str)
                    instance.description = description
                    instance.conids = conids
                    instance.universe = universe
                    instance.historical_days_prior = historical_days_prior
                    instance.description = description

                    self.strategy_implementations.append(cast(Strategy, instance))

                    # logic to install and download data for the strategy
                    if instance.install(self):
                        instance.enable()

        except Exception as ex:
            logging.debug(ex)

    def config_loader(self, config_file: str):
        logging.debug('loading config file {}'.format(config_file))
        conf_file = open(config_file, 'r')
        config = yaml.load(conf_file, Loader=yaml.FullLoader)

        for strategy_config in config['strategies']:
            self.load_strategy(
                name=strategy_config['name'],
                bar_size_str=strategy_config['bar_size'],
                conids=strategy_config['conids'] if 'conids' in strategy_config else None,
                universe=strategy_config['universe'] if 'universe' in strategy_config else None,
                historical_days_prior=strategy_config['historical_days_prior'] if 'historical_days_prior' in strategy_config else 1,
                module=strategy_config['module'] if 'module' in strategy_config else '',
                description=strategy_config['description'] if 'description' in strategy_config else '',
            )

    async def get_historical_data(self):
        for strategy in self.strategy_implementations:
            historical_days = strategy.historical_days_prior if strategy.historical_days_prior else 1

            if strategy.conids:
                for conId in strategy.conids:
                    await self.historical_data_client.get_contract_history(
                        security=Contract(conId=conId),
                        what_to_show=WhatToShow.MIDPOINT,
                        bar_size=strategy.bar_size,
                        start_date=dt.datetime.now() - dt.timedelta(days=historical_days),
                        end_date=dt.datetime.now(),
                    )

            if strategy.universe:
                conids = [x.conId for x in self.accessor.get(strategy.universe).security_definitions]
                for conId in conids:
                    await self.historical_data_client.get_contract_history(
                        security=Contract(conId=conId),
                        what_to_show=WhatToShow.MIDPOINT,
                        bar_size=strategy.bar_size,
                        start_date=dt.datetime.now() - dt.timedelta(days=historical_days),
                        end_date=dt.datetime.now(),
                    )
        logging.debug('finished get_historical_data()')

    async def run(self):
        logging.info('starting strategy_runtime')
        logging.debug('StrategyRuntime.run()')

        asyncio.get_event_loop().run_until_complete(self.remoted_client.connect())

        self.zmq_subscriber = TopicPubSub[Ticker](
            self.zmq_pubsub_server_address,
            self.zmq_pubsub_server_port,
        )

        logging.debug('subscribing to tick stream')
        observable = await self.zmq_subscriber.subscriber('ticker')
        self.observer = AutoDetachObserver(on_next=self.on_next, on_error=self.on_error, on_completed=self.on_completed)
        self.subscription = observable.subscribe(self.observer)

        logging.debug('loading {} config file'.format(self.strategy_config_file))
        self.config_loader(self.strategy_config_file)

        logging.debug('subscribing to streams for all conids')
        for strategy in self.strategy_implementations:
            if strategy.conids:
                for conId in strategy.conids:
                    self.subscribe(strategy, Contract(conId=conId))

            if strategy.universe:
                self.subscribe_universe(strategy, strategy.universe)

        logging.debug('starting connection to IB for historical data')
        client = IB()
        self.historical_data_client = IBHistoryWorker(
            self.ib_server_address,
            self.ib_server_port,
            self.strategy_runtime_ib_client_id + 1,
        )
        await self.historical_data_client.connect()
        await self.get_historical_data()

