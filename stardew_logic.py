import asyncio
import typing
import urllib.parse
from typing import Optional
import websockets
import ssl

import Utils
from NetUtils import (Endpoint, decode, NetworkSlot, encode, NetworkItem)
from Utils import Version

try:
    from yaml import CLoader as Loader, CDumper as Dumper
except ImportError:
    from yaml import Loader, Dumper
import yaml


class SWData:
    missing_locations: []


class ConnectionContext:
    tags: typing.Set[str] = {"AP", "Tracker"}
    game: typing.Optional[str] = "Stardew Valley"
    items_handling: typing.Optional[int] = 0b111
    want_slot_data: bool = True  # should slot_data be retrieved via Connect

    # defaults
    server: typing.Optional[Endpoint] = None
    server_version: Version = Version(0, 0, 0)
    generator_version: Version = Version(0, 0, 0)

    # remaining type info
    server_address: typing.Optional[str]
    password: typing.Optional[str]
    watcher_event: asyncio.Event
    items_received: typing.List[NetworkItem]
    missing_locations: typing.Set[int]  # server state
    checked_locations: typing.Set[int]  # server state
    server_locations: typing.Set[int]
    slot_data: typing.Dict[str, typing.Any]

    def __init__(self, server_address: str, username: str, password: str) -> None:
        self.server_address = server_address
        self.username = username
        self.password = password
        self.watcher_event = asyncio.Event()
        self.items_received = []
        self.missing_locations = set()  # server state
        self.checked_locations = set()  # server state
        self.server_locations = set()  # all locations the server knows of, missing_location | checked_locations
        self.slot_data = {}

    async def disconnect(self):
        if self.server and self.server.socket is not None and self.server.socket.open:
            await self.server.socket.close()

    async def send_msgs(self, msgs: typing.List[typing.Any]) -> None:
        """ `msgs` JSON serializable """
        if not self.server or not self.server.socket.open or self.server.socket.closed:
            return
        await self.server.socket.send(encode(msgs))

    async def send_connect(self, **kwargs: typing.Any) -> None:
        """ send `Connect` packet to log in to server """
        payload = {
            'cmd': 'Connect',
            'password': self.password, 'name': self.username, 'version': Utils.version_tuple,
            'tags': self.tags, 'items_handling': self.items_handling,
            'uuid': Utils.get_unique_identifier(), 'game': self.game, "slot_data": self.want_slot_data,
        }
        if kwargs:
            payload.update(kwargs)
        await self.send_msgs([payload])


async def process_server_cmd(ctx, args: dict):
    try:
        cmd = args["cmd"]
    except Exception:
        print()
        print(f"[ERROR] Could not get command from {args}")
        raise
    if cmd == 'RoomInfo':
        print('--------------------------------')
        print('Room Information:')
        print('--------------------------------')
        version = args["version"]
        ctx.server_version = Version(*version)

        if "generator_version" in args:
            ctx.generator_version = Version(*args["generator_version"])
            print(f'Server protocol version: {ctx.server_version.as_simple_string()}, '
                  f'generator version: {ctx.generator_version.as_simple_string()}, '
                  f'tags: {", ".join(args["tags"])}')
        else:
            print(f'Server protocol version: {ctx.server_version.as_simple_string()}, '
                  f'tags: {", ".join(args["tags"])}')

        await ctx.send_connect()

    elif cmd == 'ConnectionRefused':
        print()
        errors = args["errors"]
        if 'InvalidSlot' in errors:
            print('[ERROR]: Invalid Slot; please verify that you have connected to the correct world.')
        elif 'InvalidGame' in errors:
            print('[ERROR]: Invalid Game; please verify that you connected with the right game to the correct world.')
        elif 'IncompatibleVersion' in errors:
            print('[ERROR]: Server reported your client version as incompatible. '
                  'This probably means you have to update.')
        elif 'InvalidItemsHandling' in errors:
            print('[ERROR]: The item handling flags requested by the client are not supported')
        elif 'InvalidPassword' in errors:
            print('[ERROR]: Invalid password')
        elif errors:
            raise Exception("Unknown connection errors: " + str(errors))
        else:
            raise Exception('Connection refused by the multiworld host, no reason provided')
        await ctx.disconnect()

    elif cmd == 'Connected':
        game = args["slot_info"][str(args["slot"])].game
        if game != ctx.game:
            print(f"The game is {game}, not {ctx.game}, aborting")
            await ctx.disconnect()
        else:
            ctx.missing_locations = set(args["missing_locations"])
            ctx.checked_locations = set(args["checked_locations"])
            ctx.server_locations = ctx.missing_locations | ctx.checked_locations
            ctx.slot_data = args["slot_data"]
            if ctx.slot_data["client_version"] != "5.0.0":
                print(f"Stardew Valley client version is {ctx.slot_data['client_version']}, while the only supported "
                      f"version is 5.0.0")
                ctx.disconnect()

    elif cmd == 'ReceivedItems':
        start_index = args["index"]

        if start_index == 0:
            ctx.items_received = []
            ctx.watcher_event.set()
        elif start_index != len(ctx.items_received):
            sync_msg = [{'cmd': 'Sync'}]
            await ctx.send_msgs(sync_msg)
        if start_index == len(ctx.items_received):
            for item in args['items']:
                ctx.items_received.append(NetworkItem(*item))
            ctx.watcher_event.set()
        if ctx.watcher_event.is_set():
            print("Items:")
            for value in ctx.items_received:
                print(value)
            await ctx.disconnect()


async def server_loop(ctx: ConnectionContext):
    server_url = urllib.parse.urlparse(ctx.server_address)
    print(f'Connecting to Archipelago server at {ctx.server_address}')
    try:
        port = server_url.port or 38281  # raises ValueError if invalid
        socket = await websockets.connect(ctx.server_address, port=port, ping_timeout=None, ping_interval=None,
                                          ssl=get_ssl_context() if ctx.server_address.startswith("wss://") else None)
        ctx.server = Endpoint(socket)
        print('Connected')
        async for data in ctx.server.socket:
            for msg in decode(data):
                await process_server_cmd(ctx, msg)
                if not ctx.server.socket.open:
                    break
        print(f"Disconnected from multiworld server")
    except websockets.InvalidMessage:
        # probably encrypted
        if ctx.server_address.startswith("ws://"):
            # try wss
            ctx.server_address = "ws" + ctx.server_address[1:]
            await server_loop(ctx)
        else:
            print_with_linebreak("[ERROR]: Lost connection to the multiworld server due to InvalidMessage")
    except ConnectionRefusedError:
        print_with_linebreak("[ERROR]: Connection refused by the server. May not be running Archipelago on that "
                             "address or port.")
    except websockets.InvalidURI:
        print_with_linebreak("[ERROR]: Failed to connect to the multiworld server (invalid URI)")
    except OSError:
        print_with_linebreak("[ERROR]: Failed to connect to the multiworld server")
        raise
    except Exception:
        print_with_linebreak("[ERROR]: Lost connection to the multiworld server")
        raise
    finally:
        await ctx.disconnect()


def print_with_linebreak(msg: str) -> None:
    print()
    print(msg)


def get_ssl_context():
    import certifi
    return ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=certifi.where())


def main():
    with open("stardew_extra_data.yaml", "r") as document:
        yaml_data = yaml.load(document, Loader)
        address = yaml_data["connection"]["server"]
        if "://" not in address:
            address = f"ws://{address}"
        ctx = ConnectionContext(address, yaml_data["connection"]["player"], yaml_data["connection"]["password"])
    asyncio.run(server_loop(ctx))


if __name__ == '__main__':
    main()
