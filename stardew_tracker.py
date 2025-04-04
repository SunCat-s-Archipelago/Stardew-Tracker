import asyncio
import random
import re
import typing
import urllib.parse
from argparse import Namespace
from typing import Optional
import websockets
import ssl
from types import MappingProxyType
from colorama import just_fix_windows_console, Style, Fore, Back

try:
    from yaml import CLoader as Loader, CDumper as Dumper
except ImportError:
    from yaml import Loader, Dumper
import yaml

import Utils
from BaseClasses import MultiWorld, CollectionState, Entrance, Region, Item
from NetUtils import (Endpoint, decode, encode)
from Utils import Version
from worlds.stardew_valley import StardewValleyWorld, StardewLogic, StardewLocation, BundleRoom, set_rules, item_table, \
    items_by_group, Group
from worlds.stardew_valley.bundles.bundle import Bundle
from worlds.stardew_valley.bundles.bundle_item import BundleItem
from worlds.stardew_valley.data.bundle_data import strawberry, vault_carnival_items, pineapple, taro_root, ostrich_egg, \
    banana, mango, deluxe_fertilizer, deluxe_retaining_soil, hyper_speed_gro, tiger_slime_egg, ginger_ale, pina_colada, \
    lionfish, blue_discus, stingray, magic_bait, ginger, magma_cap, vault_qi_helper_items
from worlds.stardew_valley.locations import all_locations, events_locations
from worlds.stardew_valley.options import StardewValleyOptions, EntranceRandomization, SeasonRandomization
from worlds.stardew_valley.region_classes import ConnectionData, RandomizationFlag
from worlds.stardew_valley.regions import RegionFactory, create_final_connections_and_regions, \
    remove_excluded_entrances, create_connections_for_generation, add_non_randomized_connections
from worlds.stardew_valley.items import StardewItemFactory, StardewItemDeleter, remove_items, create_unique_items


def print_with_linebreak(msg: str) -> None:
    print()
    print(msg)


def get_ssl_context():
    import certifi
    return ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=certifi.where())


class ConnectionContext:
    tags: typing.Set[str] = {"Tracker", "NoText"}
    game: typing.Optional[str] = StardewValleyWorld.game
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
    items_received: typing.Set[int]
    missing_locations: typing.Set[int]
    slot_data: typing.Dict[str, typing.Any]

    def __init__(self, server_address: str, username: str, password: str) -> None:
        self.server_address = server_address
        self.username = username
        self.password = password
        self.watcher_event = asyncio.Event()
        self.items_received = set()
        self.missing_locations = set()  # server state
        self.checked_locations = set()  # server state
        self.server_locations = set()  # all locations the server knows of, missing_location | checked_locations
        self.slot_data = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        asyncio.run(self.disconnect())

    async def disconnect(self):
        if self.server and self.server.socket is not None and self.server.socket.state == websockets.protocol.State.OPEN:
            await self.server.socket.close()

    async def send_msgs(self, msgs: typing.List[typing.Any]) -> None:
        """ `msgs` JSON serializable """
        if (not self.server or not self.server.socket.state == websockets.protocol.State.OPEN or
                self.server.socket.state == websockets.protocol.State.CLOSED):
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
            ctx.items_received = set()
            ctx.watcher_event.set()
        elif start_index != len(ctx.items_received):
            sync_msg = [{'cmd': 'Sync'}]
            await ctx.send_msgs(sync_msg)
        if start_index == len(ctx.items_received):
            for item in args['items']:
                ctx.items_received.add(item.item)
            ctx.watcher_event.set()
        if ctx.watcher_event.is_set():
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
                if not ctx.server.socket.state == websockets.protocol.State.OPEN:
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


