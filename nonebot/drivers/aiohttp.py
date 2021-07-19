"""
"""

import signal
import asyncio
from dataclasses import dataclass
from typing import Set, List, Union, Callable, Awaitable

import aiohttp
from yarl import URL

from nonebot.log import logger
from nonebot.adapters import Bot
from nonebot.typing import overrides
from nonebot.config import Env, Config
from nonebot.exception import SetupFailed
from nonebot.drivers import ForwardDriver, HTTPConnection, HTTPRequest, WebSocket

STARTUP_FUNC = Callable[[], Awaitable[None]]
SHUTDOWN_FUNC = Callable[[], Awaitable[None]]
AVAILABLE_REQUEST = Union[HTTPRequest, WebSocket]


@dataclass
class RequestSetup:
    adapter: str
    request: AVAILABLE_REQUEST
    poll_interval: float
    reconnect_interval: float


class Driver(ForwardDriver):

    def __init__(self, env: Env, config: Config):
        super().__init__(env, config)
        self.startup_funcs: Set[STARTUP_FUNC] = set()
        self.shutdown_funcs: Set[SHUTDOWN_FUNC] = set()
        self.requests: List[RequestSetup] = []

    @property
    @overrides(ForwardDriver)
    def type(self) -> str:
        """驱动名称: ``aiohttp``"""
        return "aiohttp"

    @property
    @overrides(ForwardDriver)
    def logger(self):
        return logger

    @overrides(ForwardDriver)
    def on_startup(self, func: Callable) -> Callable:
        self.startup_funcs.add(func)
        return func

    @overrides(ForwardDriver)
    def on_shutdown(self, func: Callable) -> Callable:
        self.shutdown_funcs.add(func)
        return func

    @overrides(ForwardDriver)
    def setup(self,
              adapter: str,
              request: HTTPConnection,
              poll_interval: float = 3.,
              reconnect_interval: float = 3.) -> None:
        if not isinstance(request, (HTTPRequest, WebSocket)):
            raise TypeError(f"Request Type {type(request)!r} is not supported!")
        self.requests.append(
            RequestSetup(adapter, request, poll_interval, reconnect_interval))

    @overrides(ForwardDriver)
    def run(self, *args, **kwargs):
        loop = asyncio.get_event_loop()
        signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
        for s in signals:
            loop.add_signal_handler(
                s,
                lambda s=s: asyncio.create_task(self.shutdown(loop, signal=s)))

        try:
            asyncio.create_task(self.startup())
            loop.run_forever()
        finally:
            loop.close()

    async def startup(self):
        setups = []
        loop = asyncio.get_event_loop()
        for setup in self.requests:
            if isinstance(setup.request, HTTPRequest):
                setups.append(
                    self._http_setup(setup.adapter, setup.request,
                                     setup.poll_interval))
            else:
                setups.append(
                    self._ws_setup(setup.adapter, setup.request,
                                   setup.reconnect_interval))

        try:
            await asyncio.gather(*setups)
        except Exception as e:
            logger.opt(
                colors=True,
                exception=e).error("Application startup failed. Exiting.")
            asyncio.create_task(self.shutdown(loop))
            return

        # run startup
        cors = [startup() for startup in self.startup_funcs]
        if cors:
            try:
                await asyncio.gather(*cors)
            except Exception as e:
                logger.opt(colors=True, exception=e).error(
                    "<r><bg #f8bbd0>Error when running startup function. "
                    "Ignored!</bg #f8bbd0></r>")

    async def shutdown(self,
                       loop: asyncio.AbstractEventLoop,
                       signal: signal.Signals = None):
        # TODO: shutdown

        # run shutdown
        cors = [shutdown() for shutdown in self.shutdown_funcs]
        if cors:
            try:
                await asyncio.gather(*cors)
            except Exception as e:
                logger.opt(colors=True, exception=e).error(
                    "<r><bg #f8bbd0>Error when running shutdown function. "
                    "Ignored!</bg #f8bbd0></r>")

        tasks = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task()
        ]

        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

        loop.stop()

    async def _http_setup(self, adapter: str, request: HTTPRequest,
                          poll_interval: float):
        BotClass = self._adapters[adapter]
        self_id, _ = await BotClass.check_permission(self, request)

        if not self_id:
            raise SetupFailed("Bot self_id get failed")

        bot = BotClass(self_id, request)
        self._bot_connect(bot)
        asyncio.create_task(self._http_loop(bot, request, poll_interval))

    async def _ws_setup(self, adapter: str, request: WebSocket,
                        reconnect_interval: float):
        BotClass = self._adapters[adapter]
        self_id, _ = await BotClass.check_permission(self, request)

        if not self_id:
            raise SetupFailed("Bot self_id get failed")

        bot = BotClass(self_id, request)
        self._bot_connect(bot)
        asyncio.create_task(self._ws_loop(bot, request, reconnect_interval))

    async def _http_loop(self, bot: Bot, request: HTTPRequest,
                         poll_interval: float):
        try:
            headers = request.headers
            url = URL.build(scheme=request.scheme,
                            host=request.headers["host"],
                            path=request.path,
                            query_string=request.query_string.decode("latin-1"))
            timeout = aiohttp.ClientTimeout(30)
            async with aiohttp.ClientSession(headers=headers,
                                             timeout=timeout) as session:
                while True:
                    try:
                        async with session.request(
                                request.method, url,
                                data=request.body) as response:
                            response.raise_for_status()
                            data = await response.read()
                            asyncio.create_task(bot.handle_message(data))
                    except aiohttp.ClientResponseError as e:
                        logger.opt(colors=True, exception=e).error(
                            f"Error occurred while requesting {url}")

                    await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.opt(colors=True, exception=e).error(
                "Unexpected exception occurred while http polling")
        finally:
            self._bot_disconnect(bot)

    async def _ws_loop(self, bot: Bot, request: WebSocket,
                       reconnect_interval: float):
        try:
            headers = request.headers
            url = URL.build(scheme=request.scheme,
                            host=request.headers["host"],
                            path=request.path,
                            query_string=request.query_string.decode("latin-1"))
            timeout = aiohttp.ClientTimeout(30)
            async with aiohttp.ClientSession(headers=headers,
                                             timeout=timeout) as session:
                while True:
                    async with session.ws_connect(url) as ws:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.text:
                                asyncio.create_task(
                                    bot.handle_message(msg.data.encode()))
                            elif msg.type == aiohttp.WSMsgType.binary:
                                asyncio.create_task(bot.handle_message(
                                    msg.data))
                            elif msg.type == aiohttp.WSMsgType.error:
                                logger.opt(colors=True).error(
                                    "<r><bg #f8bbd0>Error while handling websocket frame. "
                                    "Try to reconnect...</bg></r>")
                                break
                    asyncio.sleep(reconnect_interval)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.opt(colors=True, exception=e).error(
                "Unexpected exception occurred while websocket loop")
        finally:
            self._bot_disconnect(bot)