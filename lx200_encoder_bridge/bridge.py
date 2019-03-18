#!/usr/bin/env python3

import argparse
import logging

import asyncio
from aiohttp import web
import requests
import socketio
from socketio.exceptions import ConnectionError

import lx200.parser
import lx200.commands
import lx200.responses
import lx200.store


logger = logging.getLogger('lx200-bridge')

store = lx200.store.Store()


class LX200Protocol(asyncio.Protocol):

    def __init__(self, store, server_path, ra_id='RA', dec_id='DEC', *args, **kwargs):
        self.transport = None
        self.store = store
        self.server_path = server_path
        self.ra_id = ra_id
        self.dec_id = dec_id

        self.parser = lx200.parser.Parser()

        self.dispatch = {
            lx200.commands.SlewToTarget: self.do_goto,
            lx200.commands.SlewToTargetObject: self.do_goto,
            lx200.commands.SyncDatabase: self.do_sync,
        }

    def connection_made(self, transport):
        self.transport = transport
        transport.set_write_buffer_limits(high=0, low=0)
        logger.info('Connected: {}'.format(transport.get_extra_info('socket', 'peername')))

    def data_received(self, data):
        decoded_data = data.decode('ascii')
        self.parser.feed(decoded_data)

        logger.debug('<< {}'.format(decoded_data))

        while self.parser.output:
            command = self.parser.output.pop()
            self.store.commit_command(command)

            response = lx200.responses.for_command(command)

            action = self.dispatch.get(command.__class__, None)
            if action:
                action(command=command, response=response)

            self.store.fill_response(response)

            logger.debug('>> {}'.format(repr(response)))
            logger.debug('>> {}'.format(str(response)))

            self.transport.write(bytes(str(response), 'ascii'))

    def do_goto(self, *args, **kwargs):
        return self.__call_axis_action('goto')

    def do_sync(self, *args, **kwargs):
        return self.__call_axis_action('sync')

    def __call_axis_action(self, action='goto'):
        if 'mount.target.right_ascencion' in self.store:
            payload = self.store['mount.target.right_ascencion']
            requests.put('{}/api/devices/{}/{}/astronomical'.format(self.server_path, self.ra_id, action), json=payload)

        if 'mount.target.declination' in self.store:
            payload = self.store['mount.target.declination']
            requests.put('{}/api/devices/{}/{}/angle'.format(self.server_path, self.dec_id, action), json=payload)



def build_lx200_protocol_factory(store, server, ra_id, dec_id, *args, **kwargs):
    def __inner(*args, **kwargs):
        return LX200Protocol(store, server, ra_id, dec_id, *args, **kwargs)
    return __inner



class ScopeStoreServer:

    def __init__(self, host, port, store):
        self.host = host
        self.port = port
        self.store = store

        routes = web.RouteTableDef()

        @routes.get('/')
        async def json_store_status(request):
            return web.json_response(request.app['scope_store'])

        app = self.app = web.Application()
        app['scope_store'] = store

        app.add_routes(routes)
        self.runner = web.AppRunner(app)

    async def start(self):

        await self.runner.setup()

        site = self.site = web.TCPSite(self.runner, self.host, self.port)

        await self.site.start()


class WSUpdater:
    def __init__(self, encoder_server_path, store, ra_axis_id, dec_axis_id):
        self.server_path = encoder_server_path
        self.store = store
        self.ra_axis_id = ra_axis_id
        self.dec_axis_id = dec_axis_id

        sio = self.sio = socketio.AsyncClient()

        @sio.on('position')
        async def update_position(payload):
            parameter_map = {
                self.ra_axis_id: [
                    ('position_astronomical', 'mount.right_ascencion'),
                    ('target_astronomical', 'mount.target.right_ascencion'),
                ],
                self.dec_axis_id: [
                    ('position_angle', 'mount.declination'),
                    ('target_angle', 'mount.target.declination'),
                ]
            }

            for src, dest in parameter_map[payload['id']]:
                self.store[dest].update(payload[src])

    async def start(self):
        async def __connect():
            while True:
                try:
                    await self.sio.connect(self.server_path)
                except (ConnectionError, ValueError):
                    pass
                else:
                    break

            await asyncio.sleep(1)
        return asyncio.create_task(__connect())


async def run(args):

    loop = asyncio.get_running_loop()
    lx200_factory = build_lx200_protocol_factory(store, args.encoder_server, args.ra_axis_id, args.dec_axis_id)

    server = await loop.create_server(lx200_factory, args.host, args.port)
    store_server = ScopeStoreServer(args.host, args.web_port, store)
    await store_server.start()

    logger.info('LX200 <-> Ethernet Encoder Bridge')
    logger.info('Serving on {}'.format(server.sockets[0].getsockname()))
    logger.info('Serving state on http://{}:{}'.format(args.host, args.web_port))


def main():
    parser = argparse.ArgumentParser(description='Bridge between ethernet-encoder-servo and LX200 protocol')

    parser.add_argument('--encoder-server', required=False, default='http://localhost:5000', help='ethernet encoder server url')
    parser.add_argument('--ra-axis-id', required=False, default='RA', help='Id of axis to map to Right Ascencion')
    parser.add_argument('--dec-axis-id', required=False, default='DEC', help='Id of axis to map to Declination')

    parser.add_argument('--port', type=int, required=False, default=7634)
    parser.add_argument('--web-port', type=int, required=False, default=8081)
    parser.add_argument('--host', type=str, required=False, default='127.0.0.1')

    parser.add_argument('--verbose', required=False, default=False, action='store_true')

    args = parser.parse_args()

    logging.basicConfig()
    logger.setLevel(logging.INFO)

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(run(args))
    loop.run_forever()