def create_regions(sw: StardewValleyWorld,
                   modified_bundles_compact: typing.Dict[str, typing.Dict[str, typing.Dict[str, typing.Any]]],
                   server_location_names: typing.Set[str]):
    def create_region(name: str, exits: typing.Iterable[str]) -> Region:
        region = Region(name, sw.player, sw.multiworld)
        region.exits = [Entrance(sw.player, exit_name, region) for exit_name in exits]
        return region

    def create_regions(region_factory: RegionFactory,
                       world_options: StardewValleyOptions,
                       randomized_entrances: typing.Dict) -> typing.Tuple[
        typing.Dict[str, Region], typing.Dict[str, Entrance]]:
        entrances_data, regions_data = create_final_connections_and_regions(world_options)
        regions_by_name: typing.Dict[str: Region] = {
            region_name: region_factory(region_name, regions_data[region_name].exits) for region_name in
            regions_data}
        entrances_by_name: typing.Dict[str: Entrance] = {entrance.name: entrance for region in
                                                         regions_by_name.values() for
                                                         entrance in region.exits
                                                         if entrance.name in entrances_data}

        connections_to_randomize: typing.List[ConnectionData] = []
        connections = None
        if world_options.entrance_randomization == EntranceRandomization.option_chaos:
            connections = list(entrances_data.values())
        elif world_options.entrance_randomization == EntranceRandomization.option_pelican_town:
            connections_to_randomize = [entrances_data[connection] for connection in entrances_data if
                                        RandomizationFlag.PELICAN_TOWN in entrances_data[connection].flag]
        elif world_options.entrance_randomization == EntranceRandomization.option_non_progression:
            connections_to_randomize = [entrances_data[connection] for connection in entrances_data if
                                        RandomizationFlag.NON_PROGRESSION in entrances_data[connection].flag]
        elif world_options.entrance_randomization == EntranceRandomization.option_buildings:
            connections_to_randomize = [entrances_data[connection] for connection in entrances_data if
                                        RandomizationFlag.BUILDINGS in entrances_data[connection].flag]
        if connections is None:
            connections_to_randomize = remove_excluded_entrances(connections_to_randomize, world_options)
            randomized_connections = {}
            for connection in connections_to_randomize:
                destination_name = randomized_entrances[connection.name]
                destination = next(filter(lambda connection: connection.name == destination_name,
                                          connections_to_randomize))
                randomized_connections[connection] = destination
            add_non_randomized_connections(list(entrances_data.values()), connections_to_randomize,
                                           randomized_connections)
            connections = create_connections_for_generation(randomized_connections)
        for connection in connections:
            if connection.name in entrances_by_name:
                entrances_by_name[connection.name].connect(regions_by_name[connection.destination])
        return regions_by_name, entrances_by_name

    world_regions, world_entrances = create_regions(create_region, sw.options,
                                                    SWData.slot_data["randomized_entrances"])
    sw.randomized_entrances = SWData.slot_data["randomized_entrances"]
    sw.logic = StardewLogic(sw.player, sw.options, world_regions.keys())
    modified_bundles: typing.List[BundleRoom] = list()
    festival_bundle_items = {strawberry.item_name, vault_carnival_items.item_name}
    island_bundle_items = {pineapple.item_name, taro_root.item_name, ostrich_egg.item_name, banana.item_name,
                           mango.item_name, deluxe_fertilizer.item_name, deluxe_retaining_soil.item_name,
                           hyper_speed_gro.item_name, tiger_slime_egg.item_name, ginger_ale.item_name,
                           pina_colada.item_name, lionfish.item_name, blue_discus.item_name, stingray.item_name,
                           magic_bait.item_name, ginger.item_name, magma_cap.item_name, vault_qi_helper_items.item_name}
    for room_string, room_bundles_strings in modified_bundles_compact.items():
        bundles: typing.List[Bundle] = list()
        for bundle_string, bundle_items_string in room_bundles_strings.items():
            items: typing.List[BundleItem] = list()
            i = 0
            s = str(i)
            while s in bundle_items_string.keys():
                item_string = bundle_items_string[s]
                name, amount_str, quality = item_string.split("|")
                amount = int(amount_str)
                if name in festival_bundle_items:
                    source = BundleItem.Sources.festival
                elif name in island_bundle_items:
                    source = BundleItem.Sources.island
                else:
                    source = BundleItem.Sources.vanilla
                items.append(BundleItem(name, amount, quality, source))
                i += 1
                s = str(i)
            bundles.append(Bundle(room_string, bundle_string, items, bundle_items_string["number_required"]))
        room = BundleRoom(room_string, bundles)
        modified_bundles.append(room)
    sw.modified_bundles = modified_bundles

    def add_location(name: str, code: Optional[int], region: str):
        region = world_regions[region]
        location = StardewLocation(sw.player, name, code, region)
        region.locations.append(location)

    for location_name in server_location_names:
        location = next(filter(lambda loc: loc.name == location_name, all_locations))
        add_location(location_name, location.code, location.region)

    sw.multiworld.regions.extend(world_regions.values())


class SWData:
    # slot_data contains all options but bundle randomization, and contains
    # how bundles and entrances got randomized
    slot_data: typing.Dict[str, typing.Any] = {}
    missing_location_names: typing.Set[str] = set()
    server_location_names: typing.Set[str] = set()
    items_received: typing.Set[str] = set()


