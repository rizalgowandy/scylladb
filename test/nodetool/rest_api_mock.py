#
# Copyright 2023-present ScyllaDB
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#

import aiohttp
import aiohttp.web
import asyncio
import json
import logging
import requests
import sys
import traceback

from typing import Any, Dict, Tuple


logger = logging.getLogger(__name__)


class approximate_value:
    """
    Allow matching query params with a non-exact match, allowing for a given
    difference tolerance (delta).
    """
    def __init__(self, value=None, delta=None):
        self._value = value
        self._delta = delta

    def __eq__(self, v):
        coerced_v = type(self._value)(v)
        return abs(self._value - coerced_v) <= self._delta

    def to_json(self):
        return {"__type__": "approximate_value", "value": self._value, "delta": self._delta}


class expected_request:
    ANY = -1  # allow for any number of requests (including no requests at all), similar to the `*` quantity in regexp
    ONE = 0  # exactly one request is allowed
    MULTIPLE = 1  # one or more request is allowed

    def __init__(self, method: str, path: str, params: dict = {}, multiple: int = ONE,
                 response: Dict[str, Any] = None, response_status: int = 200):
        self.method = method
        self.path = path
        self.params = params
        self.multiple = multiple
        self.response = response
        self.response_status = response_status

        self.hit = 0

    def as_json(self):
        def param_to_json(v):
            try:
                return v.to_json()
            except AttributeError:
                return v

        return {
                "method": self.method,
                "path": self.path,
                "multiple": self.multiple,
                "params": {k: param_to_json(v) for k, v in self.params.items()},
                "response": self.response,
                "response_status": self.response_status}

    def __eq__(self, o):
        return self.method == o.method and self.path == o.path and self.params == o.params

    def __str__(self):
        return json.dumps(self.as_json())


def _make_param_value(value):
    if type(value) is dict and "__type__" in value:
        cls = globals()[value["__type__"]]
        del value["__type__"]
        return cls(**value)

    return value


def _make_expected_request(req_json):
    return expected_request(
            req_json["method"],
            req_json["path"],
            params={k: _make_param_value(v) for k, v in req_json.get("params", dict()).items()},
            multiple=req_json.get("multiple", expected_request.ONE),
            response=req_json.get("response"),
            response_status=req_json.get("response_status", 200))


class handler_match_info(aiohttp.abc.AbstractMatchInfo):
    def __init__(self, handler):
        self.handler = handler
        self._apps = list()

    async def handler(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        """Catch all exceptions and return them to the client.
           Without this, the client would get an 'Internal server error' message
           without any details. Thanks to this the test log shows the actual error.
        """
        try:
            return await self.handler(request)
        except Exception as e:
            tb = traceback.format_exc()
            logger.error(f'Exception when executing {self.handler.__name__}: {e}\n{tb}')
            return aiohttp.web.Response(status=500, text=str(e))

    async def expect_handler(self) -> None:
        return None

    async def http_exception(self) -> None:
        return None

    def get_info(self) -> Dict[str, Any]:
        return {}

    def apps(self) -> Tuple[aiohttp.web.Application, ...]:
        return tuple(self._apps)

    def add_app(self, app: aiohttp.web.Application):
        self._apps.append(app)

    def freeze(self) -> None:
        pass


class rest_server(aiohttp.abc.AbstractRouter):
    EXPECTED_REQUESTS_PATH = "__expected_requests__"

    def __init__(self):
        self.expected_requests = []

    async def resolve(self, request: aiohttp.web.Request) -> aiohttp.abc.AbstractMatchInfo:
        if request.path == f"/{self.EXPECTED_REQUESTS_PATH}":
            return handler_match_info(getattr(self, f"{request.method.lower()}_expected_requests"))

        for req in self.expected_requests:
            if req.path == request.path and req.method == request.method:
                return handler_match_info(self.handle_generic_request)

        raise aiohttp.web.HTTPNotFound()

    async def get_expected_requests(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        return aiohttp.web.json_response([r.as_json() for r in self.expected_requests])

    async def post_expected_requests(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        payload = await request.json()
        self.expected_requests = list(map(_make_expected_request, payload))
        logger.info(f"expected_requests: {list(map(str, self.expected_requests))}")
        return aiohttp.web.json_response({})

    async def delete_expected_requests(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        self.expected_requests = []
        return aiohttp.web.json_response({})

    async def handle_generic_request(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        this_req = expected_request(request.method, request.path, params=dict(request.query))

        if len(self.expected_requests) == 0:
            logger.error(f"unexpected request, expected no request, got {this_req}")
            return aiohttp.web.Response(status=500, text="Expected no requests, got {this_req}")

        expected_req = self.expected_requests[0]
        while this_req != expected_req:
            if expected_req.multiple == expected_request.ANY or (
                    expected_req.multiple >= expected_request.MULTIPLE and expected_req.hit >= expected_req.multiple):
                logger.info(f"popping multi request {expected_req}")
                del self.expected_requests[0]
                expected_req = self.expected_requests[0]

                if len(self.expected_requests) > 0:
                    expected_req = self.expected_requests[0]
                    continue

            logger.error(f"unexpected request\nexpected {expected_req}\ngot      {this_req}")
            return aiohttp.web.Response(status=500, text=f"Expected {expected_req}, got {this_req}")

        if expected_req.multiple == expected_request.ONE:
            del self.expected_requests[0]
        else:
            expected_req.hit += 1

        if expected_req.response is None:
            logger.info(f"expected_request: {expected_req}, no response")
            return aiohttp.web.json_response({})
        else:
            logger.info(f"expected_request: {expected_req}, response: {expected_req.response}")
            return aiohttp.web.json_response(expected_req.response, status=expected_req.response_status)


async def run_server(ip, port):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s - %(message)s",
        datefmt="%H:%M:%S",
    )

    server = rest_server()
    app = aiohttp.web.Application(router=server)

    logger.info("start serving")

    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, ip, port)
    await site.start()

    try:
        while True:
            await asyncio.sleep(3600)  # sleep forever
    except asyncio.exceptions.CancelledError:
        pass

    logger.info("stopping")

    await runner.cleanup()


def get_expected_requests(server):
    """Get the expected requests list from the server.

    This will contain all the unconsumed expected request currently on the
    server. Can be used to check whether all expected requests arrived.

    Params:
    * server - resolved `rest_api_mock_server` fixture (see conftest.py).
    """
    ip, port = server
    r = requests.get(f"http://{ip}:{port}/{rest_server.EXPECTED_REQUESTS_PATH}")
    r.raise_for_status()
    return [_make_expected_request(r) for r in r.json()]


def clear_expected_requests(server):
    """Clear the expected requests list on the server.

    Params:
    * server - resolved `rest_api_mock_server` fixture (see conftest.py).
    """
    ip, port = server
    r = requests.delete(f"http://{ip}:{port}/{rest_server.EXPECTED_REQUESTS_PATH}")
    r.raise_for_status()


def set_expected_requests(server, expected_requests):
    """Set the expected requests list on the server.

    Params:
    * server - resolved `rest_api_mock_server` fixture (see conftest.py).
    * requests - a list of request objects
    """
    ip, port = server
    payload = json.dumps([r.as_json() for r in expected_requests])
    r = requests.post(f"http://{ip}:{port}/{rest_server.EXPECTED_REQUESTS_PATH}", data=payload)
    r.raise_for_status()


if __name__ == '__main__':
    sys.exit(asyncio.run(run_server(sys.argv[1], int(sys.argv[2]))))