def connect_and_fill_swdata(address, username, password):
    with ConnectionContext(address, username, password) as ctx:
        asyncio.run(server_loop(ctx))
        if not ctx.watcher_event.is_set():
            exit(1)
        SWData.slot_data = ctx.slot_data
        for name, _id in StardewValleyWorld.location_name_to_id.items():
            if _id in ctx.missing_locations:
                SWData.missing_location_names.add(name)
                SWData.server_location_names.add(name)
            elif _id in ctx.server_locations:
                SWData.server_location_names.add(name)
        for name, _id in StardewValleyWorld.item_name_to_id.items():
            if _id in ctx.items_received:
                SWData.items_received.add(name)


def create_items(sw: StardewValleyWorld):
    items_to_exclude = sw.multiworld.precollected_items[sw.player]
    if sw.options.season_randomization == SeasonRandomization.option_disabled:
        items_to_exclude = [item for item in items_to_exclude
                            if item_table[item.name] not in items_by_group[Group.SEASON]]

    def create_items(item_factory: StardewItemFactory, item_deleter: StardewItemDeleter,
                     items_to_exclude: typing.List[Item], options: StardewValleyOptions, random: random.Random) -> typing.List[Item]:
        unique_items = create_unique_items(item_factory, options, random)
        babies_to_exclude = list(filter(lambda item: item.name in [
            _item.name for _item in items_by_group[Group.BABY]
        ], items_to_exclude))
        for item in babies_to_exclude:
            items_to_exclude.remove(item)
        remove_items(item_deleter, items_to_exclude, unique_items)
        added_babies = list(filter(lambda item: item.name in [
            _item.name for _item in items_by_group[Group.BABY]
        ], unique_items))
        for i in range(min(len(babies_to_exclude), 2)):
            baby = added_babies.pop()
            unique_items.remove(baby)
            item_deleter(baby)
        return unique_items

    created_items = create_items(sw.create_item, sw.delete_item, items_to_exclude, sw.options, sw.random)

    sw.multiworld.itempool += created_items
    sw.setup_player_events()
    sw.setup_victory()


def create_multiworld():
    multiworld = MultiWorld(1)
    multiworld.game[1] = StardewValleyWorld.game
    multiworld.player_name = {1: "Player"}
    multiworld.state = CollectionState(multiworld)
    args = Namespace()

    for name, option in StardewValleyWorld.options_dataclass.type_hints.items():
        options = {}
        value = option(SWData.slot_data[name]) if name in SWData.slot_data else option.from_any(option.default)
        options.update({1: value})
        setattr(args, name, options)
    multiworld.set_options(args)
    sw_world = multiworld.worlds[1]

    # gen_steps = ("generate_early", "create_regions", "create_items", "set_rules", "generate_basic", "pre_fill")

    # no generate early

    create_regions(sw_world, SWData.slot_data["modified_bundles"], SWData.server_location_names)

    # treat server inventory as starting inventory
    for item_name in SWData.items_received:
        item = sw_world.create_starting_item(item_name)
        if item.advancement:
            multiworld.push_precollected(item)
    create_items(sw_world)

    set_rules(sw_world)

    # no generate_basic

    # no pre_fill

    return multiworld


def build_map_for_sorting(multiworld: MultiWorld) -> MappingProxyType[str, typing.Tuple[typing.Optional[str], int]]:
    region_name_to_parent_name_mutable: typing.Dict[str, typing.Tuple[typing.Optional[str], int]] = {}
    regions_to_visit = set(multiworld.regions.region_cache[1].values())
    region = multiworld.regions.region_cache[1]["Menu"]
    queue = []
    regions_to_visit.remove(region)
    region_name_to_parent_name_mutable[region.name] = (None, 0)
    candidates: typing.List[str] = []
    while True:
        min_depth = -1
        for i in range(len(region.exits)):
            connected_region = region.exits[i].connected_region
            if connected_region in regions_to_visit:
                regions_to_visit.remove(connected_region)
                queue.append(connected_region)
        if region.name != "Menu":
            for i in range(len(region.entrances)):
                parent_region = region.entrances[i].parent_region
                if parent_region.name in region_name_to_parent_name_mutable.keys():
                    cur_depth = region_name_to_parent_name_mutable[parent_region.name][1]
                    if min_depth == -1 or min_depth < cur_depth:
                        min_depth = cur_depth
                        candidates.clear()
                        candidates.append(parent_region.name)
                    elif min_depth == cur_depth:
                        candidates.append(parent_region.name)
            candidates.sort()
            region_name_to_parent_name_mutable[region.name] = (candidates[0],
                                                               min_depth + 1)
        if len(queue) <= 0:
            break
        region = queue.pop(0)
        candidates.clear()

    return MappingProxyType(region_name_to_parent_name_mutable)


def main():
    with open("stardew_tracker_options.yaml", "r") as document:
        yaml_data = yaml.load(document, Loader)
        address = yaml_data["connection"]["server"]
        if "://" not in address:
            address = f"ws://{address}"
    username = yaml_data["connection"]["player"]
    password = yaml_data["connection"]["password"]

    connect_and_fill_swdata(address, username, password)

    # https://stackoverflow.com/a/16090640
    # https://creativecommons.org/licenses/by-sa/3.0/
    def natural_sort_key(s, _nsre=re.compile(r'(\d+)')):
        return [int(text) if text.isdigit() else text.lower()
                for text in _nsre.split(s)]

    def loc_alphabetical_sort_key(loc: StardewLocation):
        return loc.name

    def loc_natural_sort_key(loc: StardewLocation):
        return natural_sort_key(loc.name)

    def loc_type_sort_key(loc: StardewLocation):
        return 1 if loc.event else 0

    def event_sort_key(event: StardewLocation):
        return [event.parent_region.name, event.name]

    multiworld = create_multiworld()

    region_name_to_parent_name = build_map_for_sorting(multiworld)

    def tuple_region_sort_key(t: typing.Tuple[Region, typing.Any], _region_name_to_parent_name: MappingProxyType[str,
                              typing.Tuple[typing.Optional[str], int]] =
                              region_name_to_parent_name):
        _region = t[0]
        region_name = _region.name
        lst = []
        while region_name is not None:
            lst.insert(0, region_name)
            region_name = _region_name_to_parent_name[region_name][0]
        return lst

    locations_to_check_per_region: typing.List[typing.Tuple[Region, typing.List[StardewLocation]]] = []
    event_location_list: typing.List[StardewLocation] = []
    for region in multiworld.regions:
        location_list: typing.List[StardewLocation] = []
        for loc in region.locations:
            if not loc.event and loc.name in SWData.missing_location_names:
                location_list.append(loc)
            elif loc.event:
                event_location_list.append(loc)
        if len(location_list) > 0:
            locations_to_check_per_region.append((region, location_list))
        location_list.sort(key=loc_alphabetical_sort_key)
        location_list.sort(key=loc_natural_sort_key)
        location_list.sort(key=loc_type_sort_key)
    locations_to_check_per_region.sort(key=tuple_region_sort_key)
    goal_names = [ev.name for ev in events_locations]
    goal: StardewLocation = next(filter(lambda loc: loc.name in goal_names, event_location_list))
    event_location_list.remove(goal)
    event_location_list.sort(key=event_sort_key)

    print()
    just_fix_windows_console()
    while True:
        accessible_event_loc = None
        out_region_names, out_region_to_locations = extract_accessible_locations(multiworld,
                                                                                 locations_to_check_per_region)
        for reg in out_region_names:
            print(f"{Fore.BLACK}{Back.WHITE}Region: {reg}{Style.RESET_ALL}")
            for loc in out_region_to_locations[reg]:
                print(loc)
        for event in event_location_list:
            if event.can_reach(multiworld.state):
                accessible_event_loc = event
                break
        if accessible_event_loc is None:
            break
        event_location_list.remove(accessible_event_loc)
        multiworld.push_precollected(accessible_event_loc.item)
        print(f"{Fore.BLACK}{Back.BLUE}{Back.LIGHTBLACK_EX}[Event: {accessible_event_loc.name}]{Style.RESET_ALL}")
    is_goal_accessible = goal.can_reach(multiworld.state)
    print(f"{Fore.BLACK}{Back.BLUE}{Back.LIGHTBLACK_EX}[Goal: {goal.name}, is {'' if is_goal_accessible else 'not '}" +
          f"accessible]{Style.RESET_ALL}")


def extract_accessible_locations(mw: MultiWorld, locs_per_region: typing.List[typing.Tuple[Region, typing.List[StardewLocation]]]):
    output_region_to_locations: typing.Dict[str, typing.List[str]] = {}
    output_regions: typing.List[str] = []
    for _tuple in locs_per_region[:]:
        region = _tuple[0]
        if len(_tuple[1]) == 0:
            locs_per_region.remove(_tuple)
            continue
        if not region.can_reach(mw.state):
            continue
        if region.name not in output_region_to_locations.keys():
            output_region_to_locations[region.name] = []
        locs = _tuple[1]
        for loc in locs:
            if loc.can_reach(mw.state):
                output_region_to_locations[region.name].append(loc.name)
        if len(output_region_to_locations[region.name]) > 0:
            output_regions.append(region.name)
            locs[:] = (loc for loc in locs if loc.name not in output_region_to_locations[region.name])
        if len(locs) == 0:
            locs_per_region.remove(_tuple)
    return output_regions, output_region_to_locations


if __name__ == '__main__':
    main()
    print("Press Enter to finish running the program")
    input()
